package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
)

// RequestTask is the idempotent request to run one command in a workspace.
type RequestTask struct {
	WorkspaceID domain.WorkspaceID
	RequestID   domain.RequestID
	Command     string
	MaxAttempts int
	Deadline    time.Time
}

// RequestTask creates a task run, or returns the existing one when the same
// request id was already used for this workspace. The unique index makes the
// duplicate check atomic; a concurrent duplicate surfaces as ErrConflict with
// the winner's row returned by the follow-up read.
func (s *Store) RequestTask(ctx context.Context, req RequestTask) (domain.TaskRun, bool, error) {
	if req.WorkspaceID.Validate() != nil {
		return domain.TaskRun{}, false, fmt.Errorf("store: request task: %w", domain.ErrInvalidID)
	}
	if req.RequestID == "" || req.Command == "" {
		return domain.TaskRun{}, false, errors.New("store: request id and command are required")
	}
	if req.MaxAttempts <= 0 {
		return domain.TaskRun{}, false, errors.New("store: max attempts must be positive")
	}
	if req.Deadline.IsZero() {
		return domain.TaskRun{}, false, errors.New("store: deadline is required")
	}

	var (
		created bool
		out     domain.TaskRun
	)
	err := s.withTx(ctx, func(tx *sql.Tx) error {
		if err := requireWorkspace(ctx, tx, req.WorkspaceID); err != nil {
			return err
		}
		now := s.clock.Now().UTC()
		run := domain.TaskRun{
			ID:          domain.TaskRunID(domain.NewID("run")),
			WorkspaceID: req.WorkspaceID,
			RequestID:   req.RequestID,
			Command:     req.Command,
			State:       domain.TaskRequested,
			MaxAttempts: req.MaxAttempts,
			Deadline:    req.Deadline.UTC(),
			CreatedAt:   now,
			UpdatedAt:   now,
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO task_runs
				(id, workspace_id, request_id, command, state, attempts, max_attempts, deadline, outcome, created_at, updated_at)
			VALUES (?, ?, ?, ?, ?, 0, ?, ?, '', ?, ?)`,
			string(run.ID), string(run.WorkspaceID), string(run.RequestID), run.Command,
			string(run.State), run.MaxAttempts, formatTime(run.Deadline), formatTime(now), formatTime(now))
		if err == nil {
			created = true
			out = run
			return nil
		}
		if !errors.Is(mapWriteError(err), domain.ErrConflict) {
			return fmt.Errorf("store: insert task run: %w", err)
		}
		// Duplicate request: return the run that already owns this request id.
		found, err := getRunByRequestTx(ctx, tx, req.WorkspaceID, req.RequestID)
		if err != nil {
			return err
		}
		out = found
		return nil
	})
	return out, created, err
}

func requireWorkspace(ctx context.Context, tx *sql.Tx, id domain.WorkspaceID) error {
	var exists int
	err := tx.QueryRowContext(ctx, `SELECT 1 FROM workspaces WHERE id = ?`, string(id)).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("store: check workspace: %w", err)
	}
	return nil
}

const runColumns = `id, workspace_id, request_id, command, state, attempts, max_attempts, deadline, outcome, created_at, updated_at, finished_at`

func scanRun(row rowScanner) (domain.TaskRun, error) {
	var (
		run                       domain.TaskRun
		id, ws, requestID, state  string
		deadline, created, update string
		finished                  sql.NullString
	)
	err := row.Scan(&id, &ws, &requestID, &run.Command, &state, &run.Attempts,
		&run.MaxAttempts, &deadline, &run.Outcome, &created, &update, &finished)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.TaskRun{}, domain.ErrNotFound
	}
	if err != nil {
		return domain.TaskRun{}, fmt.Errorf("store: scan task run: %w", err)
	}
	run.ID = domain.TaskRunID(id)
	run.WorkspaceID = domain.WorkspaceID(ws)
	run.RequestID = domain.RequestID(requestID)
	run.State = domain.TaskState(state)
	if run.Deadline, err = scanTime(deadline); err != nil {
		return domain.TaskRun{}, err
	}
	if run.CreatedAt, err = scanTime(created); err != nil {
		return domain.TaskRun{}, err
	}
	if run.UpdatedAt, err = scanTime(update); err != nil {
		return domain.TaskRun{}, err
	}
	if run.FinishedAt, err = nullTime(finished); err != nil {
		return domain.TaskRun{}, err
	}
	return run, nil
}

func getRunByRequestTx(ctx context.Context, tx *sql.Tx, ws domain.WorkspaceID, request domain.RequestID) (domain.TaskRun, error) {
	row := tx.QueryRowContext(ctx, `SELECT `+runColumns+` FROM task_runs WHERE workspace_id = ? AND request_id = ?`,
		string(ws), string(request))
	return scanRun(row)
}

// GetTaskRun loads one run.
func (s *Store) GetTaskRun(ctx context.Context, id domain.TaskRunID) (domain.TaskRun, error) {
	return scanRun(s.db.QueryRowContext(ctx, `SELECT `+runColumns+` FROM task_runs WHERE id = ?`, string(id)))
}

// ListTaskRunsInState returns runs currently in a state, oldest first.
func (s *Store) ListTaskRunsInState(ctx context.Context, state domain.TaskState) ([]domain.TaskRun, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+runColumns+` FROM task_runs WHERE state = ? ORDER BY created_at`, string(state))
	if err != nil {
		return nil, fmt.Errorf("store: list task runs: %w", err)
	}
	defer rows.Close()

	var out []domain.TaskRun
	for rows.Next() {
		run, err := scanRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, run)
	}
	return out, rows.Err()
}

// TransitionTask applies one task state edge. A terminal target records the
// finish time; the outcome string is stored for the audit story.
func (s *Store) TransitionTask(ctx context.Context, id domain.TaskRunID, to domain.TaskState, outcome string) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var current string
		err := tx.QueryRowContext(ctx, `SELECT state FROM task_runs WHERE id = ?`, string(id)).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read task state: %w", err)
		}
		if err := domain.TaskState(current).Transition(to); err != nil {
			return err
		}
		var finished any
		if to.Terminal() {
			finished = s.now()
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE task_runs SET state = ?, outcome = ?, updated_at = ?, finished_at = COALESCE(?, finished_at)
			WHERE id = ?`, string(to), outcome, s.now(), finished, string(id))
		if err != nil {
			return fmt.Errorf("store: transition task: %w", mapWriteError(err))
		}
		return nil
	})
}

