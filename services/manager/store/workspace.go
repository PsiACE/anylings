package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/PsiACE/anylings/services/manager/domain"
)

// CreateWorkspace inserts a workspace whose box starts out Requested.
func (s *Store) CreateWorkspace(ctx context.Context, name, provider string) (domain.Workspace, error) {
	if name == "" || provider == "" {
		return domain.Workspace{}, errors.New("store: workspace name and provider are required")
	}
	now := s.now()
	ws := domain.Workspace{
		ID:        domain.WorkspaceID(domain.NewID("ws")),
		Name:      name,
		Provider:  provider,
		BoxState:  domain.ResourceRequested,
		CreatedAt: s.clock.Now().UTC(),
		UpdatedAt: s.clock.Now().UTC(),
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO workspaces (id, name, provider, provider_resource_id, box_state, last_error, created_at, updated_at)
		VALUES (?, ?, ?, '', ?, '', ?, ?)`,
		string(ws.ID), ws.Name, ws.Provider, string(ws.BoxState), now, now)
	if err != nil {
		return domain.Workspace{}, fmt.Errorf("store: create workspace: %w", mapWriteError(err))
	}
	return ws, nil
}

// GetWorkspace loads one workspace.
func (s *Store) GetWorkspace(ctx context.Context, id domain.WorkspaceID) (domain.Workspace, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT id, name, provider, provider_resource_id, box_state, last_error, created_at, updated_at
		FROM workspaces WHERE id = ?`, string(id))
	return scanWorkspace(row)
}

type rowScanner interface{ Scan(dest ...any) error }

func scanWorkspace(row rowScanner) (domain.Workspace, error) {
	var (
		ws              domain.Workspace
		id, state       string
		created, update string
	)
	err := row.Scan(&id, &ws.Name, &ws.Provider, &ws.ProviderResourceID, &state, &ws.LastError, &created, &update)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.Workspace{}, domain.ErrNotFound
	}
	if err != nil {
		return domain.Workspace{}, fmt.Errorf("store: scan workspace: %w", err)
	}
	ws.ID = domain.WorkspaceID(id)
	ws.BoxState = domain.ResourceLifecycle(state)
	if ws.CreatedAt, err = scanTime(created); err != nil {
		return domain.Workspace{}, err
	}
	if ws.UpdatedAt, err = scanTime(update); err != nil {
		return domain.Workspace{}, err
	}
	return ws, nil
}

// TransitionBox moves the persistent box to a new lifecycle state. The edge is
// validated against the single state machine before the row is written.
//
// A provider resource id may be recorded on the same edge (done by the
// reconciler when allocation succeeds) so a crash between provider allocation
// and record cannot lose the id: callers must record it in the same statement.
func (s *Store) TransitionBox(
	ctx context.Context,
	id domain.WorkspaceID,
	to domain.ResourceLifecycle,
	providerResourceID string,
	lastError string,
) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var current string
		err := tx.QueryRowContext(ctx, `SELECT box_state FROM workspaces WHERE id = ?`, string(id)).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read box state: %w", err)
		}
		if err := domain.ResourceLifecycle(current).Transition(to); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE workspaces
			SET box_state = ?, provider_resource_id = CASE WHEN ? = '' THEN provider_resource_id ELSE ? END,
			    last_error = ?, updated_at = ?
			WHERE id = ?`,
			string(to), providerResourceID, providerResourceID, lastError, s.now(), string(id))
		if err != nil {
			return fmt.Errorf("store: transition box: %w", mapWriteError(err))
		}
		return nil
	})
}

// ReclaimBox moves a persistent box to Reclaimed in one transaction, refusing
// while any run or lease of the workspace could still use it.
//
// The guard covers the whole workspace, not just one state: a run in any
// non-terminal state, or any lease that is not released (including a stranded
// one), blocks reclamation. Because the check and the update share a
// transaction, a run requested concurrently cannot slip between them.
func (s *Store) ReclaimBox(ctx context.Context, id domain.WorkspaceID) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var current string
		err := tx.QueryRowContext(ctx, `SELECT box_state FROM workspaces WHERE id = ?`, string(id)).Scan(&current)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read box state: %w", err)
		}
		if domain.ResourceLifecycle(current) == domain.ResourceReclaimed {
			return nil
		}
		if err := requireBoxIdle(ctx, tx, id); err != nil {
			return err
		}
		if err := domain.ResourceLifecycle(current).Transition(domain.ResourceReclaimed); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE workspaces SET box_state = ?, updated_at = ? WHERE id = ?`,
			string(domain.ResourceReclaimed), s.now(), string(id)); err != nil {
			return fmt.Errorf("store: reclaim box: %w", mapWriteError(err))
		}
		return nil
	})
}

// requireBoxIdle refuses when anything in the workspace may still hold the box.
func requireBoxIdle(ctx context.Context, tx *sql.Tx, id domain.WorkspaceID) error {
	var active int
	err := tx.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM task_runs
		WHERE workspace_id = ? AND state NOT IN (?, ?, ?)`,
		string(id), string(domain.TaskReclaimed), string(domain.TaskFailed), string(domain.TaskInterrupted)).
		Scan(&active)
	if err != nil {
		return fmt.Errorf("store: count active runs: %w", err)
	}
	if active > 0 {
		return fmt.Errorf("store: workspace %s still has %d live task run(s): %w", id, active, domain.ErrConflict)
	}
	var unreleased int
	err = tx.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM leases l
		JOIN attempts a ON a.id = l.attempt_id
		JOIN task_runs r ON r.id = a.task_run_id
		WHERE r.workspace_id = ? AND l.state <> ?`,
		string(id), string(domain.LeaseReleased)).Scan(&unreleased)
	if err != nil {
		return fmt.Errorf("store: count unreleased leases: %w", err)
	}
	if unreleased > 0 {
		return fmt.Errorf("store: workspace %s still has %d unreleased lease(s): %w", id, unreleased, domain.ErrConflict)
	}
	return nil
}

// ListWorkspacesInBoxState returns workspaces whose box is in the given state.
func (s *Store) ListWorkspacesInBoxState(ctx context.Context, state domain.ResourceLifecycle) ([]domain.Workspace, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, name, provider, provider_resource_id, box_state, last_error, created_at, updated_at
		FROM workspaces WHERE box_state = ? ORDER BY created_at`, string(state))
	if err != nil {
		return nil, fmt.Errorf("store: list workspaces: %w", err)
	}
	defer rows.Close()

	var out []domain.Workspace
	for rows.Next() {
		ws, err := scanWorkspace(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, ws)
	}
	return out, rows.Err()
}
