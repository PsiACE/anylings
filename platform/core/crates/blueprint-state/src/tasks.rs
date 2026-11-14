//! Task submission, claim, heartbeat, cancel, report and reconciliation.
//!
//! Every state change runs inside a `BEGIN IMMEDIATE` transaction: SQLite takes the
//! write lock before the read of the current row, so two concurrent claimers cannot
//! both observe `submitted` and both be granted a lease. That is the mechanism, not a
//! convention — a deferred transaction would allow both to read and one to fail at
//! commit time only if a constraint happened to catch it.
//!
//! # Unknown outcome
//!
//! Expiry is not authorization to re-run. For `idempotent = false`, an expired lease
//! moves the task to `outcome_unknown`, which is terminal until an explicit reconcile
//! call names the surviving verdict. The worker may already have caused the side
//! effect; reclaiming would duplicate it.

use blueprint_contracts::task::{
    self, Lease, Report, ReportAcceptance, ReportOutcome, TaskKind, TaskState, TaskSubmission,
};
use rusqlite::{OptionalExtension, params};

use crate::Store;
use crate::error::StateError;

/// An artifact a worker uploaded for a task generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ArtifactRecord<'a> {
    pub artifact_id: &'a str,
    pub task_id: &'a str,
    pub generation: u32,
    pub name: &'a str,
    pub digest: &'a str,
    pub bytes: u64,
}

/// The columns read back when settling a report: state, generation, worker, outcome.
type ReportRow = (String, u32, Option<String>, Option<String>, Option<String>);

/// The result of a claim attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClaimOutcome {
    /// A lease was granted at this generation.
    Granted(Lease),
    /// No task was claimable.
    Empty,
    /// The task exists but is not in a claimable state or its declared capacity bound
    /// is already reached.
    NotClaimable { reason: &'static str },
}

/// What a report attempt settled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReportOutcomeRecord {
    Accepted(ReportAcceptance),
    /// The generation or worker does not hold the live lease.
    StaleGeneration,
    /// The task is already settled with a different verdict; the stored one wins.
    AlreadySettled {
        state: TaskState,
    },
}

/// How an expired lease was resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExpiryOutcome {
    /// Idempotent work: the task is claimable again at the next generation.
    ReturnedToQueue { generation: u32 },
    /// Non-idempotent work: the side effect may have happened.
    OutcomeUnknown { generation: u32 },
    /// The lease was still valid when checked.
    StillLeased,
}

