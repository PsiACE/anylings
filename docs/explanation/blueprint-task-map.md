# Blueprint phase task map

Prepared briefs so the delivering session for each phase can start from the accepted P1
commit instead of inventing contracts. **Nothing here is dispatched or running.** The
program root owns dispatch and uses the accepted commit as the base
(`startFromOrigin: false`).

The single-writer rule: this phase owns `platform/contracts/**`, `platform/spin.toml` and
`platform/core/**`. Every later phase consumes those and requests changes through the
root rather than editing them in parallel.

## Frozen interface available to every phase

| Thing            | Location                                   | What a consumer may rely on                         |
| ---------------- | ------------------------------------------ | --------------------------------------------------- |
| Contracts        | `platform/contracts/v1/*.schema.json`      | `schemaVersion` `"1"`, stable `$id`                 |
| Bounds           | `platform/contracts/limits.json`           | The only place a number is chosen                   |
| Fixtures         | `platform/contracts/fixtures/`             | Valid and invalid examples to test against          |
| Types + rules    | `platform/core/crates/blueprint-contracts` | Envelope validation, lease lifecycle, health policy |
| State operations | `platform/core/crates/blueprint-state`     | Parameterized SQL, migrations, transactions         |
| Request contract | `platform/core/crates/blueprint-service`   | Routes, scopes, error shape, dedup                  |
| Manifest         | `platform/spin.toml`                       | One application, one catch-all component route      |

## P1 follow-up: the Spin-SQLite executor (blocking for runtime work)

This is the first item any later phase needs, and it belongs to the state layer, so it
stays with the shared-core owner unless the root reassigns it.

**Why it blocks.** `spin_sdk::sqlite::Connection` is async over WIT; the state layer is
synchronous `rusqlite`. The statements and their `BEGIN IMMEDIATE` ordering must be
expressed against a small trait so both executors serve one implementation rather than
two drifting copies.

**Shape.** Introduce a transaction trait with `execute`/`query_row`/`changes`, implement
it for `rusqlite::Connection`, then implement it for the Spin handle. Keep every rule in
one place: the trait implementations move rows, the decisions stay in the existing
modules.

**Acceptance.** The existing 63 tests keep passing on the native executor; the Spin
executor runs the same behaviour tests inside a Spin host on a developer machine; a
`spin up` of `platform/spin.toml` answers `/healthz` `204` and serves one event ingest
against a real Spin SQLite file.

**Explicitly out of scope.** No second database backend, no fallback to in-memory, no
async rewrite of the contract crate.

## P2 — telemetry, archive and recovery

**Unique paths.** `platform/core/crates/blueprint-telemetry/**`,
`platform/telemetry.toml` (a second Spin application manifest, not an edit to
`spin.toml`), `tests/blueprint/telemetry/**`, `docs/how-to/blueprint-telemetry.md`.

**Contracts it consumes.** `event` (ingest), `dataset` (catalog, partitions, immutable
batches), `limits` (`request`, `dataset`, `storage`).

**Boundaries.**

- Ingestion validates through the existing envelope type; it does not re-implement
  redaction. Sensitive-field handling already happened at the boundary.
- The durable buffer is a bounded local queue table with a configured row ceiling;
  overflow is a visible `queue_full` reply, never a silent drop and never unbounded disk.
- An upload batch is `pending` -> `uploaded` (object digest verified) -> `committed`
  (cataloged). Re-announcing resolves to the same `batch_id` via `key_digest`.
- Litestream backup/restore follows the startup contract in
  `docs/how-to/blueprint-core.md`, including "remove stale sidecars before opening" and
  "verify with `check_integrity` before serving".

**Tests.** Simulated S3 outage with the buffer at its bound; duplicate upload of the
same batch; a batch abandoned by expiry releasing its slot; restore into a new directory
verified by integrity check and a dedup assertion; assertion that no credential value
appears in an archived object.

**Acceptance dependency.** P1 contracts (met). Local emulator only — no real R2
credential, no paid resource.

## P3-Q — bounded DuckDB query

**Unique paths.** `platform/core/crates/blueprint-query/**`, `tests/blueprint/query/**`,
`docs/how-to/blueprint-query.md`.

**Contracts it consumes.** `dataset` (name -> key resolution and scope), `limits`
(`query`), `error` (`forbidden_scope`, `queue_full`).

**Boundaries.**

