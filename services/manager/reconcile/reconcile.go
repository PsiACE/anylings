// Package reconcile advances persisted task runs through their lifecycle.
//
// The reconciler is the only place that decides a state edge. It reads state
// from the store, never from memory, so a restart resumes from the same
// durable snapshot a crash left behind. Every step is idempotent: a step that
// already happened is a no-op rather than an error.
package reconcile

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/PsiACE/anylings/services/manager/clock"
	"github.com/PsiACE/anylings/services/manager/domain"
	"github.com/PsiACE/anylings/services/manager/provider"
	"github.com/PsiACE/anylings/services/manager/provider/fake"
	"github.com/PsiACE/anylings/services/manager/store"
)

// Config is the tunable policy. Zero values fall back to defaults so callers
// cannot accidentally run with an unbounded retry count.
type Config struct {
	MaxAttempts          int
	TaskMaxRuntime       time.Duration
	LeaseMaxRuntime      time.Duration
	HeartbeatInterval    time.Duration
	HeartbeatGrace       time.Duration
	AttemptBudget        domain.Money
	ArtifactAttemptLimit int
}

func (c Config) withDefaults() Config {
	if c.MaxAttempts <= 0 {
		c.MaxAttempts = 2
	}
	if c.TaskMaxRuntime <= 0 {
		c.TaskMaxRuntime = 30 * time.Minute
	}
	if c.LeaseMaxRuntime <= 0 {
		c.LeaseMaxRuntime = 15 * time.Minute
	}
	if c.HeartbeatInterval <= 0 {
		c.HeartbeatInterval = 30 * time.Second
	}
	if c.HeartbeatGrace <= 0 {
		c.HeartbeatGrace = 90 * time.Second
	}
	if c.AttemptBudget.Minor <= 0 {
		c.AttemptBudget = domain.USDcents(25)
	}
	if c.ArtifactAttemptLimit <= 0 {
		c.ArtifactAttemptLimit = 2
	}
	return c
}

// Reconciler advances task runs. It is safe for concurrent use: the store
// serializes writes and every step re-reads persisted state.
type Reconciler struct {
	store   *store.Store
	sandbox provider.Sandbox
	runner  fake.Runner
	clock   clock.Clock
	cfg     Config
}

// New builds a reconciler.
func New(s *store.Store, sandbox provider.Sandbox, runner fake.Runner, c clock.Clock, cfg Config) *Reconciler {
	if c == nil {
		c = clock.System{}
	}
	return &Reconciler{store: s, sandbox: sandbox, runner: runner, clock: c, cfg: cfg.withDefaults()}
}

// leaseResourceID is the deterministic provider name for a lease. Deriving it
// from the lease id is what makes retried allocation idempotent: the provider
// sees the same name and returns the existing resource.
func leaseResourceID(id domain.LeaseID) string { return "sbx-" + string(id) }

// Step advances one run by at most one durable transition. Returning done=true
// means the run reached a terminal state.
func (r *Reconciler) Step(ctx context.Context, runID domain.TaskRunID) (done bool, err error) {
	run, err := r.store.GetTaskRun(ctx, runID)
	if err != nil {
		return false, err
	}
	if run.State.Terminal() {
		return true, nil
	}
	if r.expired(run) {
		if err := r.failRun(ctx, run, domain.TaskFailed, "max runtime exceeded"); err != nil {
			return false, err
		}
		return true, nil
	}

	switch run.State {
	case domain.TaskRequested:
		err = r.store.TransitionTask(ctx, runID, domain.TaskProvisioning, "")
	case domain.TaskProvisioning:
		err = r.provision(ctx, run)
	case domain.TaskReady:
		err = r.start(ctx, run)
	case domain.TaskRunning:
		err = r.observe(ctx, run)
	case domain.TaskCollecting:
		err = r.collect(ctx, run)
	case domain.TaskTerminating:
		var settled bool
		settled, err = r.terminate(ctx, run)
		if err == nil && settled {
			return true, nil
		}
	default:
		return false, fmt.Errorf("reconcile: unhandled state %q", run.State)
	}
	if err != nil {
		return false, err
	}
	// done is derived from persisted state, never from the branch that ran.
	after, err := r.store.GetTaskRun(ctx, runID)
	if err != nil {
		return false, err
	}
	return after.State.Terminal(), nil
}

