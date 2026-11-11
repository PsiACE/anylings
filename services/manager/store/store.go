// Package store persists domain state in SQLite. One pinned driver
// (modernc.org/sqlite) backs every path; there is no JSON file fallback.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sync"

	_ "modernc.org/sqlite" // pinned SQLite driver, pure Go

	"github.com/PsiACE/anylings/services/manager/clock"
)

// Options configures a store instance.
type Options struct {
	// Path is the SQLite database file. ":memory:" is only used by tests.
	Path string
	// Clock supplies timestamps. Defaults to the system clock.
	Clock clock.Clock
	// MaxOpenConns bounds concurrent writers. SQLite serializes writes, so a
	// single connection keeps transaction ordering predictable.
	MaxOpenConns int
}

// Store is the durable repository. It is safe for concurrent use.
type Store struct {
	db    *sql.DB
	clock clock.Clock
	// writeMu serializes the read-modify-write transactions that enforce
	// state transitions and reservation accounting. SQLite would serialize
	// them anyway; making it explicit keeps the error surface deterministic.
	writeMu sync.Mutex
}

// Open opens the database at opts.Path and applies the schema. Reopening an
// existing file preserves all rows, which is what crash recovery relies on.
func Open(ctx context.Context, opts Options) (*Store, error) {
	if opts.Path == "" {
		return nil, errors.New("store: path is required")
	}
	dsn := opts.Path + "?_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_txlock=immediate"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	if opts.MaxOpenConns > 0 {
		db.SetMaxOpenConns(opts.MaxOpenConns)
	} else {
		db.SetMaxOpenConns(1)
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: apply schema: %w", err)
	}
	c := opts.Clock
	if c == nil {
		c = clock.System{}
	}
	return &Store{db: db, clock: c}, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// now returns the current UTC time.
func (s *Store) now() string { return formatTime(s.clock.Now()) }

// withTx runs fn inside one immediate transaction, rolling back on error.
func (s *Store) withTx(ctx context.Context, fn func(*sql.Tx) error) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: begin: %w", err)
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit: %w", err)
	}
	return nil
}
