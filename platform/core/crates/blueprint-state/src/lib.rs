//! SQLite state for the shared platform core.
//!
//! One database file, one writable owner. The application opens it once per process
//! and serializes writes through a single connection. `journal_mode = WAL` lets
//! readers proceed while a writer holds the write lock, and the checkpoint contract
//! below is what Litestream will consume later.
//!
//! # Durability contract
//!
//! - `synchronous = FULL` on the writer. A committed transaction is on disk before
//!   the reply is produced, so a crash cannot lose an acknowledged write.
//! - `wal_autocheckpoint` is pinned from the limits document rather than left to the
//!   SQLite default, so WAL growth is a known number and not a surprise on a small
//!   volume.
//! - Side effects (object uploads, notifications) are recorded in their own state
//!   rows and are never claimed as done by a transaction that has not committed.
//!
//! # Backup and restore startup contract (implemented by P2)
//!
//! Litestream, when added, must satisfy this exact sequence; nothing in this crate
//! pretends to perform a backup:
//!
//! 1. The previous process has exited and no other writer holds the database.
//! 2. Restore writes `state.db`, and any `state.db-wal`/`state.db-shm` present after
//!    restore are removed before the application opens the file.
//! 3. The application opens the database, applies the WAL pragmas below, and only
//!    then begins serving.
//! 4. Replication starts after the application has opened the file, so the replica
//!    sees a consistent, already-migrated database.
//! 5. A restore is verified by opening the restored file and running
//!    [`Store::check_integrity`] plus the expected schema version, before serving.

mod error;
mod events;
mod health;
mod migrate;
pub use migrate::format_utc_seconds;
mod tasks;

pub use error::{StateError, StoreErrorKind};
pub use events::{IngestOutcome, reject_envelope};
pub use health::PendingNotification;
pub use tasks::{ArtifactRecord, ClaimOutcome, ExpiryOutcome, ReportOutcomeRecord};

use std::path::{Path, PathBuf};

use blueprint_contracts as contracts;
use blueprint_contracts::error::ErrorCode;
use blueprint_contracts::limits::Limits;
use rusqlite::Connection;

/// The database file name inside the owned directory. The directory, not a file
/// path, is the unit of ownership so WAL and SHM siblings stay together.
pub const DATABASE_FILE: &str = "state.db";

/// Current schema version. A database reporting a higher version is refused rather
/// than opened with a partial understanding of its tables.
pub const SCHEMA_VERSION: i64 = 1;

/// A single-owner handle on the state database.
///
/// There is deliberately no connection pool: one writer is the correctness property,
/// not a performance choice. Callers that need concurrency serialize on the
/// application, not on the database.
pub struct Store {
    conn: Connection,
    limits: Limits,
}

impl Store {
    /// Open or create the database in `dir`, applying migrations.
    ///
    /// The directory must already exist and be writable by this process. Ownership
    /// and permissions are the operator's contract; this function never widens them.
    pub fn open(dir: &Path, limits: Limits) -> Result<Self, StateError> {
        limits
            .check_invariants()
            .map_err(|rejection| StateError::Contract(rejection.into_error()))?;
        let path = dir.join(DATABASE_FILE);
        let conn = Connection::open(&path)?;
        Self::configure(&conn, &limits)?;
        // SQLite creates the file `0644` whatever the umask, so a private directory
        // alone would leave the state world-readable. The core owns this file, so it
        // narrows the mode itself instead of relying on the operator to notice.
        restrict_to_owner(&path)?;
        let mut store = Self { conn, limits };
        store.migrate()?;
        restrict_to_owner(&path)?;
        Ok(store)
    }

    /// Open an in-memory database. Used by tests only; production opens a directory
    /// so the WAL and SHM files land next to the database.
    pub fn open_in_memory(limits: Limits) -> Result<Self, StateError> {
        limits
            .check_invariants()
            .map_err(|rejection| StateError::Contract(rejection.into_error()))?;
        let conn = Connection::open_in_memory()?;
        // Memory databases have no WAL file to checkpoint; the rest of the
        // configuration still applies so tests exercise the same locking mode.
        conn.pragma_update(None, "journal_mode", "MEMORY")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.busy_timeout(std::time::Duration::from_millis(
            limits.storage.busy_timeout_ms as u64,
        ))?;
        let mut store = Self { conn, limits };
        store.migrate()?;
        Ok(store)
    }