// RunToCompletion steps a run until it is terminal or a step fails. It is the
// single entry point used by tests and by the future API adapter; each step is
// persisted, so an interruption leaves a resumable snapshot.
func (r *Reconciler) RunToCompletion(ctx context.Context, runID domain.TaskRunID) (domain.TaskRun, error) {
	const maxSteps = 32
	for i := 0; i < maxSteps; i++ {
		done, err := r.Step(ctx, runID)
		if err != nil {
			return domain.TaskRun{}, err
		}
		if done {
			return r.store.GetTaskRun(ctx, runID)
		}
	}
	return domain.TaskRun{}, errors.New("reconcile: run did not settle within the step bound")
}

func (r *Reconciler) expired(run domain.TaskRun) bool {
	return !run.Deadline.IsZero() && !r.clock.Now().Before(run.Deadline)
}

// provision allocates the attempt lease. An ambiguous allocation is probed by
// its deterministic resource id before a second allocate is attempted.
func (r *Reconciler) provision(ctx context.Context, run domain.TaskRun) error {
	if run.Attempts >= run.MaxAttempts {
		return r.failRun(ctx, run, domain.TaskFailed, "attempt budget exhausted")
	}
	number, err := r.store.IncrementAttempts(ctx, run.ID)
	if err != nil {
		return r.classifyAttemptError(ctx, run, err)
	}
	providerName := "fake"
	attempt, err := r.store.StartAttempt(ctx, run.ID, number, domain.SandboxLease{Provider: providerName})
	if err != nil {
		return err
	}
	resourceID := leaseResourceID(attempt.LeaseID)

	alloc, err := r.sandbox.Allocate(ctx, provider.Request{
		LeaseID:     attempt.LeaseID,
		AttemptID:   attempt.ID,
		TaskRunID:   run.ID,
		WorkspaceID: run.WorkspaceID,
		ResourceID:  resourceID,
		MaxRuntime:  r.cfg.LeaseMaxRuntime,
	})
	if errors.Is(err, provider.ErrCreateTimeout) {
		// The provider may still have created the resource: probe its
		// deterministic name and adopt it rather than allocating twice.
		exists, probeErr := r.sandbox.Probe(ctx, resourceID)
		if probeErr != nil {
			return probeErr
		}
		if !exists {
			return r.retryAttempt(ctx, run, attempt, "allocation timed out")
		}
		if err := r.store.AttachLeaseResource(ctx, attempt.LeaseID, resourceID, domain.LeaseActive); err != nil {
			return err
		}
	} else if err != nil {
		return r.retryAttempt(ctx, run, attempt, err.Error())
	} else if err := r.store.AttachLeaseResource(ctx, attempt.LeaseID, alloc.ResourceID, domain.LeaseActive); err != nil {
		return err
	}

	if err := r.store.TransitionAttempt(ctx, attempt.ID, domain.AttemptRunning, ""); err != nil {
		return err
	}
	return r.store.TransitionTask(ctx, run.ID, domain.TaskReady, "")
}

// retryAttempt closes a failed attempt and either requeues the run or fails it
// once the bounded retry budget is spent.
func (r *Reconciler) retryAttempt(ctx context.Context, run domain.TaskRun, attempt domain.Attempt, cause string) error {
	if err := r.store.TransitionAttempt(ctx, attempt.ID, domain.AttemptFailed, cause); err != nil {
		return err
	}
	if run.Attempts >= run.MaxAttempts {
		return r.failRun(ctx, run, domain.TaskFailed, cause)
	}
	// The run stays in Provisioning: the next step opens the next attempt.
	// No edge is written, which keeps every persisted transition meaningful.
	return nil
}

// classifyAttemptError maps the attempt-budget conflict into a terminal task
// outcome instead of a generic error.
func (r *Reconciler) classifyAttemptError(ctx context.Context, run domain.TaskRun, err error) error {
	if errors.Is(err, domain.ErrConflict) {
		return r.failRun(ctx, run, domain.TaskFailed, "attempt budget exhausted")
	}
	return err
}

func (r *Reconciler) start(ctx context.Context, run domain.TaskRun) error {
	attempt, err := r.activeAttempt(ctx, run.ID)
	if err != nil {
		return err
	}
	if err := r.store.RecordHeartbeat(ctx, attempt.ID); err != nil {
		return err
	}
	return r.store.TransitionTask(ctx, run.ID, domain.TaskRunning, "")
}

