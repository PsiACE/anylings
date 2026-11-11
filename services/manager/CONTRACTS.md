# manager: lifecycle contracts and gaps

Scope of this increment: domain, store, reconciler and a fake provider with a
fake clock. There is no HTTP API, no UI and no cloud provider adapter.

## Persistent vs ephemeral

| Object           | Lifetime                                 | Owner                             |
| ---------------- | ---------------------------------------- | --------------------------------- |
| `Workspace`/box  | durable, reusable across task runs       | `ResourceLifecycle`               |
| `TaskRun`        | one unit of work, bounded by `Deadline`  | `TaskState`                       |
| `Attempt`        | one bounded try, bounded retry budget    | `AttemptState`                    |
| `SandboxLease`   | one provider allocation, released on end | `LeaseState`                      |

Task outcome (`Reclaimed`, `Failed`, `Interrupted`) never implies box deletion.
`Reconciler.ReclaimBox` is a separate call and refuses to run while the
workspace still has a live task.

## Task lifecycle

`Requested -> Provisioning -> Ready -> Running -> Collecting -> Terminating ->
Reclaimed`, with `Failed` and `Interrupted` reachable from any non-terminal
state. The allowed edges live in one table in `domain/state.go`; the store
rejects any other edge before writing.

## Durability and idempotency

- One pinned driver, `modernc.org/sqlite`, one database file. There is no JSON
  file store and no in-memory concurrency path.
- `task_runs` has a unique `(workspace_id, request_id)` index, so a repeated
  request returns the original run.
- The provider resource name is derived from the lease id, so a retried
  allocate returns the same resource. A create timeout is probed rather than
  re-allocated.
- The artifact reference and checksum are persisted before `Collecting` may
  advance; a run without a persisted artifact fails instead of releasing its
  lease.
- Recovery reads only the database (`Reconciler.Recover`), so a killed process
  leaves resumable work. No `finally`-only cleanup.

## Budget and quota

`Store.Reserve` checks concurrency and budget and inserts the hold inside one
immediate transaction. `$24.00` per month is the fixed planning baseline
(`domain.MonthlyBaseline`); it is a reservation target, not a provider billing
cap. Elastic budget is not approved and is disabled in code.

## Provider policy

The default is disabled and spends nothing. The only provider in this increment
is in-process and performs no network I/O. A future DigitalOcean adapter is
on-demand CPU only; spot and other clouds are out of scope.

## Known gaps

- No HTTP/API adapter yet, so nothing external can request a run.
- The reconciler is driven by an explicit `Sweep` call, not a running service;
  no background loop, heartbeat ticker or lease renewal is implemented.
- The command runner is a fake that reports a synthetic artifact checksum over
  the run identity. It does not execute anything and produces no real bytes.
- Lease renewal before expiry is not implemented; an over-run lease is
  interrupted by the janitor rather than extended.
- Artifact retry is not implemented: there is no re-collection attempt after a
  failed artifact persist, only a bounded path to `Failed`.
- Per-workspace budget and concurrency are enforced; global limits exist but
  have no per-tenant fairness.
- The audit table is append-only through its API, but SQLite is not
  tamper-proof storage.
- Runtime footprint (memory, disk) is unmeasured.

## Later Bub adapter contract

`provide_environment(session_id, workspace)` in Bub is process-scoped and has
no per-session release, so the manager must expose:

- a request that names the effective workspace explicitly, not the process cwd;
- a release call per session, so a lease does not have to live until process
  exit;
- fail-closed behaviour: an unavailable provider ends the turn rather than
  falling back to a local environment.

Cloud API keys stay in the provider adapter, never in the worker environment.
