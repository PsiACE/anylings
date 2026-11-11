# manager

Task and sandbox lifecycle for the anylings platform.

A **workspace** owns a persistent box. A **task run** executes one command and
uses an ephemeral sandbox lease that is released when the run ends. Releasing a
lease never touches the box.

This increment ships the domain model, a SQLite store, a reconciler and a fake
provider. There is no HTTP API, no UI and no cloud provider adapter.

## Task lifecycle

```
Requested -> Provisioning -> Ready -> Running -> Collecting -> Terminating -> Reclaimed
```

`Failed` and `Interrupted` are reachable from any non-terminal state. `Reclaimed`
means the run finished and its lease was released; it says nothing about the
box, which stays until `ReclaimBox` is called explicitly. `ReclaimBox` refuses
to run while the workspace still has a live task.

## Usage

```go
st, err := store.Open(ctx, store.Options{Path: "manager.db"})
defer st.Close()

ws, err := st.CreateWorkspace(ctx, "primary", "fake")
run, created, err := st.RequestTask(ctx, store.RequestTask{
    WorkspaceID: ws.ID,
    RequestID:   "deploy-42",   // repeated requests return this same run
    Command:     "make test",
    MaxAttempts: 2,
    Deadline:    time.Now().Add(30 * time.Minute).UTC(),
})

rec := reconcile.New(st, sandbox, runner, clock.System{}, reconcile.Config{})
final, err := rec.RunToCompletion(ctx, run.ID)
```

Cancellation and recovery act on persisted state only:

```go
err = rec.Cancel(ctx, run.ID)      // Interrupted, lease released
report, err := rec.Sweep(ctx)      // resume or interrupt after a restart
```

Run the tests with `go test -race ./...` from this directory. CI runs the same
command.

## Behaviour and limits

- **Idempotent requests.** `(workspace, request_id)` is unique, so a retried
  request returns the original run instead of starting a second one.
- **Idempotent allocation.** The provider resource name derives from the lease
  id. A create that times out is probed, not allocated twice.
- **Persist before release.** The artifact reference and checksum are stored
  before a run may release its lease. A run without them fails rather than
  releasing.
- **Failure is visible.** A lease whose release fails becomes `Stranded`, keeps
  its error text, and is retried by `Sweep`.
- **Recovery is durable.** `Sweep` reads the database, so a killed process
  leaves resumable work. Nothing depends on a `finally` block.
- **Bounded work.** Attempts, task runtime and lease runtime are all bounded by
  `Config`.
- **Holds are temporary.** A terminal run releases its admission hold in the
  same transaction, unless one of its leases is still unreleased. A retained
  hold is returned as soon as a later pass confirms cleanup.

## Configuration

| Field | Default | Meaning |
| --- | --- | --- |
| `MaxAttempts` | 2 | attempts per task run |
| `TaskMaxRuntime` | 30m | wall clock a run may take before it fails |
| `LeaseMaxRuntime` | 15m | wall clock a lease may exist |
| `HeartbeatGrace` | 90s | silence before an attempt is treated as lost |

Admission limits live in `store.Limits`:

| Field | Default | Meaning |
| --- | --- | --- |
| `MaxConcurrentRuns` | 2 | live runs per workspace |
| `MaxGlobalConcurrentRuns` | 8 | live runs across workspaces |
| `WorkspaceBudget` | unset | optional reserved-amount ceiling |
| `GlobalBudget` | unset | optional total ceiling |

The defaults bound concurrency only. Budgets are unset until an operator
supplies them; no amount is built into the code. All amounts are integer minor
units with an explicit currency.

## Providers

The only provider in this increment is in-process (`provider/fake`). It performs
no network I/O and spends nothing. A provider is enabled by the caller, so the
zero value allocates nothing.