// observe runs the command once and records the result, then moves to
// collection. A timed-out command is an interrupted run.
func (r *Reconciler) observe(ctx context.Context, run domain.TaskRun) error {
	attempt, err := r.activeAttempt(ctx, run.ID)
	if err != nil {
		return err
	}
	lease, err := r.store.GetLease(ctx, attempt.LeaseID)
	if err != nil {
		return err
	}
	result, err := r.runner.Run(ctx, fake.RunRequest{
		TaskRunID:  run.ID,
		LeaseID:    lease.ID,
		ResourceID: lease.ProviderResourceID,
		Command:    run.Command,
	})
	if err != nil {
		return err
	}
	if result.TimedOut {
		return r.failRun(ctx, run, domain.TaskInterrupted, "command timed out")
	}
	if result.Error != "" {
		return r.failRun(ctx, run, domain.TaskFailed, safeDetail(result.Error))
	}
	if result.ExitCode != 0 {
		return r.failRun(ctx, run, domain.TaskFailed, fmt.Sprintf("command exited %d", result.ExitCode))
	}
	// Persist the artifact reference and checksum before the run may release
	// its lease; Collecting verifies that the row is really there.
	if err := r.store.SaveArtifact(ctx, run.ID, result.Artifact); err != nil {
		if errors.Is(err, domain.ErrConflict) || result.Artifact.URI == "" {
			return r.failRun(ctx, run, domain.TaskFailed, "artifact could not be persisted")
		}
		return err
	}
	return r.store.TransitionTask(ctx, run.ID, domain.TaskCollecting, "")
}

// collect persists the artifact reference before the lease may be released.
// Without a persisted checksum the run cannot proceed to termination, which is
// what makes the "persist before delete" policy enforceable rather than
// advisory.
func (r *Reconciler) collect(ctx context.Context, run domain.TaskRun) error {
	if _, err := r.store.GetArtifact(ctx, run.ID); errors.Is(err, domain.ErrArtifactMissing) {
		return r.failRun(ctx, run, domain.TaskFailed, "artifact reference missing before release")
	} else if err != nil {
		return err
	}
	return r.store.TransitionTask(ctx, run.ID, domain.TaskTerminating, "")
}

// terminate releases the ephemeral lease. Cleanup failure is recorded and the
// lease becomes stranded so the janitor can retry; the persistent workspace
// box is never part of this path.
func (r *Reconciler) terminate(ctx context.Context, run domain.TaskRun) (bool, error) {
	attempt, err := r.activeAttempt(ctx, run.ID)
	if errors.Is(err, domain.ErrNotFound) {
		// No live attempt: the lease was already settled.
		return true, r.finish(ctx, run, domain.TaskReclaimed, "")
	}
	if err != nil {
		return false, err
	}
	lease, err := r.store.GetLease(ctx, attempt.LeaseID)
	if err != nil {
		return false, err
	}
	if lease.State == domain.LeaseReleased {
		return true, r.finish(ctx, run, domain.TaskReclaimed, "")
	}
	if err := r.store.TransitionLease(ctx, lease.ID, domain.LeaseReleasing, ""); err != nil &&
		!errors.Is(err, domain.ErrInvalidTransition) {
		return false, err
	}
	if err := r.releaseLease(ctx, lease); err != nil {
		return false, err
	}
	if err := r.store.TransitionAttempt(ctx, attempt.ID, domain.AttemptSucceeded, ""); err != nil &&
		!errors.Is(err, domain.ErrInvalidTransition) {
		return false, err
	}
	return true, r.finish(ctx, run, domain.TaskReclaimed, "")
}

func (r *Reconciler) releaseLease(ctx context.Context, lease domain.SandboxLease) error {
	err := r.sandbox.Destroy(ctx, lease.ProviderResourceID)
	switch {
	case err == nil, errors.Is(err, provider.ErrNotFound):
		return r.store.TransitionLease(ctx, lease.ID, domain.LeaseReleased, "")
	case errors.Is(err, provider.ErrDestroyFailed), errors.Is(err, provider.ErrDisabled):
		// Cleanup failure stays visible: the lease is stranded, not released.
		return r.strand(ctx, lease, err.Error())
	default:
		return r.strand(ctx, lease, err.Error())
	}
}

func (r *Reconciler) strand(ctx context.Context, lease domain.SandboxLease, detail string) error {
	if err := r.store.TransitionLease(ctx, lease.ID, domain.LeaseStranded, safeDetail(detail)); err != nil {
		return err
	}
	return &CleanupError{LeaseID: lease.ID, ResourceID: lease.ProviderResourceID, Err: errors.New(safeDetail(detail))}
}