    pub fn path(&self) -> PathBuf {
        self.conn.path().map(PathBuf::from).unwrap_or_default()
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    pub fn connection(&self) -> &Connection {
        &self.conn
    }

    pub fn connection_mut(&mut self) -> &mut Connection {
        &mut self.conn
    }

    fn configure(conn: &Connection, limits: &Limits) -> Result<(), StateError> {
        // `journal_mode` returns a row, so it must be read, not executed. Setting it
        // first takes the persistent WAL mode; `synchronous` then applies outside any
        // implicit transaction.
        let mode: String = conn.query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))?;
        if !mode.eq_ignore_ascii_case("wal") {
            return Err(StateError::Corrupt(format!(
                "database refused WAL journal mode (got {mode})"
            )));
        }
        conn.execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;")?;
        conn.pragma_update(
            None,
            "wal_autocheckpoint",
            limits.storage.wal_autocheckpoint_pages,
        )?;
        conn.busy_timeout(std::time::Duration::from_millis(
            limits.storage.busy_timeout_ms as u64,
        ))?;
        Ok(())
    }

    /// Force a WAL checkpoint into the main database file.
    ///
    /// Called on a deliberate shutdown and by the backup contract's operator path.
    /// `TRUNCATE` also resets the WAL file, so a subsequent copy of the database
    /// directory has no growing sidecar.
    pub fn checkpoint_truncate(&self) -> Result<(), StateError> {
        self.conn
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| {
                let busy: i64 = row.get(0)?;
                if busy != 0 {
                    return Err(rusqlite::Error::SqliteFailure(
                        rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY),
                        Some("checkpoint blocked by a reader".to_owned()),
                    ));
                }
                Ok(())
            })
            .map_err(StateError::from)
    }

    /// `PRAGMA integrity_check` plus an expected-version assertion. This is the check
    /// the restore path must pass before serving.
    pub fn check_integrity(&self) -> Result<(), StateError> {
        let result: String = self
            .conn
            .query_row("PRAGMA integrity_check", [], |row| row.get(0))?;
        if result != "ok" {
            return Err(StateError::Integrity(result));
        }
        let version = self.schema_version()?;
        if version != SCHEMA_VERSION {
            return Err(StateError::SchemaVersion {
                found: version,
                expected: SCHEMA_VERSION,
            });
        }
        Ok(())
    }

    pub fn schema_version(&self) -> Result<i64, StateError> {
        let version: i64 = self.conn.query_row(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
            [],
            |row| row.get(0),
        )?;
        Ok(version)
    }
}

/// Restrict the database file to its owner.
///
/// SQLite creates the file with the process umask applied to `0666`, so a private
/// parent directory still leaves `state.db` group- and world-readable. The core owns
/// this file, so it enforces the mode itself.
fn restrict_to_owner(path: &Path) -> Result<(), StateError> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = std::fs::metadata(path)
        .map_err(StateError::Io)?
        .permissions();
    permissions.set_mode(0o600);
    std::fs::set_permissions(path, permissions).map_err(StateError::Io)
}

/// `OptionalExtension` is used through this shim so the crate keeps one import site.
trait OptionalExt<T> {
    fn optional(self) -> Result<Option<T>, rusqlite::Error>;
}

impl<T> OptionalExt<T> for Result<T, rusqlite::Error> {
    fn optional(self) -> Result<Option<T>, rusqlite::Error> {
        match self {
            Ok(value) => Ok(Some(value)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(error) => Err(error),
        }
    }
}

/// Map a contract rejection into the state error surface so callers have one type.
pub(crate) fn contract_error(rejection: contracts::Rejection) -> StateError {
    StateError::Contract(rejection.into_error())
}

/// Convenience for the common "invalid request" rejection in this layer.
pub(crate) fn invalid(message: &'static str) -> contracts::Rejection {
    contracts::Rejection::new(ErrorCode::InvalidRequest, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIMITS: &str = include_str!("../../../../contracts/limits.json");

    pub(crate) fn limits() -> Limits {
        Limits::from_json(LIMITS.as_bytes()).expect("limits")
    }

    #[test]
    fn migrations_apply_and_report_version_one() {
        let store = Store::open_in_memory(limits()).expect("open");
        assert_eq!(store.schema_version().expect("version"), SCHEMA_VERSION);
        store.check_integrity().expect("integrity");
    }

    #[test]
    fn opening_twice_is_idempotent_and_preserves_rows() {
        let dir = tempdir();
        let limits = limits();
        {
            let store = Store::open(&dir, limits.clone()).expect("first open");
            store
                .connection()
                .execute(
                    "INSERT INTO datasets (name, service, signal, retention_days, \
                     authorization_scope, state, created_at) \
                     VALUES ('svc-events', 'svc', 'events', 30, 'telemetry:read', 'active', \
                     '2026-01-01T00:00:00Z')",
                    [],
                )
                .expect("insert");
        }
        let store = Store::open(&dir, limits).expect("second open");
        let count: i64 = store
            .connection()
            .query_row("SELECT COUNT(*) FROM datasets", [], |row| row.get(0))
            .expect("count");
        assert_eq!(count, 1, "state survives a restart on the same directory");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn wal_mode_is_enabled_and_auto_checkpoint_is_pinned() {
        let dir = tempdir_with(0o700);
        let limits = limits();
        let store = Store::open(&dir, limits.clone()).expect("open");
        let mode: String = store
            .connection()
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .expect("mode");
        assert_eq!(mode.to_lowercase(), "wal");
        let pages: i64 = store
            .connection()
            .query_row("PRAGMA wal_autocheckpoint", [], |row| row.get(0))
            .expect("autocheckpoint");
        assert_eq!(pages, limits.storage.wal_autocheckpoint_pages);
        store.checkpoint_truncate().expect("checkpoint");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn integrity_check_rejects_a_wrong_schema_version() {
        let store = Store::open_in_memory(limits()).expect("open");
        store
            .connection()
            .execute("UPDATE schema_migrations SET version = 0", [])
            .expect("downgrade");
        let error = store.check_integrity().expect_err("must reject");
        assert!(matches!(error, StateError::SchemaVersion { .. }));
    }

    pub(crate) fn tempdir() -> PathBuf {
        tempdir_with(0o700)
    }

    pub(crate) fn tempdir_with(mode: u32) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let mut path = std::env::temp_dir();
        let unique = format!(
            "blueprint-state-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        );
        path.push(unique);
        std::fs::create_dir_all(&path).expect("create temp dir");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode))
            .expect("set permissions");
        path
    }
}
