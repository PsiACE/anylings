package reconcile

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
	"github.com/PsiACE/anylings/services/manager/provider"
	"github.com/PsiACE/anylings/services/manager/provider/fake"
	"github.com/PsiACE/anylings/services/manager/store"
)

// These tests cover three defects found in review: admission holds leaked on
// every failure path, box reclamation ignored non-running work, and a crash
// window could release a lease while its provider resource was still alive.

// reserveRun holds an admission amount for a run so a leak would be visible.
func (h *harness) reserveRun(run domain.TaskRun, minor int64, limits store.Limits) {
	h.t.Helper()
	if err := h.store.Reserve(context.Background(), run.ID, domain.Amount(minor, domain.USD), limits); err != nil {
		h.t.Fatalf("reserve: %v", err)
	}
}

func (h *harness) reserved(minor int64) {
	h.t.Helper()
	total, err := h.store.ReservedTotal(context.Background(), h.ws.ID)
	if err != nil {
		h.t.Fatalf("reserved total: %v", err)
	}
	if total.Minor != minor {
		h.t.Fatalf("reserved = %d, want %d", total.Minor, minor)
	}
}

// H1: a command failure after a successful resource cleanup must return the
// admission hold, otherwise the next request is refused for no live reason.
func TestFailedRunReleasesReservation(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	limits := store.DefaultLimits()
	limits.WorkspaceBudget = budget(100)

	run := h.newRun("h1-failed")
	h.reserveRun(run, 80, limits)
	h.runner.Script(run.ID, fake.RunResult{ExitCode: 1})

	final, err := h.rec.RunToCompletion(ctx, run.ID)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if final.State != domain.TaskFailed {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskFailed)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("resource still live: %d", h.sandbox.Live())
	}
	h.reserved(0)

	// The released hold makes the budget usable again.
	next := h.newRun("h1-next")
	h.reserveRun(next, 30, limits)
}

// H1: cancellation, heartbeat expiry, deadline expiry and retry exhaustion all
// end a run, and every one of them must release the hold.
func TestAllTerminalPathsReleaseReservation(t *testing.T) {
	cases := []struct {
		name  string
		drive func(h *harness, run domain.TaskRun, limits store.Limits)
	}{
		{
			name: "cancel",
			drive: func(h *harness, run domain.TaskRun, limits store.Limits) {
				for i := 0; i < 3; i++ {
					if _, err := h.rec.Step(context.Background(), run.ID); err != nil {
						h.t.Fatalf("step %d: %v", i, err)
					}
				}
				if err := h.rec.Cancel(context.Background(), run.ID); err != nil {
					h.t.Fatalf("cancel: %v", err)
				}
			},
		},
		{
			name: "heartbeat expiry",
			drive: func(h *harness, run domain.TaskRun, limits store.Limits) {
				for i := 0; i < 2; i++ {
					if _, err := h.rec.Step(context.Background(), run.ID); err != nil {
						h.t.Fatalf("step %d: %v", i, err)
					}
				}
				h.clock.Advance(10 * time.Minute)
				if _, err := h.rec.Recover(context.Background()); err != nil {
					h.t.Fatalf("recover: %v", err)
				}
			},
		},
		{
			name: "deadline expiry",
			drive: func(h *harness, run domain.TaskRun, limits store.Limits) {
				if _, err := h.rec.Step(context.Background(), run.ID); err != nil {
					h.t.Fatalf("step: %v", err)
				}
				h.clock.Advance(2 * time.Hour)
				if _, err := h.rec.Step(context.Background(), run.ID); err != nil {
					h.t.Fatalf("expired step: %v", err)
				}
			},
		},
		{
			name: "retry exhaustion",
			drive: func(h *harness, run domain.TaskRun, limits store.Limits) {
				h.sandbox.CreateFailures = 2
				if _, err := h.rec.RunToCompletion(context.Background(), run.ID); err != nil {
					h.t.Fatalf("run: %v", err)
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t)
			limits := store.DefaultLimits()
			limits.WorkspaceBudget = budget(100)
			run := h.newRun("h1-path")
			h.reserveRun(run, 80, limits)

			tc.drive(h, run, limits)

			final, err := h.store.GetTaskRun(context.Background(), run.ID)
			if err != nil {
				t.Fatalf("get run: %v", err)
			}
			if !final.State.Terminal() {
				t.Fatalf("state = %s, want a terminal state", final.State)
			}
			h.reserved(0)
			if h.sandbox.Live() != 0 {
				t.Fatalf("resource still live: %d", h.sandbox.Live())
			}
		})
	}
}

