package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/PsiACE/anylings/services/manager/domain"
)

// Limits are the admission ceilings applied inside one transaction. Budgets are
// optional: a nil budget only enforces concurrency, which is the neutral
// default. Amounts are supplied by configuration, never built into the domain.
type Limits struct {
	// MaxConcurrentRuns bounds live runs per workspace.
	MaxConcurrentRuns int
	// MaxGlobalConcurrentRuns bounds live runs across all workspaces.
	MaxGlobalConcurrentRuns int
	// WorkspaceBudget bounds the amount reserved per workspace, if set.
	WorkspaceBudget *domain.Money
	// GlobalBudget bounds the amount reserved in total, if set.
	GlobalBudget *domain.Money
}

// DefaultLimits enforces concurrency only. Budgets stay unset until an operator
// configures them, so the default surface enforces concurrency only.
func DefaultLimits() Limits {
	return Limits{
		MaxConcurrentRuns:       2,
		MaxGlobalConcurrentRuns: 8,
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

		live, err := countLiveRuns(ctx, tx, wsID, string(runID))
		if err != nil {
			return err
		}
		if live >= limits.MaxConcurrentRuns {
			return domain.ErrQuotaExceeded
		}
		global, err := countLiveRuns(ctx, tx, "", "")
		if err != nil {
			return err
		}
		if global >= limits.MaxGlobalConcurrentRuns {
			return domain.ErrQuotaExceeded
		}

		if err := checkBudget(ctx, tx, wsID, amount, limits.WorkspaceBudget); err != nil {
			return err
		}
		if err := checkBudget(ctx, tx, "", amount, limits.GlobalBudget); err != nil {
			return err
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

// checkBudget refuses a hold that would exceed the configured ceiling. An unset
// budget allows any amount.
func checkBudget(ctx context.Context, tx *sql.Tx, ws string, amount domain.Money, limit *domain.Money) error {
	if limit == nil {
		return nil
	}
	if amount.Currency != limit.Currency {
		return domain.ErrCurrencyMismatch
	}
	reserved, err := sumReservations(ctx, tx, ws, amount.Currency)
	if err != nil {
		return err
	}
	total, err := reserved.Add(amount)
	if err != nil {
		return err
	}
	exceeds, err := total.Exceeds(*limit)
	if err != nil {
		return err
	}
	if exceeds {
		return domain.ErrBudgetExceeded
	}
	return nil
}

// countLiveRuns counts other runs that still hold a concurrency slot. The run
// being reserved excludes itself: its own slot is the one it is asking for. An
// empty workspace counts every workspace.
func countLiveRuns(ctx context.Context, tx *sql.Tx, ws, exceptRun string) (int, error) {
	const live = `state NOT IN ('Reclaimed', 'Failed', 'Interrupted')`
	var (
		n   int
		err error
	)
	if ws == "" {
		err = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM task_runs WHERE `+live+` AND id <> ?`, exceptRun).Scan(&n)
	} else {
		err = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM task_runs WHERE workspace_id = ? AND `+live+` AND id <> ?`,
			ws, exceptRun).Scan(&n)
	}
	if err != nil {
		return 0, fmt.Errorf("store: count live runs: %w", err)
	}
	return n, nil
}

// sumReservations totals unreleased holds in one currency. An empty workspace
// totals every workspace.
func sumReservations(ctx context.Context, tx *sql.Tx, ws string, currency domain.Currency) (domain.Money, error) {
	var (
		total int64
		err   error
	)
	if ws == "" {
		err = tx.QueryRowContext(ctx,
			`SELECT COALESCE(SUM(minor), 0) FROM reservations WHERE released = 0 AND currency = ?`,
			string(currency)).Scan(&total)
	} else {
		err = tx.QueryRowContext(ctx,
			`SELECT COALESCE(SUM(minor), 0) FROM reservations WHERE released = 0 AND currency = ? AND workspace_id = ?`,
			string(currency), ws).Scan(&total)
	}
	if err != nil {
		return domain.Money{}, fmt.Errorf("store: sum reservations: %w", err)
	}
	return domain.Amount(total, currency), nil
}

// ReservedTotal reports live reservations for a workspace, or for all
// workspaces when ws is empty. An empty result has the zero minor unit value
// and an empty currency, because no amount was reserved.
func (s *Store) ReservedTotal(ctx context.Context, ws domain.WorkspaceID) (domain.Money, error) {
	var (
		rows *sql.Rows
		err  error
	)
	if ws == "" {
		rows, err = s.db.QueryContext(ctx, `
			SELECT currency, COALESCE(SUM(minor), 0) FROM reservations
			WHERE released = 0 GROUP BY currency`)
	} else {
		rows, err = s.db.QueryContext(ctx, `
			SELECT currency, COALESCE(SUM(minor), 0) FROM reservations
			WHERE released = 0 AND workspace_id = ? GROUP BY currency`, string(ws))
	}
	if err != nil {
		return domain.Money{}, fmt.Errorf("store: reserved total: %w", err)
	}
	defer rows.Close()

	var total domain.Money
	for rows.Next() {
		var (
			currency string
			minor    int64
		)
		if err := rows.Scan(&currency, &minor); err != nil {
			return domain.Money{}, fmt.Errorf("store: reserved total: %w", err)
		}
		if total.Currency == "" {
			total = domain.Amount(minor, domain.Currency(currency))
			continue
		}
		sum, err := total.Add(domain.Amount(minor, domain.Currency(currency)))
		if err != nil {
			return domain.Money{}, err
		}
		total = sum
	}
	return total, rows.Err()
}