impl Store {
    /// Persist a task submission. A repeated submission of the same task id is a
    /// conflict rather than a second task, because the id is server-derived from the
    /// caller's request key.
    pub fn submit_task(
        &mut self,
        submission: &TaskSubmission,
        params: &str,
        now: &str,
    ) -> Result<(), StateError> {
        task::validate_submission(submission, self.limits()).map_err(crate::contract_error)?;
        let open: i64 = self.connection().query_row(
            "SELECT COUNT(*) FROM tasks WHERE service = ?1 AND state IN \
             ('submitted', 'leased', 'cancel_requested', 'outcome_unknown')",
            params![submission.service],
            |row| row.get(0),
        )?;
        if open as u32 >= self.limits().task.max_open_tasks_per_service {
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::QueueFull,
                "service has reached its open-task bound",
            )));
        }
        let inserted = self.connection().execute(
            "INSERT OR IGNORE INTO tasks (task_id, service, kind, idempotent, state, \
             submitted_by, submitted_at, params, params_digest) \
             VALUES (?1, ?2, ?3, ?4, 'submitted', ?5, ?6, ?7, ?8)",
            params![
                submission.task_id,
                submission.service,
                kind_str(submission.kind),
                submission.idempotent as i64,
                submission.submitted_by,
                now,
                params,
                submission.params_digest,
            ],
        )?;
        if inserted == 0 {
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::Conflict,
                "a task with this id already exists",
            )));
        }
        Ok(())
    }

    /// Claim the oldest claimable task for `service`.
    ///
    /// The whole decision is one immediate transaction, so the read that chooses a
    /// candidate and the update that grants the lease cannot interleave with another
    /// claimer's.
    pub fn claim_next(
        &mut self,
        service: &str,
        worker_id: &str,
        now: &str,
        expires_at: &str,
    ) -> Result<ClaimOutcome, StateError> {
        let tx = self
            .connection_mut()
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let candidate: Option<(String, u32)> = tx
            .query_row(
                "SELECT task_id, generation FROM tasks \
                 WHERE service = ?1 AND state = 'submitted' \
                 ORDER BY submitted_at LIMIT 1",
                params![service],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((task_id, generation)) = candidate else {
            tx.commit()?;
            return Ok(ClaimOutcome::Empty);
        };
        let next_generation = generation + 1;
        // The state predicate is repeated in the UPDATE so the row cannot have moved
        // between the read and the write even if a future caller drops the immediate
        // behavior.
        let updated = tx.execute(
            "UPDATE tasks SET state = 'leased', generation = ?3, worker_id = ?4, \
             lease_deadline = ?5, heartbeat_at = ?6 \
             WHERE task_id = ?1 AND generation = ?2 AND state = 'submitted'",
            params![
                task_id,
                generation,
                next_generation,
                worker_id,
                expires_at,
                now
            ],
        )?;
        if updated != 1 {
            tx.rollback()?;
            return Ok(ClaimOutcome::NotClaimable {
                reason: "task state changed while claiming",
            });
        }
        tx.execute(
            "INSERT INTO task_leases (task_id, generation, worker_id, granted_at, expires_at) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![task_id, next_generation, worker_id, now, expires_at],
        )?;
        tx.commit()?;
        Ok(ClaimOutcome::Granted(Lease {
            task_id,
            generation: next_generation,
            worker_id: worker_id.to_owned(),
            granted_at: now.to_owned(),
            expires_at: expires_at.to_owned(),
        }))
    }

    /// Extend the live lease. A heartbeat from a stale generation is refused, so a
    /// worker that was superseded cannot keep a lease alive against the new holder.
    pub fn heartbeat(
        &mut self,
        task_id: &str,
        generation: u32,
        worker_id: &str,
        now: &str,
        next_expires_at: &str,
    ) -> Result<bool, StateError> {
        let tx = self
            .connection_mut()
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<(u32, String, String)> = tx
            .query_row(
                "SELECT generation, worker_id, lease_deadline FROM tasks WHERE task_id = ?1",
                params![task_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((current_generation, current_worker, deadline)) = row else {
            tx.rollback()?;
            return Ok(false);
        };
        if current_generation != generation
            || current_worker != worker_id
            || deadline.as_str() <= now
        {
            tx.rollback()?;
            return Ok(false);
        }
        tx.execute(
            "UPDATE tasks SET lease_deadline = ?2, heartbeat_at = ?3 WHERE task_id = ?1",
            params![task_id, next_expires_at, now],
        )?;
        tx.execute(
            "UPDATE task_leases SET expires_at = ?3 \
             WHERE task_id = ?1 AND generation = ?2 AND released_at IS NULL",
            params![task_id, generation, next_expires_at],
        )?;
        tx.commit()?;
        Ok(true)
    }

    /// Record a cancel request. A submitted task becomes cancelled immediately; a
    /// leased task becomes `cancel_requested` and stays there until the worker reports
    /// or the lease expires, so the side effect is not abandoned silently.
    pub fn cancel_task(
        &mut self,
        task_id: &str,
        principal: &str,
        now: &str,
    ) -> Result<TaskState, StateError> {
        let tx = self
            .connection_mut()
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<(String, String)> = tx
            .query_row(
                "SELECT state, submitted_by FROM tasks WHERE task_id = ?1",
                params![task_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((state, submitted_by)) = row else {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::NotFound,
                "task does not exist",
            )));
        };
        if submitted_by != principal {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::ForbiddenScope,
                "only the submitting principal may cancel this task",
            )));
        }
        let from = parse_task_state(&state)?;
        if from.is_terminal() {
            tx.commit()?;
            return Ok(from);
        }
        let to = match from {
            TaskState::Submitted => TaskState::Cancelled,
            _ => TaskState::CancelRequested,
        };
        task::transition(from, to).map_err(crate::contract_error)?;
        tx.execute(
            "UPDATE tasks SET state = ?2, settled_at = CASE WHEN ?2 = 'cancelled' \
             THEN ?3 ELSE settled_at END WHERE task_id = ?1",
            params![task_id, to.as_str(), now],
        )?;
        if to == TaskState::Cancelled {
            tx.execute(
                "UPDATE task_leases SET released_at = ?2, release_reason = 'cancelled' \
                 WHERE task_id = ?1 AND released_at IS NULL",
                params![task_id, now],
            )?;
        }
        tx.commit()?;
        Ok(to)
    }

    /// Accept a worker report.
    ///
    /// A duplicate report — same generation, same worker, same outcome — returns the
    /// stored verdict and writes nothing. A report from a superseded generation is
    /// refused, which is what stops a worker whose lease expired from settling a task
    /// someone else now owns.
    pub fn report_task(
        &mut self,
        report: &Report,
        now: &str,
    ) -> Result<ReportOutcomeRecord, StateError> {
        task::validate_report(report).map_err(crate::contract_error)?;
        let tx = self
            .connection_mut()
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<ReportRow> = tx
            .query_row(
                "SELECT state, generation, worker_id, outcome, result_digest FROM tasks \
                 WHERE task_id = ?1",
                params![report.task_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .optional()?;
        let Some((state, generation, worker_id, outcome, _result_digest)) = row else {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::NotFound,
                "task does not exist",
            )));
        };
        let from = parse_task_state(&state)?;

        // Duplicate first: a converging worker retrying its own accepted report gets
        // the same answer instead of a conflict it cannot resolve.
        if from.is_terminal() {
            let matches = generation == report.generation
                && worker_id.as_deref() == Some(report.worker_id.as_str())
                && outcome.as_deref() == Some(report.outcome.as_str());
            tx.commit()?;
            return Ok(if matches {
                ReportOutcomeRecord::Accepted(ReportAcceptance::Duplicate {
                    state: from,
                    generation,
                })
            } else {
                ReportOutcomeRecord::AlreadySettled { state: from }
            });
        }
        if from == TaskState::Submitted || generation != report.generation {
            tx.rollback()?;
            return Ok(ReportOutcomeRecord::StaleGeneration);
        }
        if worker_id.as_deref() != Some(report.worker_id.as_str()) {
            tx.rollback()?;
            return Ok(ReportOutcomeRecord::StaleGeneration);
        }
        let to = report.outcome.task_state();
        task::transition(from, to).map_err(crate::contract_error)?;

        // A result digest must refer to an artifact the core already recorded. Naming
        // a digest that was never uploaded is a failed reconciliation, not an invented
        // result.
        if let Some(digest) = &report.result_digest {
            let known: i64 = tx.query_row(
                "SELECT COUNT(*) FROM task_artifacts WHERE task_id = ?1 AND digest = ?2",
                params![report.task_id, digest],
                |row| row.get(0),
            )?;
            if known == 0 {
                tx.rollback()?;
                return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                    blueprint_contracts::error::ErrorCode::InvalidRequest,
                    "reported result digest has no recorded artifact",
                )));
            }
        }

        tx.execute(
            "UPDATE tasks SET state = ?2, outcome = ?3, result_digest = ?4, settled_at = ?5 \
             WHERE task_id = ?1",
            params![
                report.task_id,
                to.as_str(),
                report.outcome.as_str(),
                report.result_digest,
                now,
            ],
        )?;
        tx.execute(
            "UPDATE task_leases SET released_at = ?3, release_reason = 'reported' \
             WHERE task_id = ?1 AND generation = ?2 AND released_at IS NULL",
            params![report.task_id, report.generation, now],
        )?;
        tx.commit()?;
        Ok(ReportOutcomeRecord::Accepted(ReportAcceptance::Accepted {
            state: to,
            generation: report.generation,
        }))
    }

    /// Resolve an expired lease.
    ///
    /// This is the one place where "the lease ran out" is turned into a new state. It
    /// never silently re-runs non-idempotent work.
    pub fn expire_lease(&mut self, task_id: &str, now: &str) -> Result<ExpiryOutcome, StateError> {
        let tx = self
            .connection_mut()
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<(String, u32, i64, Option<String>)> = tx
            .query_row(
                "SELECT state, generation, idempotent, lease_deadline FROM tasks \
                 WHERE task_id = ?1",
                params![task_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()?;
        let Some((state, generation, idempotent, deadline)) = row else {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::NotFound,
                "task does not exist",
            )));
        };
        let from = parse_task_state(&state)?;
        let Some(deadline) = deadline else {
            tx.commit()?;
            return Ok(ExpiryOutcome::StillLeased);
        };
        if deadline.as_str() > now {
            tx.commit()?;
            return Ok(ExpiryOutcome::StillLeased);
        }
        let to = if idempotent == 1 {
            TaskState::Submitted
        } else {
            TaskState::OutcomeUnknown
        };
        task::transition(from, to).map_err(crate::contract_error)?;
        tx.execute(
            "UPDATE tasks SET state = ?2, worker_id = NULL, lease_deadline = NULL, \
             heartbeat_at = NULL WHERE task_id = ?1",
            params![task_id, to.as_str()],
        )?;
        tx.execute(
            "UPDATE task_leases SET released_at = ?3, release_reason = 'expired' \
             WHERE task_id = ?1 AND generation = ?2 AND released_at IS NULL",
            params![task_id, generation, now],
        )?;
        if to == TaskState::OutcomeUnknown {
            tx.execute(
                "UPDATE tasks SET settled_at = NULL WHERE task_id = ?1",
                params![task_id],
            )?;
        }
        tx.commit()?;
        Ok(if to == TaskState::Submitted {
            ExpiryOutcome::ReturnedToQueue { generation }
        } else {
            ExpiryOutcome::OutcomeUnknown { generation }
        })
    }

    /// Settle an `outcome_unknown` task after someone established what really happened.
    ///
    /// The principal must be the submitter. This is deliberately an explicit call and
    /// never a timeout: only an operator or a reconciler that checked the side effect
    /// may claim it.
    pub fn reconcile_unknown(
        &mut self,
        task_id: &str,
        principal: &str,
        outcome: ReportOutcome,
        now: &str,
    ) -> Result<TaskState, StateError> {
        let tx = self
            .connection_mut()
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<(String, String)> = tx
            .query_row(
                "SELECT state, submitted_by FROM tasks WHERE task_id = ?1",
                params![task_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((state, submitted_by)) = row else {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::NotFound,
                "task does not exist",
            )));
        };
        if submitted_by != principal {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::ForbiddenScope,
                "only the submitting principal may reconcile this task",
            )));
        }
        let from = parse_task_state(&state)?;
        if from != TaskState::OutcomeUnknown {
            tx.rollback()?;
            return Err(crate::contract_error(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::Conflict,
                "only an unknown outcome requires reconciliation",
            )));
        }
        let to = outcome.task_state();
        task::transition(from, to).map_err(crate::contract_error)?;
        tx.execute(
            "UPDATE tasks SET state = ?2, outcome = ?3, settled_at = ?4 WHERE task_id = ?1",
            params![task_id, to.as_str(), outcome.as_str(), now],
        )?;
        tx.commit()?;
        Ok(to)
    }

    /// Record an uploaded artifact. Artifacts are independent of the report, so an
    /// object that arrives without a report can still be reconciled later.
    pub fn record_artifact(
        &mut self,
        artifact: &ArtifactRecord<'_>,
        now: &str,
    ) -> Result<bool, StateError> {
        let inserted = self.connection().execute(
            "INSERT OR IGNORE INTO task_artifacts \
             (artifact_id, task_id, generation, name, digest, bytes, uploaded_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                artifact.artifact_id,
                artifact.task_id,
                artifact.generation,
                artifact.name,
                artifact.digest,
                artifact.bytes as i64,
                now,
            ],
        )?;
        Ok(inserted == 1)
    }

    pub fn task_state(&self, task_id: &str) -> Result<Option<TaskState>, StateError> {
        let raw: Option<String> = self
            .connection()
            .query_row(
                "SELECT state FROM tasks WHERE task_id = ?1",
                params![task_id],
                |row| row.get(0),
            )
            .optional()?;
        raw.map(|value| parse_task_state(&value)).transpose()
    }

    pub fn task_generation(&self, task_id: &str) -> Result<Option<u32>, StateError> {
        let raw: Option<i64> = self
            .connection()
            .query_row(
                "SELECT generation FROM tasks WHERE task_id = ?1",
                params![task_id],
                |row| row.get(0),
            )
            .optional()?;
        Ok(raw.map(|value| value as u32))
    }

    pub fn lease_count(&self, task_id: &str) -> Result<i64, StateError> {
        let count = self.connection().query_row(
            "SELECT COUNT(*) FROM task_leases WHERE task_id = ?1",
            params![task_id],
            |row| row.get(0),
        )?;
        Ok(count)
    }
}

fn kind_str(kind: TaskKind) -> &'static str {
    match kind {
        TaskKind::Probe => "probe",
        TaskKind::Query => "query",
        TaskKind::Archive => "archive",
        TaskKind::Verify => "verify",
    }
}

fn parse_task_state(raw: &str) -> Result<TaskState, StateError> {
    match raw {
        "submitted" => Ok(TaskState::Submitted),
        "leased" => Ok(TaskState::Leased),
        "cancel_requested" => Ok(TaskState::CancelRequested),
        "outcome_unknown" => Ok(TaskState::OutcomeUnknown),
        "succeeded" => Ok(TaskState::Succeeded),
        "failed" => Ok(TaskState::Failed),
        "cancelled" => Ok(TaskState::Cancelled),
        "expired" => Ok(TaskState::Expired),
        other => Err(StateError::Corrupt(format!("unknown task state {other}"))),
    }
}
