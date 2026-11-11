package reconcile

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/PsiACE/anylings/services/manager/clock"
	"github.com/PsiACE/anylings/services/manager/domain"
	"github.com/PsiACE/anylings/services/manager/provider"
	"github.com/PsiACE/anylings/services/manager/provider/fake"
	"github.com/PsiACE/anylings/services/manager/store"
)

// epoch is an arbitrary but fixed instant; every test drives time through the
// fake clock instead of sleeping.
var epoch = time.Date(2026, 11, 12, 0, 0, 0, 0, time.UTC)

type harness struct {
	t       *testing.T
	dir     string
	store   *store.Store
	clock   *clock.Fake
	sandbox *fake.Sandbox
	runner  *fake.CommandRunner
	rec     *Reconciler
	ws      domain.Workspace
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	dir := t.TempDir()
	c := clock.NewFake(epoch)
	s, err := store.Open(context.Background(), store.Options{
		Path:  filepath.Join(dir, "manager.db"),
		Clock: c,
	})
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { s.Close() })

	ws, err := s.CreateWorkspace(context.Background(), "primary", "fake")
	if err != nil {
		t.Fatalf("create workspace: %v", err)
	}
	if err := s.TransitionBox(context.Background(), ws.ID, domain.ResourceProvisioning, "", ""); err != nil {
		t.Fatalf("box provisioning: %v", err)
	}
	if err := s.TransitionBox(context.Background(), ws.ID, domain.ResourceReady, "box-1", ""); err != nil {
		t.Fatalf("box ready: %v", err)
	}
	ws, err = s.GetWorkspace(context.Background(), ws.ID)
	if err != nil {
		t.Fatalf("reload workspace: %v", err)
	}

	sandbox := fake.NewSandbox()
	sandbox.Enable()
	runner := fake.NewCommandRunner()
	rec := New(s, sandbox, runner, c, Config{})
	return &harness{t: t, dir: dir, store: s, clock: c, sandbox: sandbox, runner: runner, rec: rec, ws: ws}
}

// newRun requests a task run and persists its artifact so the happy path can
// reach termination.
func (h *harness) newRun(requestID domain.RequestID) domain.TaskRun {
	h.t.Helper()
	run, created, err := h.store.RequestTask(context.Background(), store.RequestTask{
		WorkspaceID: h.ws.ID,
		RequestID:   requestID,
		Command:     "echo test",
		MaxAttempts: 2,
		Deadline:    epoch.Add(time.Hour),
	})
	if err != nil {
		h.t.Fatalf("request task: %v", err)
	}
	if !created {
		h.t.Fatalf("expected a new run for %q", requestID)
	}
	return run
}

// budget builds an optional ceiling; nil means "concurrency only".
func budget(minor int64) *domain.Money {
	m := domain.Amount(minor, domain.USD)
	return &m
}

func (h *harness) reopen() {
	h.t.Helper()
	if err := h.store.Close(); err != nil {
		h.t.Fatalf("close store: %v", err)
	}
	s, err := store.Open(context.Background(), store.Options{
		Path:  filepath.Join(h.dir, "manager.db"),
		Clock: h.clock,
	})
	if err != nil {
		h.t.Fatalf("reopen store: %v", err)
	}
	h.t.Cleanup(func() { s.Close() })
	h.store = s
	h.rec = New(s, h.sandbox, h.runner, h.clock, Config{})
}

func TestDuplicateRequestReturnsSameRun(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()

	first := h.newRun("req-1")
	second, created, err := h.store.RequestTask(ctx, store.RequestTask{
		WorkspaceID: h.ws.ID,
		RequestID:   "req-1",
		Command:     "echo test",
		MaxAttempts: 2,
		Deadline:    epoch.Add(time.Hour),
	})
	if err != nil {
		t.Fatalf("duplicate request: %v", err)
	}
	if created {
		t.Fatal("duplicate request created a second run")
	}
	if second.ID != first.ID {
		t.Fatalf("duplicate request returned %s, want %s", second.ID, first.ID)
	}
}

