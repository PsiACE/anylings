// Package fake provides an in-memory sandbox provider and command runner.
// Nothing here performs I/O outside the process: no network, no cluster, no
// cloud API and no real spend.
package fake

import (
	"context"
	"sync"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
	"github.com/PsiACE/anylings/services/manager/provider"
)

// Sandbox is a deterministic provider.
//
// Failure injection is explicit and one-shot where it matters: a create
// timeout still records the resource (the real world may have created it), so
// the reconciler must probe rather than allocate again.
type Sandbox struct {
	mu       sync.Mutex
	Disabled bool
	resource map[string]provider.Allocation
	creates  map[string]int
	destroys map[string]int
	// CreateTimeoutFor makes the first Allocate for a resource id return
	// ErrCreateTimeout after recording it as existing.
	CreateTimeoutFor map[string]bool
	// DestroyFailFor makes Destroy return ErrDestroyFailed for a resource id.
	DestroyFailFor map[string]bool
	// AllocationDelay is honoured through the caller's clock, never a sleep.
	AllocationDelay time.Duration
}

// NewSandbox returns an empty provider.
func NewSandbox() *Sandbox {
	return &Sandbox{
		resource:         map[string]provider.Allocation{},
		creates:          map[string]int{},
		destroys:         map[string]int{},
		CreateTimeoutFor: map[string]bool{},
		DestroyFailFor:   map[string]bool{},
	}
}

func (s *Sandbox) Allocate(ctx context.Context, req provider.Request) (provider.Allocation, error) {
	if err := ctx.Err(); err != nil {
		return provider.Allocation{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Disabled {
		return provider.Allocation{}, provider.ErrDisabled
	}
	if existing, ok := s.resource[req.ResourceID]; ok {
		s.creates[req.ResourceID]++
		return existing, nil
	}
	if s.CreateTimeoutFor[req.ResourceID] {
		delete(s.CreateTimeoutFor, req.ResourceID)
		// Record the resource: a timeout is ambiguous, not "nothing happened".
		s.resource[req.ResourceID] = provider.Allocation{ResourceID: req.ResourceID, Endpoint: "fake://" + req.ResourceID}
		s.creates[req.ResourceID]++
		return provider.Allocation{}, provider.ErrCreateTimeout
	}
	alloc := provider.Allocation{ResourceID: req.ResourceID, Endpoint: "fake://" + req.ResourceID}
	s.resource[req.ResourceID] = alloc
	s.creates[req.ResourceID]++
	return alloc, nil
}

func (s *Sandbox) Destroy(ctx context.Context, resourceID string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Disabled {
		return provider.ErrDisabled
	}
	if s.DestroyFailFor[resourceID] {
		delete(s.DestroyFailFor, resourceID)
		s.destroys[resourceID]++
		return provider.ErrDestroyFailed
	}
	s.destroys[resourceID]++
	if _, ok := s.resource[resourceID]; !ok {
		return provider.ErrNotFound
	}
	delete(s.resource, resourceID)
	return nil
}

func (s *Sandbox) Probe(ctx context.Context, resourceID string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.resource[resourceID]
	return ok, nil
}

// CreateCount reports how many Allocate calls a resource id saw. The
// reconciler's duplicate-prevention test asserts this stays at one.
func (s *Sandbox) CreateCount(resourceID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.creates[resourceID]
}

// DestroyCount reports how many Destroy calls a resource id saw.
func (s *Sandbox) DestroyCount(resourceID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.destroys[resourceID]
}

// Live reports how many resources are still allocated.
func (s *Sandbox) Live() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.resource)
}

// Runner executes the task command. It is a fake so tests never spawn a
// process; a real runner arrives with the provider adapter.
type Runner interface {
	Run(ctx context.Context, req RunRequest) (RunResult, error)
}

// RunRequest is one command execution inside a lease.
type RunRequest struct {
	TaskRunID  domain.TaskRunID
	LeaseID    domain.LeaseID
	ResourceID string
	Command    string
}

// RunResult is the observable outcome of a fake execution.
type RunResult struct {
	ExitCode int
	Artifact domain.ArtifactRef
	TimedOut bool
	Error    string
}

// CommandRunner returns scripted results keyed by task run id.
type CommandRunner struct {
	mu      sync.Mutex
	results map[domain.TaskRunID][]RunResult
}

// NewCommandRunner returns a runner with no scripted results; every run
// succeeds with an empty artifact until scripted otherwise.
func NewCommandRunner() *CommandRunner {
	return &CommandRunner{results: map[domain.TaskRunID][]RunResult{}}
}

// Script queues the successive results for a task run.
func (r *CommandRunner) Script(id domain.TaskRunID, results ...RunResult) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.results[id] = append(r.results[id], results...)
}

func (r *CommandRunner) Run(ctx context.Context, req RunRequest) (RunResult, error) {
	if err := ctx.Err(); err != nil {
		return RunResult{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	queue := r.results[req.TaskRunID]
	if len(queue) == 0 {
		return RunResult{ExitCode: 0}, nil
	}
	next := queue[0]
	r.results[req.TaskRunID] = queue[1:]
	return next, nil
}
