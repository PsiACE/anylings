// Package audit records who did what to which target. The table is append-only
// through the API; SQLite itself is not tamper-proof, so this is integrity of
// process, not cryptographic proof.
package audit

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/PsiACE/anylings/services/manager/clock"
)

// Outcome classifies an attempt to act.
type Outcome string

const (
	Allowed Outcome = "allowed"
	Denied  Outcome = "denied"
	Failed  Outcome = "failed"
	Cleaned Outcome = "cleaned"
)

// Event is one audit record. Detail must never hold secrets or prompt text.
type Event struct {
	Actor         string
	Action        string
	Target        string
	CorrelationID string
	Outcome       Outcome
	Detail        string
	At            time.Time
}

// Log appends events. There is no update or delete method by design.
type Log struct {
	db    *sql.DB
	clock clock.Clock
}

// NewLog wraps an existing database handle.
func NewLog(db *sql.DB, c clock.Clock) *Log {
	if c == nil {
		c = clock.System{}
	}
	return &Log{db: db, clock: c}
}

// Append writes one event.
func (l *Log) Append(ctx context.Context, e Event) error {
	if e.Actor == "" || e.Action == "" || e.Target == "" || e.CorrelationID == "" {
		return fmt.Errorf("audit: actor, action, target and correlation id are required")
	}
	at := e.At.UTC()
	if at.IsZero() {
		at = l.clock.Now()
	}
	_, err := l.db.ExecContext(ctx, `
		INSERT INTO audit_events (actor, action, target, correlation_id, outcome, detail, at)
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		e.Actor, e.Action, e.Target, e.CorrelationID, string(e.Outcome), e.Detail, at.Format(time.RFC3339Nano))
	if err != nil {
		return fmt.Errorf("audit: append: %w", err)
	}
	return nil
}

// List returns events for a target in insertion order.
func (l *Log) List(ctx context.Context, target string) ([]Event, error) {
	rows, err := l.db.QueryContext(ctx, `
		SELECT actor, action, target, correlation_id, outcome, detail, at
		FROM audit_events WHERE target = ? ORDER BY seq`, target)
	if err != nil {
		return nil, fmt.Errorf("audit: list: %w", err)
	}
	defer rows.Close()

	var out []Event
	for rows.Next() {
		var (
			e    Event
			at   string
			outc string
		)
		if err := rows.Scan(&e.Actor, &e.Action, &e.Target, &e.CorrelationID, &outc, &e.Detail, &at); err != nil {
			return nil, fmt.Errorf("audit: scan: %w", err)
		}
		e.Outcome = Outcome(outc)
		if e.At, err = time.Parse(time.RFC3339Nano, at); err != nil {
			return nil, fmt.Errorf("audit: parse time: %w", err)
		}
		e.At = e.At.UTC()
		out = append(out, e)
	}
	return out, rows.Err()
}
