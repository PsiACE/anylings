-- Schema version 1 of the shared platform state database.
--
-- One database, one writer. The application opens it read-write on a single
-- replica; nothing else holds a write connection. WAL lets readers proceed
-- without blocking the writer, which is why the checkpoint contract below is
-- explicit rather than left to SQLite defaults.

-- The connection pragmas (journal_mode, synchronous, foreign_keys,
-- wal_autocheckpoint) are applied by `Store::configure` before this file runs.
-- They cannot live here: a migration executes inside a transaction, and SQLite
-- refuses to change its safety level there.

-- Applied once, by the migration runner, before any other statement.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Durable event dedup. The primary key is the idempotency key, so a redelivered
-- event is an INSERT conflict rather than a second business fact. The payload is
-- stored only after validation and redaction.
CREATE TABLE IF NOT EXISTS events (
    dedupe_key   TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL,
    source       TEXT NOT NULL,
    service      TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    severity     TEXT,
    occurred_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    trace_id     TEXT,
    span_id      TEXT,
    attributes   TEXT NOT NULL,
    CHECK (length(dedupe_key) BETWEEN 9 AND 300),
    CHECK (length(service) BETWEEN 1 AND 63)
);

CREATE INDEX IF NOT EXISTS events_by_service_time
    ON events (service, occurred_at);

-- Inbound dedup for requests that are not events: a retried POST with the same
-- request id is acknowledged once. `response_digest` lets a retry return the same
-- answer instead of performing the side effect twice.
CREATE TABLE IF NOT EXISTS request_dedup (
    request_key      TEXT PRIMARY KEY,
    principal        TEXT NOT NULL,
    operation        TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    response_digest  TEXT
);

-- Latest health state, one row per check.
CREATE TABLE IF NOT EXISTS health_checks (
    service                TEXT NOT NULL,
    check_id               TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    state                  TEXT NOT NULL,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    consecutive_successes  INTEGER NOT NULL DEFAULT 0,
    last_observed_at       TEXT,
    since                  TEXT,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (service, check_id),
    CHECK (state IN ('unknown', 'healthy', 'degraded', 'failed')),
    CHECK (consecutive_failures >= 0 AND consecutive_successes >= 0)
);

-- Immutable transition log. The id is deterministic over content, so a replayed
-- observation that would produce the same transition conflicts instead of
-- duplicating it, and a genuine recovery is a distinct row.
CREATE TABLE IF NOT EXISTS health_transitions (
    transition_id  TEXT PRIMARY KEY,
    service        TEXT NOT NULL,
    check_id       TEXT NOT NULL,
    from_state     TEXT NOT NULL,
    to_state       TEXT NOT NULL,
    at             TEXT NOT NULL,
    FOREIGN KEY (service, check_id) REFERENCES health_checks (service, check_id)
);

CREATE INDEX IF NOT EXISTS health_transitions_by_time
    ON health_transitions (at);

-- Notification state, keyed by (transition, channel). A delivered row is terminal,
-- so nothing re-pages for a transition that already went out.
CREATE TABLE IF NOT EXISTS notifications (
    dedupe_key        TEXT PRIMARY KEY,
    transition_id     TEXT NOT NULL,
    channel           TEXT NOT NULL,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    next_attempt_at   TEXT,
    updated_at        TEXT NOT NULL,
    UNIQUE (transition_id, channel),
    CHECK (state IN ('pending', 'delivered', 'failed', 'suppressed')),
    CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS notifications_pending
    ON notifications (next_attempt_at) WHERE state = 'pending';

-- Task records. `generation` is the claim generation and is what a heartbeat or
-- report must name; `lease_deadline` is authoritative for staleness.
CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    service          TEXT NOT NULL,
    kind             TEXT NOT NULL,
    idempotent       INTEGER NOT NULL CHECK (idempotent IN (0, 1)),
    state            TEXT NOT NULL,
    submitted_by     TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    params           TEXT NOT NULL,
    params_digest    TEXT NOT NULL,
    generation       INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    worker_id        TEXT,
    lease_deadline   TEXT,
    heartbeat_at     TEXT,
    outcome          TEXT,
    result_digest    TEXT,
    settled_at       TEXT,
    CHECK (state IN (
        'submitted', 'leased', 'cancel_requested', 'outcome_unknown',
        'succeeded', 'failed', 'cancelled', 'expired'
    )),
    CHECK (kind IN ('probe', 'query', 'archive', 'verify'))
);

CREATE INDEX IF NOT EXISTS tasks_claimable
    ON tasks (submitted_at) WHERE state = 'submitted';

CREATE INDEX IF NOT EXISTS tasks_by_service_state
    ON tasks (service, state);

-- Every accepted claim is recorded, so an audit can reconstruct who held what.
CREATE TABLE IF NOT EXISTS task_leases (
    task_id        TEXT NOT NULL,
    generation     INTEGER NOT NULL,
    worker_id      TEXT NOT NULL,
    granted_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    released_at    TEXT,
    release_reason TEXT,
    PRIMARY KEY (task_id, generation),
    FOREIGN KEY (task_id) REFERENCES tasks (task_id),
    CHECK (generation >= 1),
    CHECK (release_reason IS NULL OR release_reason IN (
        'reported', 'expired', 'cancelled', 'superseded'
    ))
);

-- Artifacts a worker uploaded. Recorded independently of the report, because an
-- object that arrives without an accepted report must be reconciled, not lost.
CREATE TABLE IF NOT EXISTS task_artifacts (
    artifact_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    name         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    uploaded_at  TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks (task_id),
    UNIQUE (task_id, name, digest)
);

CREATE INDEX IF NOT EXISTS task_artifacts_by_task
    ON task_artifacts (task_id);

-- Dataset catalog. Names are public; keys, prefixes and scopes are server-owned.
CREATE TABLE IF NOT EXISTS datasets (
    name              TEXT PRIMARY KEY,
    service           TEXT NOT NULL,
    signal            TEXT NOT NULL,
    retention_days    INTEGER NOT NULL,
    authorization_scope TEXT NOT NULL,
    max_scan_bytes    INTEGER,
    state             TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL,
    CHECK (signal IN ('metrics', 'logs', 'traces', 'events')),
    CHECK (state IN ('active', 'frozen', 'retired')),
    CHECK (retention_days > 0)
);

-- Upload batches. The unique key digest is what makes "the same batch" one row.
CREATE TABLE IF NOT EXISTS upload_batches (
    batch_id       TEXT PRIMARY KEY,
    key_digest     TEXT NOT NULL UNIQUE,
    dataset        TEXT NOT NULL,
    service        TEXT NOT NULL,
    signal         TEXT NOT NULL,
    window_start   TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    object_digest  TEXT,
    state          TEXT NOT NULL,
    bytes          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    FOREIGN KEY (dataset) REFERENCES datasets (name),
    CHECK (state IN ('pending', 'uploaded', 'committed', 'abandoned')),
    CHECK (bytes >= 0)
);

-- One pending slot per (dataset, service, signal, window, seq) is already implied
-- by the unique key digest; this index serves the pending-slot bound query.
CREATE INDEX IF NOT EXISTS upload_batches_pending
    ON upload_batches (state, expires_at);
