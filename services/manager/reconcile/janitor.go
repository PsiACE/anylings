package reconcile

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
	"github.com/PsiACE/anylings/services/manager/provider"
)

// JanitorReport summarizes one reconcile pass over persisted state.
type JanitorReport struct {
	Resumed     []domain.TaskRunID
	Interrupted []domain.TaskRunID
	Stranded    []domain.LeaseID
	LeasesFreed []domain.LeaseID
}

// Recover is the crash-reconcile pass. It is the only recovery path: nothing
// depends on a finally block or on in-process state having run, so a killed
// process leaves work that this pass picks up from the database.
func (r *Reconciler) Recover(ctx context.Context) (JanitorReport, error) {
	var report JanitorReport

	// 1. Attempts whose heartbeat lapsed are interrupted, not resumed: the
	//    worker that held them may still be alive somewhere, so taking the
	//    lease is safer than double-running the command.
	cutoff := r.clock.Now().Add(-r.cfg.HeartbeatGrace)
	stale, err := r.store.ListStaleAttempts(ctx, cutoff)
	if err != nil {
		return report, err
	}
	for _, attempt := range stale {
		run, err := r.store.GetTaskRun(ctx, attempt.TaskRunID)
		if err != nil {
			return report, err
		}
		if run.State.Terminal() {
			continue
		}
		if err := r.failRun(ctx, run, domain.TaskInterrupted, "heartbeat expired"); err != nil {
			var cleanup *CleanupError
			if !errors.As(err, &cleanup) {
				return report, err
			}
			report.Stranded = append(report.Stranded, cleanup.LeaseID)
		}
		report.Interrupted = append(report.Interrupted, run.ID)
	}

	// 2. Live runs are resumed from their persisted state.
	for _, state := range []domain.TaskState{
		domain.TaskRequested, domain.TaskProvisioning, domain.TaskReady,
		domain.TaskRunning, domain.TaskCollecting, domain.TaskTerminating,
	} {
		runs, err := r.store.ListTaskRunsInState(ctx, state)
		if err != nil {
			return report, err
		}
		for _, run := range runs {
			report.Resumed = append(report.Resumed, run.ID)
		}
	}

	// 3. Stranded leases get another release attempt. A lease whose release
	//    still fails stays stranded and visible rather than silently dropped.
	stranded, err := r.store.ListLeasesInState(ctx, domain.LeaseStranded)
	if err != nil {
		return report, err
	}
	for _, lease := range stranded {
		err := r.sandbox.Destroy(ctx, lease.ProviderResourceID)
		if err == nil || errors.Is(err, provider.ErrNotFound) {
			if err := r.store.TransitionLease(ctx, lease.ID, domain.LeaseReleased, ""); err != nil {
				return report, err
			}
			report.LeasesFreed = append(report.LeasesFreed, lease.ID)
			continue
		}
		report.Stranded = append(report.Stranded, lease.ID)
	}

	// 4. Allocating leases older than the runtime bound never confirmed; the
	//    resource may exist, so probe before declaring it released.
	allocating, err := r.store.ListLeasesInState(ctx, domain.LeaseAllocating)
	if err != nil {
		return report, err
	}
	for _, lease := range allocating {
		if r.clock.Now().Sub(lease.CreatedAt) < r.cfg.LeaseMaxRuntime {
			continue
		}
		// Probe by the persisted identity. A lease row always carries its
		// deterministic provider name, so an empty name would mean a corrupt
		// row rather than an absent resource.
		resourceID := lease.ProviderResourceID
		if resourceID == "" {
			resourceID = domain.LeaseResourceID(lease.ID)
		}
		exists, probeErr := r.sandbox.Probe(ctx, resourceID)
		if probeErr != nil {
			return report, probeErr
		}
		if !exists {
			if err := r.store.TransitionLease(ctx, lease.ID, domain.LeaseReleased, "allocation never confirmed"); err != nil {
				return report, err
			}
			report.LeasesFreed = append(report.LeasesFreed, lease.ID)
			continue
		}
		// The resource is real: adopt the identity and release it.
		if err := r.store.AttachLeaseResource(ctx, lease.ID, resourceID, domain.LeaseActive); err != nil &&
			!errors.Is(err, domain.ErrInvalidTransition) {
			return report, err
		}
		adopted, err := r.store.GetLease(ctx, lease.ID)
		if err != nil {
			return report, err
		}
		if err := r.releaseLease(ctx, adopted); err != nil {
			report.Stranded = append(report.Stranded, lease.ID)
			continue
		}
		report.LeasesFreed = append(report.LeasesFreed, lease.ID)
	}

	// 5. Terminal runs whose hold was retained now that cleanup succeeded get
	//    their reservation back. This is what keeps a retained hold temporary
	//    instead of a permanent one.
	held, err := r.store.HeldRuns(ctx)
	if err != nil {
		return report, err
	}
	if err := r.store.ReleaseCleanHolds(ctx, held); err != nil {
		return report, err
	}
	return report, nil
}

// Sweep runs Recover and then advances every resumable run once. It returns the
// first error while keeping the report, so a caller can log partial progress.
func (r *Reconciler) Sweep(ctx context.Context) (JanitorReport, error) {
	report, err := r.Recover(ctx)
	if err != nil {
		return report, err
	}
	for _, id := range report.Resumed {
		if _, stepErr := r.Step(ctx, id); stepErr != nil {
			var cleanup *CleanupError
			if errors.As(stepErr, &cleanup) {
				continue
			}
			return report, fmt.Errorf("reconcile: sweep %s: %w", id, stepErr)
		}
	}
	return report, nil
}

// Heartbeat records liveness for the run's active attempt. It fails when the
// attempt is already terminal, which is how a cancelled run rejects late work.
func (r *Reconciler) Heartbeat(ctx context.Context, runID domain.TaskRunID) error {
	attempt, err := r.activeAttempt(ctx, runID)
	if err != nil {
		return err
	}
	return r.store.RecordHeartbeat(ctx, attempt.ID)
}

// ReclaimBox reclaims a persistent workspace box. It is deliberately separate
// from task termination, and the store refuses while any run or lease of the
// workspace could still use the box, so task cleanup can never erase it.
func (r *Reconciler) ReclaimBox(ctx context.Context, ws domain.WorkspaceID) error {
	return r.store.ReclaimBox(ctx, ws)
}

// HeartbeatDeadline reports when an attempt is considered lost. Tests use it to
// advance the fake clock instead of sleeping.
func (r *Reconciler) HeartbeatDeadline(attempt domain.Attempt) time.Time {
	return attempt.HeartbeatAt.Add(r.cfg.HeartbeatGrace)
}
