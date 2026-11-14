//! Health observation application and notification dedup.
//!
//! Applying an observation is one transaction that updates the latest row and, when
//! the state changed, inserts exactly one transition plus one pending notification
//! per configured channel. The notification row is keyed by (transition, channel), so
//! a retry after a failed delivery resumes the same row instead of creating a second
//! message.

use blueprint_contracts::health::{HealthPolicy, HealthState, Observation, Transition};
use rusqlite::params;

use crate::Store;
use crate::error::StateError;

/// A notification row after a transition was recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingNotification {
    pub dedupe_key: String,
    pub transition_id: String,
    pub channel: String,
}

impl Store {
    /// Ensure a check row exists, in `unknown`, without inventing a transition.
    pub fn register_check(
        &mut self,
        service: &str,
        check_id: &str,
        kind: &str,
        now: &str,
    ) -> Result<(), StateError> {
        self.connection().execute(
            "INSERT OR IGNORE INTO health_checks \
             (service, check_id, kind, state, updated_at) \
             VALUES (?1, ?2, ?3, 'unknown', ?4)",
            params![service, check_id, kind, now],
        )?;
        Ok(())
    }

    pub fn check_state(
        &self,
        service: &str,
        check_id: &str,
    ) -> Result<Option<HealthState>, StateError> {
        use crate::OptionalExt as _;
        let raw: Option<String> = self
            .connection()
            .query_row(
                "SELECT state FROM health_checks WHERE service = ?1 AND check_id = ?2",
                params![service, check_id],
                |row| row.get(0),
            )
            .optional()?;
        raw.map(|value| parse_state(&value)).transpose()
    }

