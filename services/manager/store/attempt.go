package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
)

// StartAttempt records one bounded attempt and its lease in a single
// transaction, so an attempt never exists without the lease row that the
// janitor would look for.
func (s *Store) StartAttempt(
	ctx context.Context,
	runID domain.TaskRunID,
	number int,
	lease domain.SandboxLease,
) (domain.Attempt, error) {
	var out domain.Attempt
	err := s.withTx(ctx, func(tx *sql.Tx) error {
		now := s.clock.Now().UTC()
		attempt := domain.Attempt{
			ID:          domain.AttemptID(domain.NewID("att")),
			TaskRunID:   runID,
			Number:      number,
			State:       domain.AttemptStarted,
			LeaseID:     lease.ID,
			HeartbeatAt: now,
			StartedAt:   now,
		}
		if lease.ID == "" {
			attempt.LeaseID = domain.LeaseID(domain.NewID("lease"))
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO attempts (id, task_run_id, number, state, lease_id, heartbeat_at, started_at, failure)
			VALUES (?, ?, ?, ?, ?, ?, ?, '')`,
			string(attempt.ID), string(runID), number, string(attempt.State),
			string(attempt.LeaseID), formatTime(now), formatTime(now)); err != nil {
			return fmt.Errorf("store: insert attempt: %w", mapWriteError(err))
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO leases (id, attempt_id, provider, provider_resource_id, state, last_error, created_at)
			VALUES (?, ?, ?, '', ?, '', ?)`,
			string(attempt.LeaseID), string(attempt.ID), lease.Provider,
			string(domain.LeaseAllocating), formatTime(now)); err != nil {
			return fmt.Errorf("store: insert lease: %w", mapWriteError(err))
		}
		out = attempt
		return nil
	})
	return out, err
}

// TransitionAttempt applies one attempt edge.
func (s *Store) TransitionAttempt(ctx context.Context, id domain.AttemptID, to domain.AttemptState, failure string) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var current string
		err := tx.QueryRowContext(ctx, `SELECT state FROM attempts WHERE id = ?`, string(id)).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read attempt state: %w", err)
		}
		if err := domain.AttemptState(current).Transition(to); err != nil {
			return err
		}
		var ended any
		if to.Terminal() {
			ended = s.now()
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE attempts SET state = ?, failure = ?, ended_at = COALESCE(?, ended_at) WHERE id = ?`,
			string(to), failure, ended, string(id))
		if err != nil {
			return fmt.Errorf("store: transition attempt: %w", mapWriteError(err))
		}
		return nil
	})
}

// RecordHeartbeat updates the liveness timestamp of a running attempt. A
// heartbeat on a terminal attempt is an error so a stale writer cannot revive
// it.
func (s *Store) RecordHeartbeat(ctx context.Context, id domain.AttemptID) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE attempts SET heartbeat_at = ?
		WHERE id = ? AND state IN (?, ?)`,
		s.now(), string(id), string(domain.AttemptStarted), string(domain.AttemptRunning))
	if err != nil {
		return fmt.Errorf("store: record heartbeat: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: record heartbeat: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("store: record heartbeat: %w", domain.ErrConflict)
	}
	return nil
}

// GetAttempt loads one attempt.
func (s *Store) GetAttempt(ctx context.Context, id domain.AttemptID) (domain.Attempt, error) {
	return scanAttempt(s.db.QueryRowContext(ctx, `
		SELECT id, task_run_id, number, state, lease_id, heartbeat_at, started_at, ended_at, failure
		FROM attempts WHERE id = ?`, string(id)))
}

func scanAttempt(row rowScanner) (domain.Attempt, error) {
	var (
		a                     domain.Attempt
		id, run, state, lease string
		heartbeat, started    string
		ended                 sql.NullString
	)
	err := row.Scan(&id, &run, &a.Number, &state, &lease, &heartbeat, &started, &ended, &a.Failure)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.Attempt{}, domain.ErrNotFound
	}
	if err != nil {
		return domain.Attempt{}, fmt.Errorf("store: scan attempt: %w", err)
	}
	a.ID = domain.AttemptID(id)
	a.TaskRunID = domain.TaskRunID(run)
	a.State = domain.AttemptState(state)
	a.LeaseID = domain.LeaseID(lease)
	if a.HeartbeatAt, err = scanTime(heartbeat); err != nil {
		return domain.Attempt{}, err
	}
	if a.StartedAt, err = scanTime(started); err != nil {
		return domain.Attempt{}, err
	}
	if a.EndedAt, err = nullTime(ended); err != nil {
		return domain.Attempt{}, err
	}
	return a, nil
}