// H1 safety: when cleanup fails the resource may still exist, so the hold is
// retained and stays visible. Once a later pass confirms cleanup, it is
// released rather than kept forever.
func TestRetainedHoldIsReleasedAfterCleanupSucceeds(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	limits := store.DefaultLimits()
	limits.WorkspaceBudget = budget(100)

	run := h.newRun("h1-retain")
	h.reserveRun(run, 80, limits)
	for i := 0; i < 2; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	h.sandbox.DestroyFailures = 1
	if err := h.rec.Fail(ctx, run.ID, domain.TaskFailed, "command failed"); err != nil {
		t.Fatalf("fail: %v", err)
	}

	// The lease is stranded, so the hold is retained on purpose.
	h.reserved(80)
	held, err := h.store.HeldRuns(ctx)
	if err != nil {
		t.Fatalf("held runs: %v", err)
	}
	if len(held) != 1 || held[0] != run.ID {
		t.Fatalf("held runs = %v, want [%s]", held, run.ID)
	}

	if _, err := h.rec.Recover(ctx); err != nil {
		t.Fatalf("recover: %v", err)
	}
	h.reserved(0)
	if h.sandbox.Live() != 0 {
		t.Fatalf("resource still live: %d", h.sandbox.Live())
	}
	held, err = h.store.HeldRuns(ctx)
	if err != nil {
		t.Fatalf("held runs: %v", err)
	}
	if len(held) != 0 {
		t.Fatalf("held runs after cleanup = %v, want none", held)
	}
}

// H2: a run that is Ready with an active lease blocks reclamation.
func TestReclaimBoxRefusesReadyRunWithActiveLease(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("h2-ready")

	// Requested -> Provisioning -> Ready leaves an Active lease.
	for i := 0; i < 2; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	if err := h.rec.ReclaimBox(ctx, h.ws.ID); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("reclaim error = %v, want ErrConflict", err)
	}
	ws, err := h.store.GetWorkspace(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if ws.BoxState != domain.ResourceReady {
		t.Fatalf("box state = %s, want %s", ws.BoxState, domain.ResourceReady)
	}
}

// H2: every non-terminal state blocks reclamation, not just Running.
func TestReclaimBoxRefusesEveryNonTerminalState(t *testing.T) {
	for _, tc := range []struct {
		name  string
		steps int
	}{
		{"Requested", 0},
		{"Provisioning", 1},
		{"Ready", 2},
		{"Running", 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t)
			ctx := context.Background()
			run := h.newRun("h2-state")
			for i := 0; i < tc.steps; i++ {
				if _, err := h.rec.Step(ctx, run.ID); err != nil {
					t.Fatalf("step %d: %v", i, err)
				}
			}
			if err := h.rec.ReclaimBox(ctx, h.ws.ID); !errors.Is(err, domain.ErrConflict) {
				t.Fatalf("reclaim in %s error = %v, want ErrConflict", tc.name, err)
			}
		})
	}
}

// H2: a terminal run whose lease was never released still blocks reclamation,
// because the provider resource may still exist.
func TestReclaimBoxRefusesTerminalRunWithUnreleasedLease(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("h2-stranded")

	for i := 0; i < 2; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	h.sandbox.DestroyFailures = 1
	if err := h.rec.Fail(ctx, run.ID, domain.TaskFailed, "command failed"); err != nil {
		t.Fatalf("fail: %v", err)
	}
	failed, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if !failed.State.Terminal() {
		t.Fatalf("state = %s, want terminal", failed.State)
	}
	if err := h.rec.ReclaimBox(ctx, h.ws.ID); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("reclaim with unreleased lease error = %v, want ErrConflict", err)
	}

	// After cleanup the box may be reclaimed.
	if _, err := h.rec.Recover(ctx); err != nil {
		t.Fatalf("recover: %v", err)
	}
	if err := h.rec.ReclaimBox(ctx, h.ws.ID); err != nil {
		t.Fatalf("reclaim after cleanup: %v", err)
	}
}

// H3: a crash after allocation but before the lease was marked Active must not
// release the lease while the provider resource lives. The expiry path probes
// the persisted identity, adopts the resource and destroys it.
func TestExpiryPathNeverReleasesLiveUnattachedResource(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("h3-expiry")
	attempt, err := h.store.StartAttempt(ctx, run.ID, 1, domain.SandboxLease{Provider: "fake"})
	if err != nil {
		t.Fatalf("start attempt: %v", err)
	}
	// The provider creates the resource; the crash happens before the lease
	// records that fact.
	if _, err := h.sandbox.Allocate(ctx, provider.Request{
		LeaseID:    attempt.LeaseID,
		AttemptID:  attempt.ID,
		TaskRunID:  run.ID,
		ResourceID: domain.LeaseResourceID(attempt.LeaseID),
	}); err != nil {
		t.Fatalf("allocate: %v", err)
	}
	h.clock.Advance(20 * time.Minute) // beyond LeaseMaxRuntime

	report, err := h.rec.Recover(ctx)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	lease, err := h.store.GetLease(ctx, attempt.LeaseID)
	if err != nil {
		t.Fatalf("get lease: %v", err)
	}
	if lease.State == domain.LeaseReleased && h.sandbox.Live() != 0 {
		t.Fatalf("lease released while %d resource(s) live: %+v", h.sandbox.Live(), report)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("resource leak: provider holds %d resource(s)", h.sandbox.Live())
	}
	if lease.State != domain.LeaseReleased {
		t.Fatalf("lease state = %s, want %s", lease.State, domain.LeaseReleased)
	}
}

