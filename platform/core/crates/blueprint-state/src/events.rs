//! Event ingestion and dedup.
//!
//! Ingestion is one transaction: record the dedup key and the event together. A
//! redelivery hits the primary key and is answered with `duplicate` while the stored
//! event is left untouched, so no downstream consumer sees a second fact.

use blueprint_contracts::error::ErrorCode;
use blueprint_contracts::event::Envelope;
use rusqlite::params;

use crate::{Store, error::StateError, invalid};

/// What happened to an ingested event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IngestOutcome {
    /// First time this key was seen; the event is durable.
    Recorded,
    /// The key already existed; nothing was written.
    Duplicate,
}

impl Store {
    /// Validate and persist an envelope.
    ///
    /// `received_at` is the core's own clock, stored next to the producer-claimed
    /// `occurred_at` so skew is visible in data rather than inferred later.
    pub fn ingest_event(
        &mut self,
        envelope: &Envelope,
        received_at: &str,
    ) -> Result<IngestOutcome, StateError> {
        let dedupe_key = envelope.dedupe_key();
        let attributes = serde_json::to_string(&envelope.attributes).map_err(|error| {
            StateError::Corrupt(format!("attributes are not serializable: {error}"))
        })?;
        let tx = self.connection_mut().transaction()?;
        let inserted = tx.execute(
            "INSERT OR IGNORE INTO events (\
             dedupe_key, event_id, source, service, event_type, severity, occurred_at, \
             received_at, trace_id, span_id, attributes) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                dedupe_key,
                envelope.event_id,
                envelope.source.as_str(),
                envelope.service,
                envelope.event_type,
                envelope.severity.map(|s| format!("{s:?}").to_lowercase()),
                envelope.time,
                received_at,
                envelope.trace_id,
                envelope.span_id,
                attributes,
            ],
        )?;
        tx.commit()?;
        Ok(if inserted == 1 {
            IngestOutcome::Recorded
        } else {
            IngestOutcome::Duplicate
        })
    }

    /// Number of stored events for a service. Used by the bounded-behaviour tests.
    pub fn event_count(&self, service: &str) -> Result<i64, StateError> {
        let count = self.connection().query_row(
            "SELECT COUNT(*) FROM events WHERE service = ?1",
            params![service],
            |row| row.get(0),
        )?;
        Ok(count)
    }

    /// Read one stored event's attributes back, for the redaction assertion.
    pub fn stored_attributes(&self, dedupe_key: &str) -> Result<Option<String>, StateError> {
        use crate::OptionalExt as _;
        let value = self
            .connection()
            .query_row(
                "SELECT attributes FROM events WHERE dedupe_key = ?1",
                params![dedupe_key],
                |row| row.get(0),
            )
            .optional()?;
        Ok(value)
    }

    /// Record a request id so a retried non-event request is acknowledged once.
    ///
    /// Returns `true` when this call created the record and the caller may proceed,
    /// `false` when the request was already handled and the stored response digest
    /// should be replayed instead of repeating the side effect.
    pub fn begin_request(
        &mut self,
        request_key: &str,
        principal: &str,
        operation: &str,
        received_at: &str,
    ) -> Result<bool, StateError> {
        if request_key.len() < 8 || request_key.len() > 256 {
            return Err(crate::contract_error(invalid(
                "request key length is outside the contract range",
            )));
        }
        let inserted = self.connection().execute(
            "INSERT OR IGNORE INTO request_dedup (request_key, principal, operation, received_at) \
             VALUES (?1, ?2, ?3, ?4)",
            params![request_key, principal, operation, received_at],
        )?;
        Ok(inserted == 1)
    }

    /// Close a request with the digest of the response that was produced.
    pub fn finish_request(
        &mut self,
        request_key: &str,
        response_digest: &str,
    ) -> Result<(), StateError> {
        self.connection().execute(
            "UPDATE request_dedup SET response_digest = ?2 WHERE request_key = ?1",
            params![request_key, response_digest],
        )?;
        Ok(())
    }

    /// Refuse a request whose key belongs to a different principal or operation, so a
    /// guessed key cannot hijack another caller's recorded result.
    pub fn request_owner(
        &self,
        request_key: &str,
    ) -> Result<Option<(String, String, Option<String>)>, StateError> {
        use crate::OptionalExt as _;
        let row = self
            .connection()
            .query_row(
                "SELECT principal, operation, response_digest FROM request_dedup \
                 WHERE request_key = ?1",
                params![request_key],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        Ok(row)
    }
}

/// Map a contract rejection from envelope validation into a state error.
pub fn reject_envelope(rejection: blueprint_contracts::Rejection) -> StateError {
    debug_assert_eq!(
        rejection.code,
        ErrorCode::InvalidEnvelope,
        "envelope rejections carry the invalid_envelope code"
    );
    crate::contract_error(rejection)
}
