package domain

import (
	"errors"
	"fmt"
	"time"
)

// Workspace is the persistent tenant boundary. Its box outlives individual
// task runs.
type Workspace struct {
	ID                 WorkspaceID
	Name               string
	Region             string
	CreatedAt          time.Time
	UpdatedAt          time.Time
	BoxID              BoxID
	BoxState           ResourceLifecycle
	Provider           string
	ProviderResourceID string
	LastError          string
}

// TaskRun is one unit of work inside a workspace. RequestID makes run creation
// idempotent: the same request for the same workspace returns the existing run.
type TaskRun struct {
	ID          TaskRunID
	WorkspaceID WorkspaceID
	RequestID   RequestID
	Command     string
	State       TaskState
	Attempts    int
	MaxAttempts int
	Deadline    time.Time
	Artifact    *ArtifactRef
	Outcome     string
	CreatedAt   time.Time
	UpdatedAt   time.Time
	FinishedAt  *time.Time
}

// Attempt is one bounded try at a task run.
type Attempt struct {
	ID          AttemptID
	TaskRunID   TaskRunID
	Number      int
	State       AttemptState
	LeaseID     LeaseID
	HeartbeatAt time.Time
	StartedAt   time.Time
	EndedAt     *time.Time
	Failure     string
}

// SandboxLease is an ephemeral provider allocation owned by an attempt. It is
// never the workspace box: a lease can be released while the box stays Ready.
type SandboxLease struct {
	ID                 LeaseID
	AttemptID          AttemptID
	Provider           string
	ProviderResourceID string
	State              LeaseState
	CreatedAt          time.Time
	ReleasedAt         *time.Time
	LastError          string
}

// ArtifactRef points at collected task output. The reference and checksum are
// persisted before the run may delete its lease.
type ArtifactRef struct {
	ID        ArtifactID
	URI       string
	Checksum  string
	SizeBytes int64
	CreatedAt time.Time
}

func (a ArtifactRef) Validate() error {
	if a.URI == "" || a.Checksum == "" {
		return errors.New("domain: artifact requires uri and checksum")
	}
	if a.SizeBytes < 0 {
		return errors.New("domain: artifact size must not be negative")
	}
	return nil
}

// Reservation is a transactional budget or quota hold. Holds are released when
// the run terminates so the accounting is reversible.
type Reservation struct {
	ID          string
	Scope       string
	WorkspaceID WorkspaceID
	Amount      Money
	Released    bool
	RequestID   string
	CreatedAt   time.Time
}

// Errors surfaced by the store and reconciler to callers.
var (
	ErrNotFound         = errors.New("domain: not found")
	ErrConflict         = errors.New("domain: conflict")
	ErrQuotaExceeded    = errors.New("domain: workspace quota exceeded")
	ErrBudgetExceeded   = errors.New("domain: budget exhausted")
	ErrProviderDisabled = errors.New("domain: provider disabled")
	ErrArtifactMissing  = errors.New("domain: artifact reference not persisted")
	ErrCancelled        = errors.New("domain: task run cancelled")
	ErrHeartbeatExpired = errors.New("domain: heartbeat expired")
)

// LeaseResourceID is the deterministic provider name for a lease. Deriving it
// from the lease id is what makes allocation idempotent and lets recovery probe
// or destroy the right resource even if it never stored the provider's answer.
func LeaseResourceID(id LeaseID) string { return "sbx-" + string(id) }

// ParseUTC normalizes a timestamp to UTC. All persisted times are UTC.
func ParseUTC(t time.Time) time.Time { return t.UTC() }

// FormatUTC renders a timestamp for persistence.
func FormatUTC(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func parseUTC(s string) (time.Time, error) {
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}, fmt.Errorf("domain: parse timestamp %q: %w", s, err)
	}
	return t.UTC(), nil
}