// ListAttempts returns a run's attempts ordered by number.
func (s *Store) ListAttempts(ctx context.Context, runID domain.TaskRunID) ([]domain.Attempt, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, task_run_id, number, state, lease_id, heartbeat_at, started_at, ended_at, failure
		FROM attempts WHERE task_run_id = ? ORDER BY number`, string(runID))
	if err != nil {
		return nil, fmt.Errorf("store: list attempts: %w", err)
	}
	defer rows.Close()

	var out []domain.Attempt
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// ListStaleAttempts returns attempts in a live state whose heartbeat is older
// than cutoff. This is the crash-recovery query: it works from the persisted
// table, not from in-process memory.
func (s *Store) ListStaleAttempts(ctx context.Context, cutoff time.Time) ([]domain.Attempt, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, task_run_id, number, state, lease_id, heartbeat_at, started_at, ended_at, failure
		FROM attempts
		WHERE state IN (?, ?) AND heartbeat_at < ?
		ORDER BY heartbeat_at`,
		string(domain.AttemptStarted), string(domain.AttemptRunning), formatTime(cutoff))
	if err != nil {
		return nil, fmt.Errorf("store: list stale attempts: %w", err)
	}
	defer rows.Close()

	var out []domain.Attempt
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// AttachLeaseResource records the provider resource id once allocation
// succeeded. Writing it before any further transition is what prevents a
// duplicate allocation after a crash.
func (s *Store) AttachLeaseResource(ctx context.Context, id domain.LeaseID, resourceID string, state domain.LeaseState) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var current string
		err := tx.QueryRowContext(ctx, `SELECT state FROM leases WHERE id = ?`, string(id)).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read lease state: %w", err)
		}
		if err := domain.LeaseState(current).Transition(state); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE leases SET state = ?, provider_resource_id = CASE WHEN ? = '' THEN provider_resource_id ELSE ? END
			WHERE id = ?`, string(state), resourceID, resourceID, string(id))
		if err != nil {
			return fmt.Errorf("store: attach lease resource: %w", mapWriteError(err))
		}
		return nil
	})
}

// TransitionLease applies a lease edge and records the failure text, if any.
func (s *Store) TransitionLease(ctx context.Context, id domain.LeaseID, to domain.LeaseState, lastError string) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var current string
		err := tx.QueryRowContext(ctx, `SELECT state FROM leases WHERE id = ?`, string(id)).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read lease state: %w", err)
		}
		if err := domain.LeaseState(current).Transition(to); err != nil {
			return err
		}
		var released any
		if to == domain.LeaseReleased {
			released = s.now()
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE leases SET state = ?, last_error = ?, released_at = COALESCE(?, released_at) WHERE id = ?`,
			string(to), lastError, released, string(id))
		if err != nil {
			return fmt.Errorf("store: transition lease: %w", mapWriteError(err))
		}
		return nil
	})
}

// GetLease loads one lease.
func (s *Store) GetLease(ctx context.Context, id domain.LeaseID) (domain.SandboxLease, error) {
	return scanLease(s.db.QueryRowContext(ctx, `
		SELECT id, attempt_id, provider, provider_resource_id, state, last_error, created_at, released_at
		FROM leases WHERE id = ?`, string(id)))
}

func scanLease(row rowScanner) (domain.SandboxLease, error) {
	var (
		l              domain.SandboxLease
		id, attempt    string
		state, created string
		released       sql.NullString
	)
	err := row.Scan(&id, &attempt, &l.Provider, &l.ProviderResourceID, &state, &l.LastError, &created, &released)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.SandboxLease{}, domain.ErrNotFound
	}
	if err != nil {
		return domain.SandboxLease{}, fmt.Errorf("store: scan lease: %w", err)
	}
	l.ID = domain.LeaseID(id)
	l.AttemptID = domain.AttemptID(attempt)
	l.State = domain.LeaseState(state)
	if l.CreatedAt, err = scanTime(created); err != nil {
		return domain.SandboxLease{}, err
	}
	if l.ReleasedAt, err = nullTime(released); err != nil {
		return domain.SandboxLease{}, err
	}
	return l, nil
}

// ListLeasesInState finds leases by state, used by stranded-lease cleanup.
func (s *Store) ListLeasesInState(ctx context.Context, state domain.LeaseState) ([]domain.SandboxLease, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, attempt_id, provider, provider_resource_id, state, last_error, created_at, released_at
		FROM leases WHERE state = ? ORDER BY created_at`, string(state))
	if err != nil {
		return nil, fmt.Errorf("store: list leases: %w", err)
	}
	defer rows.Close()

	var out []domain.SandboxLease
	for rows.Next() {
		l, err := scanLease(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, l)
	}
	return out, rows.Err()
}
