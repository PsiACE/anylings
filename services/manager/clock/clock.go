// Package clock supplies the time source used by the store, reconciler and
// providers. Tests use Fake so no assertion depends on sleeping.
package clock

import (
	"sync"
	"time"
)

// Clock reports the current time. All domain timestamps are UTC.
type Clock interface {
	Now() time.Time
}

// System reads the wall clock.
type System struct{}

func (System) Now() time.Time { return time.Now().UTC() }

// Fake is a manually advanced clock for deterministic tests.
type Fake struct {
	mu  sync.Mutex
	now time.Time
}

// NewFake starts a fake clock at the given instant, normalized to UTC.
func NewFake(start time.Time) *Fake {
	return &Fake{now: start.UTC()}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Advance moves the clock forward by d.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}

// Set moves the clock to an absolute instant.
func (f *Fake) Set(t time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = t.UTC()
}