    /// Apply one observation.
    ///
    /// Returns the transition that was recorded, if the state changed. `channels` are
    /// the configured notification channels; the caller never supplies recipients.
    pub fn apply_observation(
        &mut self,
        observation: &Observation,
        policy: HealthPolicy,
        channels: &[&str],
        received_at: &str,
    ) -> Result<Option<Transition>, StateError> {
        let record = self
            .load_check(&observation.service, &observation.check_id)?
            .ok_or_else(|| {
                crate::contract_error(blueprint_contracts::Rejection::at(
                    blueprint_contracts::error::ErrorCode::NotFound,
                    "check is not registered",
                    "checkID",
                ))
            })?;
        let result = blueprint_contracts::health::observe(&record, observation, policy)
            .map_err(crate::contract_error)?;

        let tx = self.connection_mut().transaction()?;
        tx.execute(
            "UPDATE health_checks SET state = ?3, consecutive_failures = ?4, \
             consecutive_successes = ?5, last_observed_at = ?6, since = ?7, updated_at = ?8 \
             WHERE service = ?1 AND check_id = ?2",
            params![
                result.record.service,
                result.record.check_id,
                result.record.state.as_str(),
                result.record.consecutive_failures,
                result.record.consecutive_successes,
                result.record.last_observed_at,
                result.record.since,
                received_at,
            ],
        )?;

        let Some(transition) = result.transition else {
            tx.commit()?;
            return Ok(None);
        };

        // The transition id is content-derived, so a replay of the same observation
        // conflicts here instead of duplicating the log or the page.
        let inserted = tx.execute(
            "INSERT OR IGNORE INTO health_transitions \
             (transition_id, service, check_id, from_state, to_state, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                transition.transition_id,
                transition.service,
                transition.check_id,
                transition.from.as_str(),
                transition.to.as_str(),
                transition.at,
            ],
        )?;
        if inserted == 0 {
            tx.commit()?;
            return Ok(None);
        }
        for channel in channels {
            let dedupe_key = blueprint_contracts::health::notification_dedupe_key(
                &transition.transition_id,
                channel,
            );
            tx.execute(
                "INSERT OR IGNORE INTO notifications \
                 (dedupe_key, transition_id, channel, state, attempts, updated_at) \
                 VALUES (?1, ?2, ?3, 'pending', 0, ?4)",
                params![dedupe_key, transition.transition_id, channel, received_at],
            )?;
        }
        tx.commit()?;
        Ok(Some(transition))
    }

    /// Pending notifications in backoff order, bounded by `limit`.
    pub fn pending_notifications(
        &self,
        now: &str,
        limit: usize,
    ) -> Result<Vec<PendingNotification>, StateError> {
        let limit = limit.min(self.limits().page.max_size) as i64;
        let mut statement = self.connection().prepare(
            "SELECT dedupe_key, transition_id, channel FROM notifications \
             WHERE state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?1) \
             ORDER BY updated_at LIMIT ?2",
        )?;
        let rows = statement.query_map(params![now, limit], |row| {
            Ok(PendingNotification {
                dedupe_key: row.get(0)?,
                transition_id: row.get(1)?,
                channel: row.get(2)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Mark a notification delivered. Idempotent: a second call changes nothing and
    /// does not re-send, because the state is already terminal.
    pub fn mark_notification_delivered(
        &mut self,
        dedupe_key: &str,
        now: &str,
    ) -> Result<bool, StateError> {
        let updated = self.connection().execute(
            "UPDATE notifications SET state = 'delivered', attempts = attempts + 1, \
             last_attempt_at = ?2, next_attempt_at = NULL, updated_at = ?2 \
             WHERE dedupe_key = ?1 AND state = 'pending'",
            params![dedupe_key, now],
        )?;
        Ok(updated == 1)
    }

    /// Record a retryable delivery failure with a bounded attempt count.
    pub fn mark_notification_retry(
        &mut self,
        dedupe_key: &str,
        now: &str,
        next_attempt_at: &str,
    ) -> Result<(), StateError> {
        let max_attempts = self.limits().storage.max_notification_attempts;
        self.connection().execute(
            "UPDATE notifications SET attempts = attempts + 1, last_attempt_at = ?2, \
             next_attempt_at = ?3, updated_at = ?2, \
             state = CASE WHEN attempts + 1 >= ?4 THEN 'failed' ELSE 'pending' END \
             WHERE dedupe_key = ?1 AND state = 'pending'",
            params![dedupe_key, now, next_attempt_at, max_attempts],
        )?;
        Ok(())
    }

    /// Same transition delivered twice is one stored delivery.
    pub fn notification_state(&self, dedupe_key: &str) -> Result<Option<String>, StateError> {
        use crate::OptionalExt as _;
        let state: Option<String> = self
            .connection()
            .query_row(
                "SELECT state FROM notifications WHERE dedupe_key = ?1",
                params![dedupe_key],
                |row| row.get(0),
            )
            .optional()?;
        Ok(state)
    }

    pub fn transition_count(&self, service: &str) -> Result<i64, StateError> {
        let count = self.connection().query_row(
            "SELECT COUNT(*) FROM health_transitions WHERE service = ?1",
            params![service],
            |row| row.get(0),
        )?;
        Ok(count)
    }

    /// Current health rows, bounded by `limit`. Names and states only: no probe detail,
    /// no recipient, nothing a caller could turn into a path.
    pub fn list_checks(&self, limit: i64) -> Result<Vec<serde_json::Value>, StateError> {
        let mut statement = self.connection().prepare(
            "SELECT service, check_id, kind, state, consecutive_failures, \
             consecutive_successes, last_observed_at, since FROM health_checks \
             ORDER BY service, check_id LIMIT ?1",
        )?;
        let rows = statement.query_map(params![limit], |row| {
            Ok(serde_json::json!({
                "service": row.get::<_, String>(0)?,
                "checkID": row.get::<_, String>(1)?,
                "kind": row.get::<_, String>(2)?,
                "state": row.get::<_, String>(3)?,
                "consecutiveFailures": row.get::<_, i64>(4)?,
                "consecutiveSuccesses": row.get::<_, i64>(5)?,
                "lastObservedAt": row.get::<_, Option<String>>(6)?,
                "since": row.get::<_, Option<String>>(7)?,
            }))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    fn load_check(
        &self,
        service: &str,
        check_id: &str,
    ) -> Result<Option<blueprint_contracts::health::CheckRecord>, StateError> {
        use crate::OptionalExt as _;
        let row = self
            .connection()
            .query_row(
                "SELECT state, consecutive_failures, consecutive_successes, \
                 last_observed_at, since FROM health_checks \
                 WHERE service = ?1 AND check_id = ?2",
                params![service, check_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, Option<String>>(3)?,
                        row.get::<_, Option<String>>(4)?,
                    ))
                },
            )
            .optional()?;
        let Some((state, failures, successes, last, since)) = row else {
            return Ok(None);
        };
        Ok(Some(blueprint_contracts::health::CheckRecord {
            service: service.to_owned(),
            check_id: check_id.to_owned(),
            state: parse_state(&state)?,
            consecutive_failures: failures as u32,
            consecutive_successes: successes as u32,
            last_observed_at: last,
            since,
        }))
    }
}

fn parse_state(raw: &str) -> Result<HealthState, StateError> {
    match raw {
        "unknown" => Ok(HealthState::Unknown),
        "healthy" => Ok(HealthState::Healthy),
        "degraded" => Ok(HealthState::Degraded),
        "failed" => Ok(HealthState::Failed),
        other => Err(StateError::Corrupt(format!("unknown health state {other}"))),
    }
}
