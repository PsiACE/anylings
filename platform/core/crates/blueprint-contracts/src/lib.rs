//! Versioned platform contracts for the Anylings shared core.
//!
//! These types are the machine-checked form of `platform/contracts/v1/*.schema.json`.
//! The JSON Schemas are the published artifact; this crate is the implementation
//! that refuses to drift from them (see the schema conformance tests).

pub mod dataset;
pub mod error;
pub mod event;
pub mod health;
pub mod ids;
pub mod limits;
pub mod task;

/// Version of every contract in this crate. A breaking change to a stored shape
/// or an accepted request is a new version, never an in-place edit.
pub const CONTRACT_VERSION: &str = "1";

/// A rejection that carries a contract error code. `Retryable` is never true for
/// an authorization failure or for an unknown outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejection {
    pub code: error::ErrorCode,
    pub message: &'static str,
    pub field: Option<&'static str>,
}

impl Rejection {
    pub fn new(code: error::ErrorCode, message: &'static str) -> Self {
        Self {
            code,
            message,
            field: None,
        }
    }

    pub fn at(code: error::ErrorCode, message: &'static str, field: &'static str) -> Self {
        Self {
            code,
            message,
            field: Some(field),
        }
    }

    pub fn into_error(self) -> error::ApiError {
        error::ApiError {
            schema_version: CONTRACT_VERSION.to_owned(),
            code: self.code,
            message: self.message.to_owned(),
            retryable: self.code.retryable(),
            field: self.field.map(str::to_owned),
            request_id: None,
        }
    }
}

impl std::fmt::Display for Rejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for Rejection {}