- The API accepts a dataset _name_ and a template id. It never accepts SQL text or an
  object path. Templates are server-side constants with named parameters.
- Concurrency is `1` (`query.maxConcurrency` is a `const` in the schema, so raising it is
  a contract change).
- Memory, scan, time and result bounds come from `limits.query` and `limits.result`.
- Cancellation terminates the engine, not just the HTTP request.
- Heavy queries become `kind = "query"` tasks through the existing task contract rather
  than blocking an interactive request.

**Tests.** Parameter-injection attempts rejected; a dataset outside the principal's
scope refused (not filtered); a timeout that actually stops execution (assert the engine
is idle afterwards); result truncation is an explicit error, not a short page; concurrent
interactive query returns `queue_full` instead of queueing behind the heavy one.

**Acceptance dependency.** P1 contracts and P2 dataset catalog rows.

## P3-UI — static Perses surface

**Unique paths.** `platform/ui/**`, `kubernetes/apps/**` only via a proposed
integration change, `docs/how-to/blueprint-ui.md`.

**Contracts it consumes.** `dataset` (names only), `error`, the P3-Q query API shape.

**Boundaries.**

- Static assets and versioned dashboard JSON; no resident Perses backend.
- One custom plugin package providing the data source, variables and correlation links.
- The plugin sends a dataset name and template id. It never sends SQL, and it never
  decides authorization: the server does, and a front-end filter is not a control.
- Correlation links use `traceID`/`spanID` from the envelope, so a request can be
  followed from metric to log to span.

**Tests.** A dashboard renders from a fixture response; an out-of-scope dataset request
is refused by the server even when the UI asks for it; a dropped front-end filter does
not widen results.

**Acceptance dependency.** Frozen query API shape. Can run in parallel with P3-Q once
that shape is agreed.

## P4 — health loop, notifications and a local worker

**Unique paths.** `platform/core/crates/blueprint-health/**`,
`platform/core/crates/blueprint-worker/**`, `tests/blueprint/health/**`,
`docs/how-to/blueprint-health.md`.

**Contracts it consumes.** `health`, `event`, `task`.

**Boundaries.**

- Probe results become observations; state derivation and transition identity stay in
  `blueprint-contracts`. The loop does not re-derive state with its own thresholds.
- Notification delivery targets a capture sink. No real recipient, no external webhook,
  no model call.
- One scheduler entry point finds due work and submits `kind = "probe"|"verify"` tasks
  through the task contract. No CronJob per rule.
- A local worker implements claim -> heartbeat -> execute -> upload artifact -> report,
  including interruption, cancellation, duplicate report and `outcome_unknown` paths.
  It never treats expiry as permission to re-run non-idempotent work.

**Tests.** Deterministic failure then recovery producing exactly two transitions and two
notifications; a redelivered transition producing none; a worker killed mid-execution
leaving `outcome_unknown` and requiring explicit reconcile; duplicate report converging;
cancel observed by a live worker and recorded.

**Acceptance dependency.** P1 contracts and P2 event/state availability for the capture
sink.

## P5 — integrated proof on local k0s

**Owner.** The P0 session, reusing its cluster and harness; this is not a new cluster
owner.

**Paths.** `tests/k0s/blueprint/**` (P0 owns), `docs/how-to/blueprint-k0s.md`.

**What it must prove.** One real failing request traced from metric to log to span; a
storage-backed query answering through the UI; an R2-emulator outage filling the bound
and recovering; cancellation actually stopping execution; a restart whose state survives;
restore from a backup into a new local volume; pod count, WSS, RSS, CPU and backlog
recorded for the old and new stack on the same node with the same load.

**Explicit non-claims.** Local-path storage is not DO CSI; a local S3 emulator is not
R2; local worker success is not cloud elasticity. Production replacement stays a
separate promotion decision.

## Dependency order

```text
P1 (this commit)
 ├── Spin-SQLite executor ──> runtime evidence for any phase
 ├── P2 telemetry/archive ──> P3-Q dataset rows ──> P3-UI
 └── P4 health/worker ─────────────────────────────> P5 integrated proof
```

P3-Q and P3-UI may run concurrently once the query API shape is frozen. P5 needs P1–P4
accepted commits, not branches in progress.

## Gates this map does not satisfy

Nothing here starts a session, authorizes production promotion, or substitutes for R0
and P0. The local k0s rehearsal and PR26/27 deployment remain with their existing owners,
and no claim in this repository asserts production or k0s proof from host tests.
