//! HTTP semantics of the shared core.
//!
//! This crate owns routing, request decoding, authorization and response shape. It
//! knows nothing about Spin, Wasm, or a socket, which is deliberate: every rule here
//! is exercised by native tests, and `spin-core` is only an adapter onto it.
//!
//! # Authorization model
//!
//! A request carries a principal. The principal's scopes are supplied by the caller of
//! [`Service::handle`] — in the deployed component, from the authenticated transport.
//! A body never states its own scopes, dataset path, SQL, storage key or recipient;
//! those are either server-derived or refused. That is why there is no "query" route
//! that accepts SQL text: dataset selection is by catalog name only.

mod routes;

pub use routes::{Reply, Request};

use blueprint_contracts::error::ErrorCode;
use blueprint_contracts::event::EventSource;
use blueprint_contracts::limits::Limits;
use blueprint_contracts::{Rejection, error::Scope};
use blueprint_state::{IngestOutcome, StateError, Store};

/// The authenticated caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Principal {
    pub name: String,
    pub scopes: Vec<Scope>,
    /// The source this principal is allowed to claim in an event envelope. `None` for
    /// principals that may not publish events at all.
    pub event_source: Option<EventSource>,
}

impl Principal {
    pub fn has(&self, scope: Scope) -> bool {
        self.scopes.contains(&scope)
    }
}

/// The core application. One instance per running host; the store is the single
/// writer and is not shared between threads.
pub struct Service {
    store: Store,
    limits: Limits,
}

impl Service {
    pub fn new(store: Store, limits: Limits) -> Self {
        Self { store, limits }
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    pub fn store(&self) -> &Store {
        &self.store
    }

    pub fn store_mut(&mut self) -> &mut Store {
        &mut self.store
    }

    /// Route and execute one request.
    ///
    /// `transport_source` is what the transport authenticated; it is never read from
    /// the body. Errors come back as [`Reply`]s with the contract error object, so a
    /// caller always sees a uniform failure shape.
    pub fn handle(&mut self, request: &Request, principal: &Principal) -> Reply {
        match routes::dispatch(self, request, principal) {
            Ok(reply) => reply,
            Err(failure) => failure.into_reply(),
        }
    }

    pub(crate) fn ingest_event(
        &mut self,
        body: &[u8],
        principal: &Principal,
        received_at: &str,
    ) -> Result<Reply, Failure> {
        let source = principal.event_source.ok_or_else(|| {
            Failure::from(Rejection::new(
                ErrorCode::ForbiddenScope,
                "principal may not publish events",
            ))
        })?;
        blueprint_contracts::error::require_scopes(&principal.scopes, &[Scope::TelemetryWrite])
            .map_err(Failure::from)?;
        if body.len() > self.limits.request.max_body_bytes {
            return Err(Failure::from(Rejection::new(
                ErrorCode::PayloadTooLarge,
                "event body exceeds the configured maximum",
            )));
        }
        let envelope = blueprint_contracts::event::Envelope::parse(body, source, &self.limits)
            .map_err(Failure::from)?;
        let outcome = self
            .store
            .ingest_event(&envelope, received_at)
            .map_err(Failure::from)?;
        let (status, duplicate) = match outcome {
            IngestOutcome::Recorded => (201, false),
            IngestOutcome::Duplicate => (200, true),
        };
        Ok(Reply::json(
            status,
            serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "eventID": envelope.event_id,
                "duplicate": duplicate,
            }),
        ))
    }
}

/// A failure on its way to the wire.
#[derive(Debug)]
pub(crate) struct Failure {
    pub error: blueprint_contracts::error::ApiError,
}

impl From<Rejection> for Failure {
    fn from(rejection: Rejection) -> Self {
        Self {
            error: rejection.into_error(),
        }
    }
}

impl From<StateError> for Failure {
    fn from(error: StateError) -> Self {
        match error {
            StateError::Contract(api) => Self { error: api },
            other => {
                // A storage failure never leaks SQL or a path to the caller.
                let _ = other;
                Self {
                    error: Rejection::new(ErrorCode::Internal, "storage operation failed")
                        .into_error(),
                }
            }
        }
    }
}

impl Failure {
    pub(crate) fn forbidden(message: &'static str) -> Self {
        Self::from(Rejection::new(ErrorCode::ForbiddenScope, message))
    }

    pub(crate) fn invalid(message: &'static str) -> Self {
        Self::from(Rejection::new(ErrorCode::InvalidRequest, message))
    }

    pub(crate) fn not_found(message: &'static str) -> Self {
        Self::from(Rejection::new(ErrorCode::NotFound, message))
    }

    pub(crate) fn into_reply(self) -> Reply {
        let status = status_for(self.error.code);
        Reply::json(
            status,
            serde_json::to_value(&self.error).unwrap_or_else(|_| {
                serde_json::json!({
                    "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                    "code": "internal",
                    "message": "response encoding failed",
                    "retryable": false
                })
            }),
        )
    }
}

fn status_for(code: ErrorCode) -> u16 {
    match code {
        ErrorCode::InvalidEnvelope | ErrorCode::InvalidRequest => 400,
        ErrorCode::Unauthorized => 401,
        ErrorCode::ForbiddenScope => 403,
        ErrorCode::NotFound => 404,
        ErrorCode::Conflict => 409,
        ErrorCode::PayloadTooLarge | ErrorCode::QueueFull => 413,
        ErrorCode::LeaseLost | ErrorCode::OutcomeUnknown => 409,
        ErrorCode::RateLimited => 429,
        ErrorCode::Unavailable | ErrorCode::Internal => 503,
    }
}
