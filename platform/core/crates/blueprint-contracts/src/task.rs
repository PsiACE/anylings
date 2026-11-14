//! Task lifecycle, lease generations and result reporting.
//!
//! The state machine here is the contract, not an implementation detail:
//!
//! ```text
//! submitted --claim--> leased --report(ok)--> succeeded
//!     |                  |  \--report(err)--> failed
//!     |                  |  \--cancel------> cancelled (after worker stops)
//!     |                  |  \--lease expiry, idempotent----> submitted (reclaimable)
//!     |                  |  \--lease expiry, non-idempotent--> outcome_unknown (never auto-run)
//!     \--cancel--------> cancelled
//! ```
//!
//! Two rules carry the safety property. A stale generation is refused, so a worker
//! that lost its lease cannot settle someone else's claim. And an expired lease on
//! non-idempotent work never re-runs automatically: the worker may already have
//! performed the side effect, so the only safe move is `outcome_unknown` until an
//! explicit reconciliation decides.

use serde::{Deserialize, Serialize};

use crate::{CONTRACT_VERSION, Rejection, error::ErrorCode};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TaskKind {
    Probe,
    Query,
    Archive,
    Verify,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskState {
    Submitted,
    Leased,
    CancelRequested,
    OutcomeUnknown,
    Succeeded,
    Failed,
    Cancelled,
    Expired,
}

impl TaskState {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Succeeded | Self::Failed | Self::Cancelled | Self::Expired
        )
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Submitted => "submitted",
            Self::Leased => "leased",
            Self::CancelRequested => "cancel_requested",
            Self::OutcomeUnknown => "outcome_unknown",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Expired => "expired",
        }
    }
}

/// What a caller asks the core to record. Parameters stay server-side; the caller
/// gets back only `paramsDigest`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskSubmission {
    pub task_id: String,
    pub service: String,
    pub kind: TaskKind,
    pub idempotent: bool,
    pub submitted_by: String,
    pub params_digest: String,
    pub param_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskRecord {
    pub task_id: String,
    pub service: String,
    pub kind: TaskKind,
    pub idempotent: bool,
    pub state: TaskState,
    pub submitted_by: String,
    pub params_digest: String,
    pub generation: u32,
    pub worker_id: Option<String>,
    pub deadline: Option<String>,
    pub outcome: Option<ReportOutcome>,
    pub result_digest: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lease {
    pub task_id: String,
    pub generation: u32,
    pub worker_id: String,
    pub granted_at: String,
    pub expires_at: String,
}

/// A worker's report. The generation is mandatory: a report without a live claim
/// generation cannot settle a task.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Report {
    pub task_id: String,
    pub generation: u32,
    pub worker_id: String,
    pub outcome: ReportOutcome,
    pub reported_at: String,
    pub result_digest: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ReportOutcome {
    Succeeded,
    Failed,
}

impl ReportOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
        }
    }

    pub fn task_state(self) -> TaskState {
        match self {
            Self::Succeeded => TaskState::Succeeded,
            Self::Failed => TaskState::Failed,
        }
    }
}

/// The result of accepting a report. `Duplicate` is a success, not an error: the
/// first accepted outcome is returned unchanged so a retrying worker converges
/// instead of inventing a second verdict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReportAcceptance {
    Accepted { state: TaskState, generation: u32 },
    Duplicate { state: TaskState, generation: u32 },
}

impl ReportAcceptance {
    pub fn state(&self) -> TaskState {
        match self {
            Self::Accepted { state, .. } | Self::Duplicate { state, .. } => *state,
        }
    }
}

/// Legal transition check. Kept in the contract crate so the state layer and its
/// tests share one definition of the lifecycle.
pub fn transition(from: TaskState, to: TaskState) -> Result<(), Rejection> {
    let legal = matches!(
        (from, to),
        (
            TaskState::Submitted,
            TaskState::Leased | TaskState::Cancelled | TaskState::Expired
        ) | (
            TaskState::Leased,
            TaskState::Succeeded
                | TaskState::Failed
                | TaskState::CancelRequested
                | TaskState::Submitted
                | TaskState::OutcomeUnknown
                | TaskState::Expired
        ) | (
            TaskState::CancelRequested,
            TaskState::Cancelled
                | TaskState::OutcomeUnknown
                | TaskState::Succeeded
                | TaskState::Failed
        ) | (
            TaskState::OutcomeUnknown,
            TaskState::Succeeded | TaskState::Failed | TaskState::Cancelled
        )
    );
    if legal {
        Ok(())
    } else {
        Err(Rejection::new(
            ErrorCode::Conflict,
            "task state transition is not permitted by the lifecycle",
        ))
    }
}

/// Validate a submission against the declared bounds.
pub fn validate_submission(
    submission: &TaskSubmission,
    limits: &crate::limits::Limits,
) -> Result<(), Rejection> {
    if !submission.task_id.starts_with("t_") || submission.task_id.len() != 34 {
        return Err(Rejection::at(
            ErrorCode::InvalidRequest,
            "taskID is not a server-issued task id",
            "task.taskID",
        ));
    }
    if submission.param_bytes > limits.task.max_param_bytes {
        return Err(Rejection::at(
            ErrorCode::PayloadTooLarge,
            "task parameters exceed the configured maximum",
            "task.params",
        ));
    }
    if !submission.params_digest.starts_with("sha256:") || submission.params_digest.len() != 71 {
        return Err(Rejection::at(
            ErrorCode::InvalidRequest,
            "paramsDigest is not a sha256 digest",
            "task.paramsDigest",
        ));
    }
    Ok(())
}

/// Validate a report's shape only. Whether it may settle the task is a state
/// question answered under the lease row lock.
pub fn validate_report(report: &Report) -> Result<(), Rejection> {
    if report.generation == 0 {
        return Err(Rejection::at(
            ErrorCode::InvalidRequest,
            "a report must name the claim generation it settles",
            "report.generation",
        ));
    }
    if report.worker_id.len() < 8 || report.worker_id.len() > 128 {
        return Err(Rejection::at(
            ErrorCode::InvalidRequest,
            "workerID length is outside the contract range",
            "report.workerID",
        ));
    }
    if let Some(digest) = &report.result_digest
        && (!digest.starts_with("sha256:") || digest.len() != 71)
    {
        return Err(Rejection::at(
            ErrorCode::InvalidRequest,
            "resultDigest is not a sha256 digest",
            "report.resultDigest",
        ));
    }
    Ok(())
}

pub const SCHEMA_VERSION: &str = CONTRACT_VERSION;