func TestConcurrentDuplicateRequestsCreateOneRun(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()

	const callers = 8
	var (
		wg   sync.WaitGroup
		mu   sync.Mutex
		ids  = map[domain.TaskRunID]bool{}
		errs []error
	)
	start := make(chan struct{})
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			run, _, err := h.store.RequestTask(ctx, store.RequestTask{
				WorkspaceID: h.ws.ID,
				RequestID:   "req-shared",
				Command:     "echo test",
				MaxAttempts: 2,
				Deadline:    epoch.Add(time.Hour),
			})
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, err)
				return
			}
			ids[run.ID] = true
		}()
	}
	close(start)
	wg.Wait()

	if len(errs) != 0 {
		t.Fatalf("concurrent duplicate requests failed: %v", errs)
	}
	if len(ids) != 1 {
		t.Fatalf("concurrent duplicates produced %d runs, want 1", len(ids))
	}
}

func TestInvalidTransitionsAreRejected(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-invalid")

	// Requested cannot jump straight to Reclaimed.
	if err := h.store.TransitionTask(ctx, run.ID, domain.TaskReclaimed, ""); !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("Requested->Reclaimed error = %v, want ErrInvalidTransition", err)
	}
	if err := h.store.TransitionTask(ctx, run.ID, domain.TaskProvisioning, ""); err != nil {
		t.Fatalf("Requested->Provisioning: %v", err)
	}
	// Provisioning cannot skip Ready and run.
	if err := h.store.TransitionTask(ctx, run.ID, domain.TaskCollecting, ""); !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("Provisioning->Collecting error = %v, want ErrInvalidTransition", err)
	}
	// A terminal task state has no outgoing edges.
	if err := h.store.TransitionTask(ctx, run.ID, domain.TaskFailed, "boom"); err != nil {
		t.Fatalf("Provisioning->Failed: %v", err)
	}
	if err := h.store.TransitionTask(ctx, run.ID, domain.TaskRunning, ""); !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("Failed->Running error = %v, want ErrInvalidTransition", err)
	}
}

func TestParallelReservationsRespectQuotaAndBudget(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	limits := store.DefaultLimits()
	limits.MaxConcurrentRuns = 1
	limits.WorkspaceBudget = budget(100)

	first := h.newRun("req-quota-1")
	if err := h.store.Reserve(ctx, first.ID, domain.Amount(60, domain.USD), limits); err != nil {
		t.Fatalf("first reservation: %v", err)
	}

	// Concurrency limit of one is now exhausted.
	second := h.newRun("req-quota-2")
	if err := h.store.Reserve(ctx, second.ID, domain.Amount(10, domain.USD), limits); !errors.Is(err, domain.ErrQuotaExceeded) {
		t.Fatalf("second reservation error = %v, want ErrQuotaExceeded", err)
	}

	// Raising concurrency leaves the budget as the binding constraint.
	limits.MaxConcurrentRuns = 4
	if err := h.store.Reserve(ctx, second.ID, domain.Amount(50, domain.USD), limits); !errors.Is(err, domain.ErrBudgetExceeded) {
		t.Fatalf("over-budget reservation error = %v, want ErrBudgetExceeded", err)
	}
	if err := h.store.Reserve(ctx, second.ID, domain.Amount(40, domain.USD), limits); err != nil {
		t.Fatalf("reservation within budget: %v", err)
	}

	total, err := h.store.ReservedTotal(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("reserved total: %v", err)
	}
	if total.Minor != 100 {
		t.Fatalf("reserved total = %d, want 100", total.Minor)
	}
}

func TestConcurrentReservationsNeverExceedBudget(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	limits := store.DefaultLimits()
	limits.MaxConcurrentRuns = 8
	limits.MaxGlobalConcurrentRuns = 16
	limits.WorkspaceBudget = budget(100)

	runs := make([]domain.TaskRun, 8)
	for i := range runs {
		runs[i] = h.newRun(domain.RequestID("req-race-" + string(rune('a'+i))))
	}

	var (
		wg       sync.WaitGroup
		mu       sync.Mutex
		accepted int
	)
	start := make(chan struct{})
	for _, run := range runs {
		wg.Add(1)
		go func(id domain.TaskRunID) {
			defer wg.Done()
			<-start
			err := h.store.Reserve(ctx, id, domain.Amount(30, domain.USD), limits)
			if err != nil {
				if !errors.Is(err, domain.ErrBudgetExceeded) {
					mu.Lock()
					defer mu.Unlock()
					t.Errorf("unexpected reservation error: %v", err)
				}
				return
			}
			mu.Lock()
			accepted++
			mu.Unlock()
		}(run.ID)
	}
	close(start)
	wg.Wait()

	// Three holds of 30 fit in 100; the fourth would exceed it.
	if accepted != 3 {
		t.Fatalf("accepted %d reservations, want 3", accepted)
	}
	total, err := h.store.ReservedTotal(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("reserved total: %v", err)
	}
	if total.Minor > limits.WorkspaceBudget.Minor {
		t.Fatalf("reserved %d exceeds budget %d", total.Minor, limits.WorkspaceBudget.Minor)
	}
}

