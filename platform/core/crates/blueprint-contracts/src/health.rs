//! Health state derivation, transitions and notification dedup keys.
//!
//! Two properties matter. Health is derived from a bounded window, so one sample
//! cannot flip it; and every state change is one immutable transition row whose id
//! is a deterministic function of its content. Notifications key off that transition
//! id, which makes redelivery a duplicate instead of a second page.

use serde::{Deserialize, Serialize};

use crate::{Rejection, error::ErrorCode, ids};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum HealthState {
    Unknown,
    Healthy,
    Degraded,
    Failed,
}

impl HealthState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Healthy => "healthy",
            Self::Degraded => "degraded",
            Self::Failed => "failed",
        }
    }
}

/// Thresholds that turn a sample window into a state. They are inputs, not magic
/// numbers, so the closed-loop tests can drive a fast window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HealthPolicy {
    /// Consecutive failures that move healthy -> degraded.
    pub degraded_after_failures: u32,
    /// Consecutive failures that move degraded -> failed.
    pub failed_after_failures: u32,
    /// Consecutive successes that move failed/degraded -> healthy.
    pub healthy_after_successes: u32,
}

impl Default for HealthPolicy {
    fn default() -> Self {
        Self {
            degraded_after_failures: 1,
            failed_after_failures: 3,
            healthy_after_successes: 2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckRecord {
    pub service: String,
    pub check_id: String,
    pub state: HealthState,
    pub consecutive_failures: u32,
    pub consecutive_successes: u32,
    pub last_observed_at: Option<String>,
    pub since: Option<String>,
}

impl CheckRecord {
    pub fn new(service: &str, check_id: &str) -> Self {
        Self {
            service: service.to_owned(),
            check_id: check_id.to_owned(),
            state: HealthState::Unknown,
            consecutive_failures: 0,
            consecutive_successes: 0,
            last_observed_at: None,
            since: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Observation {
    pub service: String,
    pub check_id: String,
    pub ok: bool,
    pub observed_at: String,
    pub observed_by: String,
}

/// The outcome of one observation: the mutated record plus an optional transition
/// that must be persisted exactly once.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObservationResult {
    pub record: CheckRecord,
    pub transition: Option<Transition>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Transition {
    pub transition_id: String,
    pub service: String,
    pub check_id: String,
    pub from: HealthState,
    pub to: HealthState,
    pub at: String,
}

/// Apply one observation. An observation older than the last stored one is refused
/// as stale rather than applied, so an out-of-order retry cannot rewrite history.
pub fn observe(
    record: &CheckRecord,
    observation: &Observation,
    policy: HealthPolicy,
) -> Result<ObservationResult, Rejection> {
    if record.service != observation.service || record.check_id != observation.check_id {
        return Err(Rejection::new(
            ErrorCode::InvalidRequest,
            "observation does not match the check it claims",
        ));
    }
    if let Some(last) = &record.last_observed_at
        && observation.observed_at.as_str() < last.as_str()
    {
        return Err(Rejection::new(
            ErrorCode::Conflict,
            "observation is older than the stored one and was not applied",
        ));
    }
    let mut next = record.clone();
    next.last_observed_at = Some(observation.observed_at.clone());
    if observation.ok {
        next.consecutive_successes = next.consecutive_successes.saturating_add(1);
        next.consecutive_failures = 0;
    } else {
        next.consecutive_failures = next.consecutive_failures.saturating_add(1);
        next.consecutive_successes = 0;
    }

    let state = if observation.ok {
        if next.consecutive_successes >= policy.healthy_after_successes {
            HealthState::Healthy
        } else {
            record.state
        }
    } else if next.consecutive_failures >= policy.failed_after_failures {
        HealthState::Failed
    } else if next.consecutive_failures >= policy.degraded_after_failures {
        HealthState::Degraded
    } else {
        record.state
    };

    let transition = if state != record.state {
        let transition = Transition {
            transition_id: ids::health_transition_id(
                &record.service,
                &record.check_id,
                record.state.as_str(),
                state.as_str(),
                &observation.observed_at,
            ),
            service: record.service.clone(),
            check_id: record.check_id.clone(),
            from: record.state,
            to: state,
            at: observation.observed_at.clone(),
        };
        next.state = state;
        next.since = Some(observation.observed_at.clone());
        Some(transition)
    } else {
        None
    };

    Ok(ObservationResult {
        record: next,
        transition,
    })
}

/// Notification dedup key: one transition reaches one channel once.
pub fn notification_dedupe_key(transition_id: &str, channel: &str) -> String {
    format!("{transition_id}\u{1f}{channel}")
}
