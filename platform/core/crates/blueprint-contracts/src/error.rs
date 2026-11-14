//! Standardized error and authorization scope vocabulary.

use std::fmt;

use serde::{Deserialize, Serialize};

/// Closed set of rejection codes. A caller branches on these, so adding one is a
/// contract change accompanied by a schema version check.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidEnvelope,
    InvalidRequest,
    PayloadTooLarge,
    Unauthorized,
    ForbiddenScope,
    NotFound,
    Conflict,
    LeaseLost,
    OutcomeUnknown,
    RateLimited,
    QueueFull,
    Unavailable,
    Internal,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InvalidEnvelope => "invalid_envelope",
            Self::InvalidRequest => "invalid_request",
            Self::PayloadTooLarge => "payload_too_large",
            Self::Unauthorized => "unauthorized",
            Self::ForbiddenScope => "forbidden_scope",
            Self::NotFound => "not_found",
            Self::Conflict => "conflict",
            Self::LeaseLost => "lease_lost",
            Self::OutcomeUnknown => "outcome_unknown",
            Self::RateLimited => "rate_limited",
            Self::QueueFull => "queue_full",
            Self::Unavailable => "unavailable",
            Self::Internal => "internal",
        }
    }

    /// Whether an identical retry could succeed with no operator action.
    ///
    /// `OutcomeUnknown` is deliberately not retryable: the side effect may already
    /// have happened, and a retry would duplicate it. Authorization failures are
    /// likewise not retryable, because retrying cannot grant a scope.
    pub fn retryable(self) -> bool {
        matches!(
            self,
            Self::RateLimited | Self::QueueFull | Self::Unavailable | Self::Internal
        )
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The wire form of every non-2xx reply.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ApiError {
    pub schema_version: String,
    pub code: ErrorCode,
    pub message: String,
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
}

/// A capability a principal holds. Requests are authorized against these on the
/// server; a client-supplied scope in a body is never trusted as the grant.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Scope {
    #[serde(rename = "health:read")]
    HealthRead,
    #[serde(rename = "task:read")]
    TaskRead,
    #[serde(rename = "task:write")]
    TaskWrite,
    #[serde(rename = "telemetry:read")]
    TelemetryRead,
    #[serde(rename = "telemetry:write")]
    TelemetryWrite,
    #[serde(rename = "query:read")]
    QueryRead,
}

impl Scope {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::HealthRead => "health:read",
            Self::TaskRead => "task:read",
            Self::TaskWrite => "task:write",
            Self::TelemetryRead => "telemetry:read",
            Self::TelemetryWrite => "telemetry:write",
            Self::QueryRead => "query:read",
        }
    }
}

/// Require every scope in `required`, rejecting with `forbidden_scope` naming only
/// the missing scope. The message never echoes a path, key or SQL fragment.
pub fn require_scopes(held: &[Scope], required: &[Scope]) -> Result<(), super::Rejection> {
    for scope in required {
        if !held.contains(scope) {
            return Err(super::Rejection::new(
                ErrorCode::ForbiddenScope,
                "principal does not hold the required scope",
            ));
        }
    }
    Ok(())
}