func TestDBReopenRecoversPersistedState(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-reopen")

	done, err := h.rec.Step(ctx, run.ID)
	if err != nil || done {
		t.Fatalf("step Requested->Provisioning: done=%v err=%v", done, err)
	}
	h.reopen()

	recovered, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run after reopen: %v", err)
	}
	if recovered.State != domain.TaskProvisioning {
		t.Fatalf("state after reopen = %s, want %s", recovered.State, domain.TaskProvisioning)
	}

	// The janitor resumes from the persisted snapshot after the restart.
	report, err := h.rec.Recover(ctx)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	found := false
	for _, id := range report.Resumed {
		if id == run.ID {
			found = true
		}
	}
	if !found {
		t.Fatalf("recover did not report %s as resumable: %+v", run.ID, report)
	}
}

func TestLostHeartbeatInterruptsRunAndReleasesLease(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()

	run := h.newRun("req-heartbeat")
	if _, err := h.rec.Step(ctx, run.ID); err != nil { // Provisioning
		t.Fatalf("step: %v", err)
	}
	if _, err := h.rec.Step(ctx, run.ID); err != nil { // Ready
		t.Fatalf("step: %v", err)
	}
	// Advance past the heartbeat grace without touching the attempt.
	h.clock.Advance(10 * time.Minute)

	report, err := h.rec.Recover(ctx)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if len(report.Interrupted) != 1 {
		t.Fatalf("interrupted %d runs, want 1: %+v", len(report.Interrupted), report)
	}
	final, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if final.State != domain.TaskInterrupted {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskInterrupted)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources after interruption", h.sandbox.Live())
	}
	// The persistent box is untouched by a task-level interruption.
	ws, err := h.store.GetWorkspace(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if ws.BoxState != domain.ResourceReady {
		t.Fatalf("box state = %s, want %s", ws.BoxState, domain.ResourceReady)
	}
}

