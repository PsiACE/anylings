# Blueprint contracts

The shared core publishes six versioned contracts and one bounds document. They are the
interface every later phase consumes, so they are machine-readable JSON Schema with
matching Rust types, not prose alone.

Schemas live in `platform/contracts/v1/`. Each carries a stable `$id` under
`https://anylings.invalid/contracts/v1/`, and `schemaVersion` is `"1"` everywhere. A
breaking change to a stored shape or an accepted request is a new directory, never an
in-place edit.

| Contract       | Schema                | Responsibility                                             |
| -------------- | --------------------- | ---------------------------------------------------------- |
| Event envelope | `event.schema.json`   | What a producer may publish, and its idempotency key       |
| Error          | `error.schema.json`   | The only non-2xx body shape, with a closed code set        |
| Limits         | `limits.schema.json`  | Every bound applied before a durable write                 |
| Dataset        | `dataset.schema.json` | Server-owned catalog, partitions, immutable upload batches |
| Task           | `task.schema.json`    | Lifecycle, claim lease generations, reports, artifacts     |
| Health         | `health.schema.json`  | Derived state, transition identity, notification dedup     |

`platform/contracts/limits.json` is the single instance of the limits schema. The state
layer compiles the same numbers and fails at startup if the document and the code
disagree, so a hand-edited bound cannot pass unnoticed.

## Why these particular rules

### Idempotency is a stored key, not a convention

An event's dedup key is `(source, eventID)`, or `(source, dedupeKey)` when a producer
supplies one, because some producers mint a fresh id on every delivery attempt. The key
is the primary key of the `events` table: a redelivery is an `INSERT` conflict, and the
caller is told `duplicate: true` with the original record untouched.

Requests that are not events use the same idea through `request_dedup`: the transport's
request id is the key, and the recorded response digest lets a retry converge on the
first answer instead of performing the side effect twice.

`source` is the authenticated principal, never a body field. A body claiming another
source is rejected with `unauthorized`, because `source` is part of the idempotency
namespace — trusting it would let one principal suppress or forge another's events.

### Privacy is enforced before storage

Attribute values whose key names a credential (`authorization`, `cookie`, `password`,
`secret`, `token`, `api_key`, `credential`, `private_key`) are replaced with
`[redacted]` during envelope validation, before the row exists. The key is kept so an
operator can see the field was present. Because redaction happens at ingest, no later
query can reveal what was dropped, and there is no "unredacted" table to leak.

Attributes are a flat string map, not nested JSON: one redaction pass over a flat map
covers every producer, whereas nested payloads need per-shape rules that would drift.

### Dataset access is catalog-by-name only

A caller sends a dataset _name_. The core resolves the storage key, partition layout and
required scope from the `datasets` table. No request field accepts a path, bucket,
prefix, SQL fragment or filesystem location — those are server-derived or absent.

`v1/dataset.schema.json` therefore has no `path`, `bucket`, `prefix` or `sql` property,
and the schema-conformance test asserts their absence rather than trusting review. The
one route family that could tempt a caller to send SQL (`/v1/query`) does not exist in
this phase at all; P3-Q adds it with a template-only parameter model.

Upload batches are immutable and bounded. A batch id (`b_<32 hex>`) and key digest are
derived from the batch descriptor, so re-announcing the same logical batch resolves to
one row via the unique `key_digest` column. A `pending` batch holds a reserved slot; it
expires to `abandoned` so a failed uploader cannot pin local capacity indefinitely.

### Task leases use generations, and expiry is not permission to re-run

Every successful claim increments `generation`. A heartbeat or report names the
generation it belongs to; a stale generation is refused. That is what stops a worker
whose lease expired from settling a task another worker now owns.

Expiry differs by declaration:

- `idempotent = true`: the lease expires back to `submitted`, and the next claim takes
  generation + 1. Re-running is safe because the work is declared repeatable.
- `idempotent = false`: the lease expires to `outcome_unknown`, which is terminal until
  an explicit reconcile call names what actually happened. The worker may already have
  caused the side effect; reclaiming automatically would duplicate it.

An uploaded artifact is recorded independently of the report. An object present with no
accepted report is therefore reconcilable: the artifact row says what exists, and
`outcome_unknown` says a human or a reconciler must decide. A report naming a
`resultDigest` that no artifact row holds is refused rather than accepted — the core
does not invent results.

A duplicate report returns the stored verdict with `duplicate: true`. It is a success,
not a conflict, so a converging worker can retry safely. A _different_ verdict for an
already settled task returns the stored state and writes nothing: the first accepted
report wins.

### Health transitions are content-addressed

Health is derived from a bounded window under an explicit policy (`degraded_after`,
`failed_after`, `healthy_after`), so one sample cannot flip state. A state change
produces one transition whose id is a deterministic function of
`(service, checkID, from, to, at)`. A replayed observation that would produce the same
transition conflicts instead of duplicating it, and a genuine recovery is a distinct row
with its own id.

Notifications key on `(transitionID, channel)` with a unique constraint. Delivery is
terminal, so nothing re-pages for a transition that already went out, and a transient
failure retries the same row under a bounded attempt count. A channel is a configured
label; a request cannot supply a recipient address or webhook URL.

### Unknown outcomes are never retryable at the error layer

`error.code = "outcome_unknown"` forces `retryable: false` in the schema itself, and
`unauthorized`/`forbidden_scope` are likewise non-retryable. The schema enforces this
with `if/then`, so a response that advertises an unknown side effect as safe to retry is
an invalid document, not a judgement call.

## Consumer rules

1. **Read the schema version first.** A document with an unknown `schemaVersion` is
   refused, not coerced.
2. **Never send a derived field.** Batch ids, task ids, digests, transition ids and
   dedup keys are server-computed. Sending one is a contract change, not a request.
3. **Treat `duplicate: true` as success.** It means the first result stands.
4. **A `lease_lost` reply is final for that generation.** Re-read the task; do not retry
   the same report against a new generation you do not hold.
5. **Do not parse `message`.** Branch on `code`.

## What this phase does not claim

The contracts are frozen and tested, and the state layer enforces them against real
SQLite. The Spin manifest is written, but no Spin runtime has executed this component
here; see `docs/how-to/blueprint-core.md` for the exact not-run boundary and the two
concrete blockers.