// IncrementAttempts records that a new attempt consumes one of the bounded
// retry budget. It fails once MaxAttempts is reached.
func (s *Store) IncrementAttempts(ctx context.Context, id domain.TaskRunID) (int, error) {
	var attempts int
	err := s.withTx(ctx, func(tx *sql.Tx) error {
		var current, max int
		err := tx.QueryRowContext(ctx, `SELECT attempts, max_attempts FROM task_runs WHERE id = ?`, string(id)).
			Scan(&current, &max)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read attempts: %w", err)
		}
		if current >= max {
			return fmt.Errorf("store: attempt budget exhausted: %w", domain.ErrConflict)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE task_runs SET attempts = attempts + 1, updated_at = ? WHERE id = ?`,
			s.now(), string(id)); err != nil {
			return fmt.Errorf("store: increment attempts: %w", err)
		}
		attempts = current + 1
		return nil
	})
	return attempts, err
}

// SaveArtifact persists the artifact reference and checksum for a run. The
// schema keeps one artifact row per run, so a retry overwrites rather than
// duplicating.
func (s *Store) SaveArtifact(ctx context.Context, runID domain.TaskRunID, ref domain.ArtifactRef) error {
	if err := ref.Validate(); err != nil {
		return err
	}
	if ref.ID == "" {
		ref.ID = domain.ArtifactID(domain.NewID("art"))
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO artifact_refs (task_run_id, artifact_id, uri, checksum, size_bytes, created_at)
		VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT(task_run_id) DO UPDATE SET
			artifact_id = excluded.artifact_id,
			uri = excluded.uri,
			checksum = excluded.checksum,
			size_bytes = excluded.size_bytes,
			created_at = excluded.created_at`,
		string(runID), string(ref.ID), ref.URI, ref.Checksum, ref.SizeBytes, s.now())
	if err != nil {
		return fmt.Errorf("store: save artifact: %w", mapWriteError(err))
	}
	return nil
}

// GetArtifact returns the persisted artifact reference for a run. A missing
// row means the run may not release its lease yet.
func (s *Store) GetArtifact(ctx context.Context, runID domain.TaskRunID) (domain.ArtifactRef, error) {
	var (
		ref               domain.ArtifactRef
		id, uri           string
		checksum, created string
	)
	err := s.db.QueryRowContext(ctx, `
		SELECT artifact_id, uri, checksum, size_bytes, created_at FROM artifact_refs WHERE task_run_id = ?`,
		string(runID)).Scan(&id, &uri, &checksum, &ref.SizeBytes, &created)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.ArtifactRef{}, domain.ErrArtifactMissing
	}
	if err != nil {
		return domain.ArtifactRef{}, fmt.Errorf("store: get artifact: %w", err)
	}
	ref.ID = domain.ArtifactID(id)
	ref.URI = uri
	ref.Checksum = checksum
	if ref.CreatedAt, err = scanTime(created); err != nil {
		return domain.ArtifactRef{}, err
	}
	return ref, nil
}