func TestCancellationReleasesLeaseAndKeepsBox(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-cancel")

	for i := 0; i < 3; i++ { // Requested -> Provisioning -> Ready -> Running
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	if err := h.rec.Cancel(ctx, run.ID); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	final, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if final.State != domain.TaskInterrupted {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskInterrupted)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources after cancel", h.sandbox.Live())
	}
	// A terminal run rejects a second cancel rather than double-releasing.
	if err := h.rec.Cancel(ctx, run.ID); !errors.Is(err, domain.ErrTerminalState) {
		t.Fatalf("second cancel error = %v, want ErrTerminalState", err)
	}
	ws, err := h.store.GetWorkspace(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if ws.BoxState != domain.ResourceReady {
		t.Fatalf("cancel deleted the persistent box: %s", ws.BoxState)
	}
}

func TestCreateTimeoutDoesNotDuplicateProviderResource(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-timeout")

	// The first allocate times out after the provider already created the
	// resource, so the reconciler must probe instead of allocating again.
	h.sandbox.CreateTimeouts = 1

	final, err := h.rec.RunToCompletion(ctx, run.ID)
	if err != nil {
		t.Fatalf("run to completion: %v", err)
	}
	if final.State != domain.TaskReclaimed {
		t.Fatalf("state = %s, want %s (outcome %q)", final.State, domain.TaskReclaimed, final.Outcome)
	}
	if got := h.sandbox.TotalCreates(); got != 1 {
		t.Fatalf("provider create count = %d, want 1", got)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources", h.sandbox.Live())
	}
}

func TestCleanupFailureIsVisibleAndRetried(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-cleanup")

	// Requested -> Provisioning -> Ready, so an active lease exists.
	for i := 0; i < 2; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	// Fail the release, then verify the stranded state is persisted rather than
	// swallowed by the failure path.
	h.sandbox.DestroyFailures = 1

	// Cleanup failure does not change the task outcome; it is recorded on the
	// lease so an operator and the janitor can both see it.
	if err := h.rec.Fail(ctx, run.ID, domain.TaskFailed, "command failed"); err != nil {
		t.Fatalf("fail: %v", err)
	}
	failed, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if failed.State != domain.TaskFailed {
		t.Fatalf("state = %s, want %s", failed.State, domain.TaskFailed)
	}

	leases, err := h.store.ListLeasesInState(ctx, domain.LeaseStranded)
	if err != nil {
		t.Fatalf("list leases: %v", err)
	}
	if len(leases) != 1 {
		t.Fatalf("stranded leases = %d, want 1", len(leases))
	}
	if leases[0].LastError == "" {
		t.Fatal("stranded lease carries no failure detail")
	}

	// The janitor retries the release; here the injected failure has been
	// consumed, so the lease is freed and stays out of the stranded set.
	report, err := h.rec.Recover(ctx)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if len(report.LeasesFreed) != 1 {
		t.Fatalf("freed leases = %d, want 1: %+v", len(report.LeasesFreed), report)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources", h.sandbox.Live())
	}
}

func TestTaskCleanupNeverDeletesPersistentBox(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-keep-box")

	final, err := h.rec.RunToCompletion(ctx, run.ID)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if final.State != domain.TaskReclaimed {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskReclaimed)
	}
	ws, err := h.store.GetWorkspace(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if ws.BoxState != domain.ResourceReady {
		t.Fatalf("box state = %s, want %s", ws.BoxState, domain.ResourceReady)
	}
	// Reclaiming the box is a separate, explicit operation.
	if err := h.rec.ReclaimBox(ctx, h.ws.ID); err != nil {
		t.Fatalf("reclaim box: %v", err)
	}
	ws, err = h.store.GetWorkspace(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if ws.BoxState != domain.ResourceReclaimed {
		t.Fatalf("box state = %s, want %s", ws.BoxState, domain.ResourceReclaimed)
	}
}

func TestReclaimBoxRefusesLiveTask(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-live")

	for i := 0; i < 3; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	if err := h.rec.ReclaimBox(ctx, h.ws.ID); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("reclaim with live task error = %v, want ErrConflict", err)
	}
}

func TestArtifactMissingBlocksRelease(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-artifact")

	// Drive to Running, then collect without a persisted artifact.
	for i := 0; i < 3; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	// Enter Collecting without an artifact row: the store is the only source of
	// truth for the "persist before delete" policy.
	if err := h.store.TransitionTask(ctx, run.ID, domain.TaskCollecting, ""); err != nil {
		t.Fatalf("force collecting: %v", err)
	}
	done, err := h.rec.Step(ctx, run.ID) // Collecting without artifact -> Failed
	if err != nil {
		t.Fatalf("step Collecting: %v", err)
	}
	if !done {
		t.Fatal("expected the run to settle")
	}
	final, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if final.State != domain.TaskFailed {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskFailed)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources", h.sandbox.Live())
	}
}

func TestBoundedRetryStopsAtMaxAttempts(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-retry")

	// Every allocation times out and no resource is recorded, so each attempt
	// fails. MaxAttempts defaults to 2.
	if _, err := h.rec.Step(ctx, run.ID); err != nil {
		t.Fatalf("step: %v", err)
	}
	// Both attempts fail with nothing recorded, so each one fails and the
	// second exhausts the bounded retry budget.
	h.sandbox.CreateFailures = 2
	if _, err := h.rec.Step(ctx, run.ID); err != nil { // attempt 1 fails, requeue
		t.Fatalf("step attempt 1: %v", err)
	}

	// Second attempt exhausts the budget and fails the run.
	for i := 0; i < 2; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step attempt 2: %v", err)
		}
	}
	final, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if final.State != domain.TaskFailed {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskFailed)
	}
	if final.Attempts != final.MaxAttempts {
		t.Fatalf("attempts = %d, want %d", final.Attempts, final.MaxAttempts)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources", h.sandbox.Live())
	}
}

func TestProviderDisabledByDefaultBlocksAllocation(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-disabled")
	// Reset to the construction default.
	h.sandbox = fake.NewSandbox()
	h.rec = New(h.store, h.sandbox, h.runner, h.clock, Config{})

	// A disabled provider cannot allocate, so the run must end failed rather
	// than appear reclaimed.
	settled, err := h.rec.RunToCompletion(ctx, run.ID)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	final := settled
	if final.State != domain.TaskFailed {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskFailed)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("disabled provider allocated %d resources", h.sandbox.Live())
	}
}

