package store_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/PsiACE/anylings/services/manager/audit"
	"github.com/PsiACE/anylings/services/manager/clock"
	"github.com/PsiACE/anylings/services/manager/domain"
	"github.com/PsiACE/anylings/services/manager/store"
)

var epoch = time.Date(2026, 11, 12, 3, 0, 0, 0, time.UTC)

func open(t *testing.T) (*store.Store, *clock.Fake, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "manager.db")
	c := clock.NewFake(epoch)
	s, err := store.Open(context.Background(), store.Options{Path: path, Clock: c})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s, c, path
}

func TestTimestampsArePersistedInUTC(t *testing.T) {
	s, c, _ := open(t)
	ctx := context.Background()

	// A non-UTC clock value must be stored as UTC, not as local time.
	c.Set(epoch.In(time.FixedZone("CST", 8*3600)))
	ws, err := s.CreateWorkspace(ctx, "utc", "fake")
	if err != nil {
		t.Fatalf("create workspace: %v", err)
	}
	loaded, err := s.GetWorkspace(ctx, ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if loaded.CreatedAt.Location() != time.UTC {
		t.Fatalf("created at location = %v, want UTC", loaded.CreatedAt.Location())
	}
	if !loaded.CreatedAt.Equal(epoch) {
		t.Fatalf("created at = %v, want %v", loaded.CreatedAt, epoch)
	}
}

func TestBoxLifecycleIsSeparateFromTaskOutcome(t *testing.T) {
	s, _, _ := open(t)
	ctx := context.Background()

	ws, err := s.CreateWorkspace(ctx, "separate", "fake")
	if err != nil {
		t.Fatalf("create workspace: %v", err)
	}
	if ws.BoxState != domain.ResourceRequested {
		t.Fatalf("box state = %s, want %s", ws.BoxState, domain.ResourceRequested)
	}
	// Reclaimed cannot be re-entered from the start state's successors either.
	if err := s.TransitionBox(ctx, ws.ID, domain.ResourceReady, "", ""); !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("Requested->Ready error = %v, want ErrInvalidTransition", err)
	}
	if err := s.TransitionBox(ctx, ws.ID, domain.ResourceProvisioning, "", ""); err != nil {
		t.Fatalf("provisioning: %v", err)
	}
	if err := s.TransitionBox(ctx, ws.ID, domain.ResourceReady, "box-9", ""); err != nil {
		t.Fatalf("ready: %v", err)
	}
	reloaded, err := s.GetWorkspace(ctx, ws.ID)
	if err != nil {
		t.Fatalf("get workspace: %v", err)
	}
	if reloaded.ProviderResourceID != "box-9" {
		t.Fatalf("provider resource id = %q, want box-9", reloaded.ProviderResourceID)
	}
	if err := s.TransitionBox(ctx, ws.ID, domain.ResourceReclaimed, "", ""); err != nil {
		t.Fatalf("reclaim: %v", err)
	}
	if err := s.TransitionBox(ctx, ws.ID, domain.ResourceReady, "", ""); !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("Reclaimed->Ready error = %v, want ErrInvalidTransition", err)
	}
}

func TestAttemptOnlyAdvancesForward(t *testing.T) {
	s, _, _ := open(t)
	ctx := context.Background()

	ws, err := s.CreateWorkspace(ctx, "attempts", "fake")
	if err != nil {
		t.Fatalf("create workspace: %v", err)
	}
	run, _, err := s.RequestTask(ctx, store.RequestTask{
		WorkspaceID: ws.ID,
		RequestID:   "req-attempt",
		Command:     "true",
		MaxAttempts: 3,
		Deadline:    epoch.Add(time.Hour),
	})
	if err != nil {
		t.Fatalf("request task: %v", err)
	}
	attempt, err := s.StartAttempt(ctx, run.ID, 1, domain.SandboxLease{Provider: "fake"})
	if err != nil {
		t.Fatalf("start attempt: %v", err)
	}
	if err := s.TransitionAttempt(ctx, attempt.ID, domain.AttemptSucceeded, ""); err != nil {
		t.Fatalf("succeed attempt: %v", err)
	}
	if err := s.TransitionAttempt(ctx, attempt.ID, domain.AttemptRunning, ""); !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("Succeeded->Running error = %v, want ErrInvalidTransition", err)
	}
	// A heartbeat on a terminal attempt is rejected: a stale worker cannot
	// revive its lease by reporting liveness.
	if err := s.RecordHeartbeat(ctx, attempt.ID); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("heartbeat on terminal attempt error = %v, want ErrConflict", err)
	}
}

func TestAuditAppendsDenialsAndCleanupWithoutUpdating(t *testing.T) {
	s, c, _ := open(t)
	ctx := context.Background()
	log := audit.NewLog(s.DB(), c)

	events := []audit.Event{
		{Actor: "reconciler", Action: "reserve", Target: "run_1", CorrelationID: "corr-1", Outcome: audit.Denied, Detail: "quota"},
		{Actor: "reconciler", Action: "release", Target: "run_1", CorrelationID: "corr-1", Outcome: audit.Cleaned},
	}
	for _, e := range events {
		if err := log.Append(ctx, e); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
	stored, err := log.List(ctx, "run_1")
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(stored) != 2 {
		t.Fatalf("audit rows = %d, want 2", len(stored))
	}
	if stored[0].Outcome != audit.Denied {
		t.Fatalf("first outcome = %s, want %s", stored[0].Outcome, audit.Denied)
	}
	if !stored[0].At.Equal(epoch) {
		t.Fatalf("audit time = %v, want %v", stored[0].At, epoch)
	}
	// The API exposes no update or delete, so history is append-only.
	if err := log.Append(ctx, audit.Event{Action: "release", Target: "run_1"}); err == nil {
		t.Fatal("append accepted an event without actor and correlation id")
	}
}
