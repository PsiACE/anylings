// Package provider defines the allocation contract the reconciler depends on.
// A provider allocates and destroys ephemeral sandbox leases; it never touches
// persistent workspace boxes.
package provider

import (
	"context"
	"errors"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
)

// Request describes one allocation.
type Request struct {
	LeaseID     domain.LeaseID
	AttemptID   domain.AttemptID
	TaskRunID   domain.TaskRunID
	WorkspaceID domain.WorkspaceID
	// ResourceID is a stable provider name derived from the lease id, so a
	// retried allocate returns the same resource instead of a second one.
	ResourceID string
	MaxRuntime time.Duration
}

// Allocation is the provider's answer. ResourceID is persisted before the
// lease becomes Active.
type Allocation struct {
	ResourceID string
	// Endpoint is opaque to the domain and never contains credentials.
	Endpoint string
}

var (
	// ErrDisabled means the provider is intentionally switched off. The
	// default deployment runs with every real provider disabled.
	ErrDisabled = errors.New("provider: disabled")
	// ErrCreateTimeout means the provider did not confirm allocation in time
	// but may have created the resource. Callers probe by ResourceID.
	ErrCreateTimeout = errors.New("provider: allocation timed out")
	// ErrUnavailable means allocation failed and no resource exists.
	ErrUnavailable = errors.New("provider: unavailable")
	// ErrDestroyFailed means release was refused or failed; the lease becomes
	// Stranded and must stay visible for the janitor.
	ErrDestroyFailed = errors.New("provider: destroy failed")
	// ErrNotFound means the provider has no such resource; destroy is then
	// already satisfied.
	ErrNotFound = errors.New("provider: resource not found")
)

// Sandbox is the narrow capability set the reconciler uses.
type Sandbox interface {
	// Allocate creates or returns the existing resource for req.ResourceID.
	// It must be idempotent per ResourceID.
	Allocate(ctx context.Context, req Request) (Allocation, error)
	// Destroy releases the resource. Destroying an unknown resource returns
	// ErrNotFound, which callers treat as success.
	Destroy(ctx context.Context, resourceID string) error
	// Probe reports whether a resource exists, used after an ambiguous create.
	Probe(ctx context.Context, resourceID string) (bool, error)
}