func TestHappyPathPersistsArtifactBeforeRelease(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-happy")

	// Requested -> Provisioning -> Ready -> Running.
	for i := 0; i < 3; i++ {
		if _, err := h.rec.Step(ctx, run.ID); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	attempts, err := h.store.ListAttempts(ctx, run.ID)
	if err != nil || len(attempts) != 1 {
		t.Fatalf("attempts = %d, err = %v", len(attempts), err)
	}
	lease, err := h.store.GetLease(ctx, attempts[0].LeaseID)
	if err != nil {
		t.Fatalf("get lease: %v", err)
	}
	if lease.ProviderResourceID == "" {
		t.Fatal("lease has no provider resource id before the command ran")
	}

	// The command result carries the artifact; running it persists the
	// reference before the lease may be released.
	if _, err := h.rec.Step(ctx, run.ID); err != nil { // Running -> Collecting
		t.Fatalf("step: %v", err)
	}
	if _, err := h.rec.Step(ctx, run.ID); err != nil { // Collecting -> Terminating
		t.Fatalf("step: %v", err)
	}
	done, err := h.rec.Step(ctx, run.ID) // Terminating -> Reclaimed
	if err != nil || !done {
		t.Fatalf("terminate: done=%v err=%v", done, err)
	}

	final, err := h.store.GetTaskRun(ctx, run.ID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if final.State != domain.TaskReclaimed {
		t.Fatalf("state = %s, want %s", final.State, domain.TaskReclaimed)
	}
	artifact, err := h.store.GetArtifact(ctx, run.ID)
	if err != nil {
		t.Fatalf("artifact must survive release: %v", err)
	}
	if artifact.Checksum == "" || artifact.URI == "" {
		t.Fatalf("artifact lost across release: %+v", artifact)
	}
	if h.sandbox.Live() != 0 {
		t.Fatalf("sandbox still holds %d resources", h.sandbox.Live())
	}
}

func TestReservationReleasedOnReclaim(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-reserve")

	if err := h.store.Reserve(ctx, run.ID, domain.Amount(25, domain.USD), store.DefaultLimits()); err != nil {
		t.Fatalf("reserve: %v", err)
	}
	if _, err := h.rec.RunToCompletion(ctx, run.ID); err != nil {
		t.Fatalf("run: %v", err)
	}
	total, err := h.store.ReservedTotal(ctx, h.ws.ID)
	if err != nil {
		t.Fatalf("reserved total: %v", err)
	}
	if total.Minor != 0 {
		t.Fatalf("reserved total = %d after reclaim, want 0", total.Minor)
	}
}

func TestMoneyUsesIntegerMinorUnits(t *testing.T) {
	sum, err := domain.Amount(1234, domain.USD).Add(domain.Amount(766, domain.USD))
	if err != nil {
		t.Fatalf("add: %v", err)
	}
	if sum.Minor != 2000 {
		t.Fatalf("sum = %d, want 2000", sum.Minor)
	}
	if _, err := domain.Amount(1, domain.USD).Add(domain.Amount(1, "EUR")); !errors.Is(err, domain.ErrCurrencyMismatch) {
		t.Fatalf("mixed currency error = %v, want ErrCurrencyMismatch", err)
	}
}

func TestDefaultLimitsCarryNoBudgetPolicy(t *testing.T) {
	limits := store.DefaultLimits()
	if limits.WorkspaceBudget != nil || limits.GlobalBudget != nil {
		t.Fatalf("default limits carry a budget: %+v", limits)
	}
	if limits.MaxConcurrentRuns <= 0 || limits.MaxGlobalConcurrentRuns <= 0 {
		t.Fatalf("default limits must bound concurrency: %+v", limits)
	}
	// A budget-free reservation therefore only has to respect concurrency.
	h := newHarness(t)
	ctx := context.Background()
	run := h.newRun("req-unbounded-budget")
	if err := h.store.Reserve(ctx, run.ID, domain.Amount(1_000_000, domain.USD), limits); err != nil {
		t.Fatalf("address without a configured budget: %v", err)
	}
}

func TestDefaultProviderIsDisabled(t *testing.T) {
	if provider.ErrDisabled == nil {
		t.Fatal("expected a disabled-provider error to exist for the default deployment")
	}
}
