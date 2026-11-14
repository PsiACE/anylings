//! The one error type this layer returns.

use blueprint_contracts::error::ApiError;

#[derive(Debug)]
pub enum StateError {
    /// A request or repository-level failure already expressed as a contract error.
    Contract(ApiError),
    /// The database refused the operation.
    Sqlite(rusqlite::Error),
    /// `PRAGMA integrity_check` did not return `ok`.
    Integrity(String),
    /// The file on disk was written by a schema this build does not implement.
    SchemaVersion { found: i64, expected: i64 },
    /// A stored row could not be read back into its contract shape.
    Corrupt(String),
    /// The database file could not be inspected or its mode could not be tightened.
    Io(std::io::Error),
}

impl From<rusqlite::Error> for StateError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Sqlite(error)
    }
}

impl From<std::io::Error> for StateError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl std::fmt::Display for StateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Contract(error) => write!(f, "{}: {}", error.code, error.message),
            Self::Sqlite(error) => write!(f, "sqlite: {error}"),
            Self::Integrity(detail) => write!(f, "integrity check failed: {detail}"),
            Self::SchemaVersion { found, expected } => {
                write!(f, "schema version {found} is not the expected {expected}")
            }
            Self::Corrupt(detail) => write!(f, "stored row is not contract-shaped: {detail}"),
            Self::Io(error) => write!(f, "database file i/o failed: {error}"),
        }
    }
}

impl std::error::Error for StateError {}

impl StateError {
    /// Coarse classification, so callers can branch without matching internals.
    pub fn kind(&self) -> StoreErrorKind {
        match self {
            Self::Contract(_) => StoreErrorKind::Contract,
            Self::Sqlite(_) => StoreErrorKind::Storage,
            Self::Integrity(_) => StoreErrorKind::Integrity,
            Self::SchemaVersion { .. } => StoreErrorKind::SchemaVersion,
            Self::Corrupt(_) => StoreErrorKind::Corrupt,
            Self::Io(_) => StoreErrorKind::Storage,
        }
    }

    /// The contract error, when this failure is a request-level rejection.
    pub fn contract(&self) -> Option<&ApiError> {
        match self {
            Self::Contract(error) => Some(error),
            _ => None,
        }
    }

    /// Whether the failure can be retried without an operator: only a busy database
    /// qualifies, and only because the timeout already bounded the wait.
    pub fn retryable(&self) -> bool {
        match self {
            Self::Sqlite(rusqlite::Error::SqliteFailure(ffi, _)) => {
                matches!(ffi.code, rusqlite::ErrorCode::DatabaseBusy)
            }
            _ => false,
        }
    }
}

/// Classification of a [`StateError`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoreErrorKind {
    /// The request was refused by a contract rule.
    Contract,
    /// The database refused or failed the operation.
    Storage,
    /// Integrity verification failed.
    Integrity,
    /// The on-disk schema is not the expected version.
    SchemaVersion,
    /// A stored row could not be read back into its contract shape.
    Corrupt,
}