// CleanupError reports a lease that could not be released. The run stays in
// Terminating so the next reconcile pass retries.
type CleanupError struct {
	LeaseID    domain.LeaseID
	ResourceID string
	Err        error
}

func (e *CleanupError) Error() string {
	return fmt.Sprintf("reconcile: cleanup failed for lease %s: %v", e.LeaseID, e.Err)
}
func (e *CleanupError) Unwrap() error { return e.Err }

// Fail records a failed or interrupted outcome and releases the lease.
func (r *Reconciler) Fail(ctx context.Context, runID domain.TaskRunID, outcome domain.TaskState, detail string) error {
	run, err := r.store.GetTaskRun(ctx, runID)
	if err != nil {
		return err
	}
	return r.failRun(ctx, run, outcome, detail)
}

// failRun moves a run to a failed or interrupted outcome and releases the
// attempt's lease if one exists. Task outcome and lease release are separate
// facts: the box is untouched.
func (r *Reconciler) failRun(ctx context.Context, run domain.TaskRun, outcome domain.TaskState, detail string) error {
	if outcome != domain.TaskFailed && outcome != domain.TaskInterrupted {
		return fmt.Errorf("reconcile: %s is not a failure outcome", outcome)
	}
	if err := r.releaseForRun(ctx, run.ID, outcome); err != nil {
		return err
	}
	return r.store.TransitionTask(ctx, run.ID, outcome, safeDetail(detail))
}

// releaseForRun best-effort releases the active attempt's lease and closes the
// attempt. A stranded lease is reported but does not change the task outcome.
func (r *Reconciler) releaseForRun(ctx context.Context, runID domain.TaskRunID, outcome domain.TaskState) error {
	attempt, err := r.activeAttempt(ctx, runID)
	if errors.Is(err, domain.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	lease, err := r.store.GetLease(ctx, attempt.LeaseID)
	if err == nil && lease.State != domain.LeaseReleased {
		if transitionErr := r.store.TransitionLease(ctx, lease.ID, domain.LeaseReleasing, ""); transitionErr != nil &&
			!errors.Is(transitionErr, domain.ErrInvalidTransition) {
			return transitionErr
		}
		if releaseErr := r.releaseLease(ctx, lease); releaseErr != nil {
			var cleanup *CleanupError
			if !errors.As(releaseErr, &cleanup) {
				return releaseErr
			}
		}
	}
	target := domain.AttemptFailed
	if outcome == domain.TaskInterrupted {
		target = domain.AttemptCancelled
	}
	if transitionErr := r.store.TransitionAttempt(ctx, attempt.ID, target, safeDetail(string(outcome))); transitionErr != nil &&
		!errors.Is(transitionErr, domain.ErrInvalidTransition) {
		return transitionErr
	}
	return nil
}

func (r *Reconciler) finish(ctx context.Context, run domain.TaskRun, outcome domain.TaskState, detail string) error {
	if err := r.store.ReleaseReservation(ctx, run.ID); err != nil {
		return err
	}
	return r.store.TransitionTask(ctx, run.ID, outcome, detail)
}

// Cancel marks a pending or running task interrupted and releases its lease.
func (r *Reconciler) Cancel(ctx context.Context, runID domain.TaskRunID) error {
	run, err := r.store.GetTaskRun(ctx, runID)
	if err != nil {
		return err
	}
	if run.State.Terminal() {
		return domain.ErrTerminalState
	}
	return r.failRun(ctx, run, domain.TaskInterrupted, "cancelled by request")
}

// activeAttempt returns the run's unfinished attempt, if any.
func (r *Reconciler) activeAttempt(ctx context.Context, runID domain.TaskRunID) (domain.Attempt, error) {
	attempts, err := r.store.ListAttempts(ctx, runID)
	if err != nil {
		return domain.Attempt{}, err
	}
	for i := len(attempts) - 1; i >= 0; i-- {
		if !attempts[i].State.Terminal() {
			return attempts[i], nil
		}
	}
	return domain.Attempt{}, domain.ErrNotFound
}

// safeDetail bounds free text that reaches the store: it keeps one line and a
// fixed length so a provider message cannot smuggle prompt or secret material
// into the audit trail.
func safeDetail(s string) string {
	s = strings.ReplaceAll(s, "\n", " ")
	s = strings.TrimSpace(s)
	const limit = 200
	if len(s) > limit {
		s = s[:limit]
	}
	return s
}
