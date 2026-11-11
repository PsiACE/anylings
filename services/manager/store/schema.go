package store

// schema is applied at open time. Each statement is idempotent so a reopen
// simply confirms the existing shape.
//
// Design notes that the tests depend on:
//   - task_runs has a unique (workspace_id, request_id) key, so a duplicated
//     request returns the original run instead of creating a second one.
//   - artifact_refs is a separate row keyed by task run: a run may only be
//     released once its artifact reference and checksum are persisted.
//   - reservations hold budget and concurrency in one table, counted inside the
//     same transaction that inserts them.
const schema = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workspaces (
	id                   TEXT PRIMARY KEY,
	name                 TEXT NOT NULL,
	provider             TEXT NOT NULL,
	provider_resource_id TEXT NOT NULL DEFAULT '',
	box_state            TEXT NOT NULL,
	last_error           TEXT NOT NULL DEFAULT '',
	created_at           TEXT NOT NULL,
	updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
	id           TEXT PRIMARY KEY,
	workspace_id TEXT NOT NULL REFERENCES workspaces(id),
	request_id   TEXT NOT NULL,
	command      TEXT NOT NULL,
	state        TEXT NOT NULL,
	attempts     INTEGER NOT NULL DEFAULT 0,
	max_attempts INTEGER NOT NULL,
	deadline     TEXT NOT NULL,
	outcome      TEXT NOT NULL DEFAULT '',
	created_at   TEXT NOT NULL,
	updated_at   TEXT NOT NULL,
	finished_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS task_runs_request
	ON task_runs(workspace_id, request_id);
CREATE INDEX IF NOT EXISTS task_runs_state ON task_runs(state);

CREATE TABLE IF NOT EXISTS attempts (
	id           TEXT PRIMARY KEY,
	task_run_id  TEXT NOT NULL REFERENCES task_runs(id),
	number       INTEGER NOT NULL,
	state        TEXT NOT NULL,
	lease_id     TEXT NOT NULL DEFAULT '',
	heartbeat_at TEXT NOT NULL,
	started_at   TEXT NOT NULL,
	ended_at     TEXT,
	failure      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_number ON attempts(task_run_id, number);

CREATE TABLE IF NOT EXISTS leases (
	id                   TEXT PRIMARY KEY,
	attempt_id           TEXT NOT NULL REFERENCES attempts(id),
	provider             TEXT NOT NULL,
	provider_resource_id TEXT NOT NULL DEFAULT '',
	state                TEXT NOT NULL,
	last_error           TEXT NOT NULL DEFAULT '',
	created_at           TEXT NOT NULL,
	released_at          TEXT
);
CREATE INDEX IF NOT EXISTS leases_state ON leases(state);

CREATE TABLE IF NOT EXISTS artifact_refs (
	task_run_id TEXT PRIMARY KEY REFERENCES task_runs(id),
	artifact_id TEXT NOT NULL,
	uri         TEXT NOT NULL,
	checksum    TEXT NOT NULL,
	size_bytes  INTEGER NOT NULL,
	created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
	id           TEXT PRIMARY KEY,
	scope        TEXT NOT NULL,
	workspace_id TEXT NOT NULL,
	currency     TEXT NOT NULL,
	minor        INTEGER NOT NULL,
	released     INTEGER NOT NULL DEFAULT 0,
	request_id   TEXT NOT NULL DEFAULT '',
	created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_live
	ON reservations(workspace_id, released);
CREATE UNIQUE INDEX IF NOT EXISTS reservations_request
	ON reservations(scope, request_id) WHERE request_id <> '';

CREATE TABLE IF NOT EXISTS audit_events (
	seq            INTEGER PRIMARY KEY AUTOINCREMENT,
	actor          TEXT NOT NULL,
	action         TEXT NOT NULL,
	target         TEXT NOT NULL,
	correlation_id TEXT NOT NULL,
	outcome        TEXT NOT NULL,
	detail         TEXT NOT NULL DEFAULT '',
	at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_target ON audit_events(target);
`