// H3: the same crash window on the interrupt path (heartbeat expiry) must also
// destroy the live resource instead of releasing an empty identity.
func TestInterruptPathNeverReleasesLiveUnattachedResource(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("h3-interrupt")
	attempt, err := h.store.StartAttempt(ctx, run.ID, 1, domain.SandboxLease{Provider: "fake"})
	if err != nil {
		t.Fatalf("start attempt: %v", err)
	}
	if _, err := h.sandbox.Allocate(ctx, provider.Request{
		LeaseID:    attempt.LeaseID,
		AttemptID:  attempt.ID,
		TaskRunID:  run.ID,
		ResourceID: domain.LeaseResourceID(attempt.LeaseID),
	}); err != nil {
		t.Fatalf("allocate: %v", err)
	}
	h.clock.Advance(10 * time.Minute) // beyond the heartbeat grace

	if _, err := h.rec.Recover(ctx); err != nil {
		t.Fatalf("recover: %v", err)
	}
	final, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	lease, err := h.store.GetLease(ctx, attempt.LeaseID)
	if err != nil {
		t.Fatalf("get lease: %v", err)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("resource leak on %s path: provider holds %d resource(s), lease %s",
			final.State, h.sandbox.Live(), lease.State)
	}
	if lease.State != domain.LeaseReleased {
		t.Fatalf("lease state = %s, want %s", lease.State, domain.LeaseReleased)
	}
}

// H3: repeated sweeps are idempotent - no second destroy, no state churn, and
// the hold is released exactly once.
func TestRepeatedSweepsAreIdempotent(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	limits := store.DefaultLimits()
	limits.WorkspaceBudget = budget(100)

	run := h.newRun("h3-idempotent")
	h.reserveRun(run, 40, limits)
	attempt, err := h.store.StartAttempt(ctx, run.ID, 1, domain.SandboxLease{Provider: "fake"})
	if err != nil {
		t.Fatalf("start attempt: %v", err)
	}
	if _, err := h.sandbox.Allocate(ctx, provider.Request{
		LeaseID:    attempt.LeaseID,
		AttemptID:  attempt.ID,
		TaskRunID:  run.ID,
		ResourceID: domain.LeaseResourceID(attempt.LeaseID),
	}); err != nil {
		t.Fatalf("allocate: %v", err)
	}
	h.clock.Advance(20 * time.Minute)

	for i := 0; i < 3; i++ {
		if _, err := h.rec.Recover(ctx); err != nil {
			t.Fatalf("recover %d: %v", i, err)
		}
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("resource still live after repeated sweeps: %d", h.sandbox.Live())
	}
	if got := h.sandbox.TotalDestroys(); got != 1 {
		t.Fatalf("destroy calls = %d, want 1", got)
	}
	h.reserved(0)
}

// H2: a concurrent request and reclaim cannot both succeed.
func TestConcurrentRequestAndReclaimCannotBothSucceed(t *testing.T) {
	for i := 0; i < 16; i++ {
		h := newHarness(t)
		ctx := context.Background()

		var (
			wg        sync.WaitGroup
			mu        sync.Mutex
			reclaimed error
			requested error
		)
		start := make(chan struct{})
		wg.Add(2)
		go func() {
			defer wg.Done()
			<-start
			_, _, err := h.store.RequestTask(ctx, store.RequestTask{
				WorkspaceID: h.ws.ID,
				RequestID:   "h2-race",
				Command:     "echo test",
				MaxAttempts: 2,
				Deadline:    epoch.Add(time.Hour),
			})
			mu.Lock()
			requested = err
			mu.Unlock()
		}()
		go func() {
			defer wg.Done()
			<-start
			err := h.rec.ReclaimBox(ctx, h.ws.ID)
			mu.Lock()
			reclaimed = err
			mu.Unlock()
		}()
		close(start)
		wg.Wait()

		if requested == nil && reclaimed == nil {
			t.Fatalf("iteration %d: both a new run and box reclamation succeeded", i)
		}
	}
}
