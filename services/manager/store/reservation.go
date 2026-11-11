package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/PsiACE/anylings/services/manager/domain"
)

// Limits are the reservation ceilings applied inside one transaction.
type Limits struct {
	// MaxConcurrentRuns bounds live runs per workspace.
	MaxConcurrentRuns int
	// MaxGlobalConcurrentRuns bounds live runs across all workspaces.
	MaxGlobalConcurrentRuns int
	// WorkspaceMonthlyBudget is the per-workspace planning baseline. It is a
	// reservation target, not a provider billing cap.
	WorkspaceMonthlyBudget domain.Money
	// GlobalMonthlyBudget bounds total reserved spend.
	GlobalMonthlyBudget domain.Money
}

// DefaultLimits returns the fixed baseline. Elastic budget is not approved, so
// spend above the baseline is refused rather than allowed to grow.
func DefaultLimits() Limits {
	return Limits{
		MaxConcurrentRuns:       2,
		MaxGlobalConcurrentRuns: 8,
		WorkspaceMonthlyBudget:  domain.MonthlyBaseline,
		GlobalMonthlyBudget:     domain.USDcents(domain.MonthlyBaseline.Minor * 4),
	}
}

// Reserve atomically checks concurrency and budget and inserts both holds.
// Concurrency is counted from live task runs, budget from unreleased
// reservations; both reads and the insert happen in one immediate transaction,
// so two racing callers cannot both pass the check.
func (s *Store) Reserve(ctx context.Context, runID domain.TaskRunID, amount domain.Money, limits Limits) error {
	if err := amount.Validate(); err != nil {
		return err
	}
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var wsID string
		err := tx.QueryRowContext(ctx, `SELECT workspace_id FROM task_runs WHERE id = ?`, string(runID)).Scan(&wsID)
		if errors.Is(err, sql.ErrNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("store: read run workspace: %w", err)
		}

		live, err := countLiveRuns(ctx, tx, wsID)
		if err != nil {
			return err
		}
		if live >= limits.MaxConcurrentRuns {
			return domain.ErrQuotaExceeded
		}
		global, err := countLiveRuns(ctx, tx, "")
		if err != nil {
			return err
		}
		if global >= limits.MaxGlobalConcurrentRuns {
			return domain.ErrQuotaExceeded
		}

		reserved, err := sumReservations(ctx, tx, wsID)
		if err != nil {
			return err
		}
		total, err := reserved.Add(amount)
		if err != nil {
			return err
		}
		if total.Minor > limits.WorkspaceMonthlyBudget.Minor {
			return domain.ErrBudgetExceeded
		}
		globalReserved, err := sumReservations(ctx, tx, "")
		if err != nil {
			return err
		}
		globalTotal, err := globalReserved.Add(amount)
		if err != nil {
			return err
		}
		if globalTotal.Minor > limits.GlobalMonthlyBudget.Minor {
			return domain.ErrBudgetExceeded
		}

		_, err = tx.ExecContext(ctx, `
			INSERT INTO reservations (id, scope, workspace_id, currency, minor, released, request_id, created_at)
			VALUES (?, 'run', ?, ?, ?, 0, ?, ?)`,
			domain.NewID("res"), wsID, string(amount.Currency), amount.Minor, string(runID), s.now())
		if err != nil {
			return fmt.Errorf("store: insert reservation: %w", mapWriteError(err))
		}
		return nil
	})
}

// countLiveRuns counts runs that still hold a concurrency slot. An empty
// workspace counts every workspace.
func countLiveRuns(ctx context.Context, tx *sql.Tx, ws string) (int, error) {
	const live = `state NOT IN ('Succeeded', 'Failed', 'Interrupted')`
	var (
		n   int
		err error
	)
	if ws == "" {
		err = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM task_runs WHERE `+live).Scan(&n)
	} else {
		err = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM task_runs WHERE workspace_id = ? AND `+live, ws).Scan(&n)
	}
	if err != nil {
		return 0, fmt.Errorf("store: count live runs: %w", err)
	}
	return n, nil
}

func sumReservations(ctx context.Context, tx *sql.Tx, ws string) (domain.Money, error) {
	var (
		total int64
		err   error
	)
	if ws == "" {
		err = tx.QueryRowContext(ctx, `SELECT COALESCE(SUM(minor), 0) FROM reservations WHERE released = 0 AND currency = 'USD'`).Scan(&total)
	} else {
		err = tx.QueryRowContext(ctx, `SELECT COALESCE(SUM(minor), 0) FROM reservations WHERE released = 0 AND currency = 'USD' AND workspace_id = ?`, ws).Scan(&total)
	}
	if err != nil {
		return domain.Money{}, fmt.Errorf("store: sum reservations: %w", err)
	}
	return domain.USDcents(total), nil
}

// ReleaseReservation returns held budget to the pool for one run. It is
// idempotent: releasing twice leaves the row released rather than adding spend.
func (s *Store) ReleaseReservation(ctx context.Context, runID domain.TaskRunID) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE reservations SET released = 1 WHERE request_id = ? AND released = 0`, string(runID))
	if err != nil {
		return fmt.Errorf("store: release reservation: %w", mapWriteError(err))
	}
	return nil
}

// ReservedTotal reports live reservations, used by tests and accounting.
func (s *Store) ReservedTotal(ctx context.Context, ws domain.WorkspaceID) (domain.Money, error) {
	var total int64
	var err error
	if ws == "" {
		err = s.db.QueryRowContext(ctx, `SELECT COALESCE(SUM(minor), 0) FROM reservations WHERE released = 0`).Scan(&total)
	} else {
		err = s.db.QueryRowContext(ctx, `SELECT COALESCE(SUM(minor), 0) FROM reservations WHERE released = 0 AND workspace_id = ?`, string(ws)).Scan(&total)
	}
	if err != nil {
		return domain.Money{}, fmt.Errorf("store: reserved total: %w", err)
	}
	return domain.USDcents(total), nil
}
