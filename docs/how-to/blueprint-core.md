# Work on the blueprint core

How to build, test and extend the shared platform core: the versioned contracts, the
SQLite state layer and the Spin application manifest. Read the
[contract explanation](../explanation/blueprint-contracts.md) first; this page is the
task recipe.

## Layout

```text
platform/contracts/v1/*.schema.json   published contracts (source of truth)
platform/contracts/limits.json        the single bounds instance
platform/contracts/fixtures/*.json    valid and invalid examples, used by tests
platform/core/crates/blueprint-contracts   types + envelope/lease/health rules
platform/core/crates/blueprint-state       SQLite schema, migrations, transactions
platform/core/crates/blueprint-service     HTTP routing, authorization, reply shape
platform/core/crates/spin-core             Wasm adapter onto Spin's HTTP types
platform/spin.toml                         the one shared Spin manifest
tests/blueprint/core/check.sh              offline check entrypoint
```

`blueprint-service` deliberately has no Wasm or socket types. That is what makes the
request contract testable on a workstation: `spin-core` only translates.

## Run the offline checks

```bash
bash tests/blueprint/core/check.sh
```

It formats, lints with `-D warnings`, and runs every test against the committed
lockfile. `CARGO_HOME` defaults to a gitignored `.local/cargo-home`, so nothing installs
into `HOME`.

Run a single area while iterating:

```bash
cargo test --manifest-path platform/core/Cargo.toml -p blueprint-state
cargo test --manifest-path platform/core/Cargo.toml -p blueprint-service
cargo test --manifest-path platform/core/Cargo.toml -p blueprint-contracts
```

To skip the resolver reaching the network, add `--offline`; the lockfile is complete.

## Change a contract

1. Edit the schema in `platform/contracts/v1/`. Bump `$id` and `schemaVersion` only for a
   breaking change; otherwise keep version `1` and treat it as a compatible addition.
2. Edit the matching Rust type in `blueprint-contracts`. Wire names with acronyms
   (`eventID`, `taskID`, `workerID`, `checkID`, `traceID`, `spanID`, `requestID`) must be
   explicit `#[serde(rename = ...)]`; `rename_all = "camelCase"` renders them as `Id`,
   which is a different contract.
3. Add or update a fixture in `platform/contracts/fixtures/`, including a negative case
   when the change adds a rule.
4. Run the checks. `schema_conformance` validates fixtures against the published schema
   _and_ parses them through the Rust type, so a one-sided change fails.

Change a bound in `platform/contracts/limits.json` only. The state layer compiles the
same numbers and asserts equality at startup; editing the document without the code is
caught by `limits_fixture_matches_the_document_the_code_compiles` and by
`Store::open`.

## Change the database schema

Migrations are forward-only and append-only.

1. Add `platform/core/crates/blueprint-state/migrations/000N_<name>.sql`. Do not edit an
   applied file.
2. Add it to `MIGRATIONS` in `crates/blueprint-state/src/migrate.rs` and raise
   `SCHEMA_VERSION`.
3. Do **not** put connection pragmas in a migration: it runs inside a transaction and
   SQLite refuses `PRAGMA synchronous` there. Pragma policy belongs in
   `Store::configure`.
4. Add a behaviour test that proves the new guarantee; the restart tests in
   `tests/restart_durability.rs` show the pattern.

## Storage facts you must not break

- **One writer.** Exactly one connection writes; there is no pool. Concurrency is
  serialized in the application, not by the database.
- **`BEGIN IMMEDIATE` for any read-then-write.** A deferred transaction lets two
  claimers both read `submitted`. `claim_next`, `heartbeat`, `cancel_task`,
  `report_task` and `expire_lease` all take the write lock first.
- **`synchronous = FULL`.** An acknowledged write is on disk before the reply.
- **The file mode is the core's job.** SQLite creates `state.db` `0644` regardless of a
  private directory, so `Store::open` narrows it to `0600`.
- **Checkpoint before copying.** `checkpoint_truncate()` empties the WAL so a directory
  copy has no growing sidecar.

### Backup and restore startup contract (for the Litestream owner in P2)

Nothing in this phase performs a backup. The sequence the restore path must satisfy:

1. The previous process has exited; no other writer holds the file.
2. Restore writes `state.db`; remove any `state.db-wal`/`state.db-shm` left by the
   restore, because a stale WAL replays against a different database.
3. Open the database, apply pragmas, then serve — in that order.
4. Start replication after the file is open, so the replica sees a migrated database.
5. Verify before serving: `Store::check_integrity()` plus the expected schema version.

`a_copied_checkpointed_database_restores_into_a_new_directory` in
`tests/restart_durability.rs` exercises exactly these steps against a real file.

## Build the Spin component

The manifest `platform/spin.toml` is the reviewed artifact; the compiled module is a
build output and is not committed.

```bash
rustup target add wasm32-wasip2
cd platform/core
cargo build -p spin-core --release --target wasm32-wasip2
```

The module lands at `platform/core/target/wasm32-wasip2/release/spin_core.wasm`, which is
exactly what the manifest's `source` names. Spin resolves that path against the
manifest's own directory, so it is `core/target/...` rather than `platform/core/...`.
`tests/blueprint/core/check.sh` asserts that the path stays inside the repository, because
a `../` slip there is invisible until `spin up` fails.

## What is NOT-RUN here, and why

This phase could not execute the Spin component, and does not claim to.

1. **No Wasm target and no Spin CLI on this host.** `rustup target list --installed`
   reports `wasm32-unknown-unknown`, `x86_64-unknown-linux-gnu` and
   `x86_64-unknown-linux-musl`; `spin` and `wasmtime` are absent from `PATH`. Adding a
   target or installing a CLI changes the host toolchain, which this phase may not do.
   `spin-sdk` builds only for wasm, so it is a `cfg(target_family = "wasm")` dependency
   and off-Wasm `spin-core` compiles to a stub with no HTTP entry point. There is no
   native shim that answers HTTP and calls itself Spin: that would let a plain process
   impersonate the component under test.
2. **No Spin-SQLite executor in the state layer.** `spin_sdk::sqlite::Connection` is an
   async handle over a WIT interface, not a `rusqlite::Connection`, and the two cannot
   be swapped. Serving real requests needs the state layer's statements behind a second
   executor with the same `BEGIN IMMEDIATE` ordering. That executor is not written here,
   because unverifiable code that cannot be compiled or run would look finished without
   being finished. It is the first item in
   `docs/explanation/blueprint-task-map.md`.

Meanwhile the component refuses stateful routes with `503 unavailable` rather than
opening a different database or answering from memory. A visible refusal is honest; a
silent fallback would mean the running process is not the thing under test.

Consequently: contract conformance, state behaviour and the HTTP request contract are
`PASS` as native tests against real SQLite. Actual Spin runtime behaviour and any
Kubernetes integration are `NOT-RUN`.

## Deployment is not part of this phase

`platform/spin.toml` is a manifest, not a workload. Turning it into a Deployment, a
Service and an Argo Application is a separate change owned by the integration phase; this
phase proposes it in its report rather than editing `kubernetes/`. No SpinKube, operator,
cert-manager, RCM or KEDA is introduced.
