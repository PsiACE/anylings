//! Bounds applied before any durable write.
//!
//! The numbers live in `platform/contracts/limits.json`. This module compiles the
//! same values and the state layer asserts equality at startup, so a config edit
//! that the code did not follow fails loudly instead of silently admitting
//! oversized input.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Limits {
    pub schema_version: String,
    pub request: RequestLimits,
    pub event: EventLimits,
    pub page: PageLimits,
    pub result: ResultLimits,
    pub task: TaskLimits,
    pub dataset: DatasetLimits,
    pub storage: StorageLimits,
    pub query: QueryLimits,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RequestLimits {
    pub max_body_bytes: usize,
    pub max_batch_items: usize,
    pub max_header_value_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EventLimits {
    pub max_attributes: usize,
    pub max_attribute_key_bytes: usize,
    pub max_attribute_value_bytes: usize,
    pub max_clock_skew_seconds: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PageLimits {
    pub default_size: usize,
    pub max_size: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ResultLimits {
    pub max_rows: usize,
    pub max_bytes: usize,
    pub max_artifacts: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TaskLimits {
    pub max_param_bytes: usize,
    pub lease_seconds: i64,
    pub heartbeat_seconds: i64,
    pub max_reports_per_task: u32,
    pub max_open_tasks_per_service: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DatasetLimits {
    pub max_pending_batches: u32,
    pub max_catalog_per_service: u32,
    pub max_partitions_per_batch: u32,
    pub max_retention_days: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StorageLimits {
    pub wal_autocheckpoint_pages: i64,
    pub busy_timeout_ms: i64,
    pub max_pending_queue_rows: u32,
    pub max_notification_attempts: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct QueryLimits {
    pub max_concurrency: u32,
    pub max_scan_bytes: u64,
    pub timeout_ms: u64,
}

impl Limits {
    /// Parse a limits document and reject a schema version this build does not
    /// implement, rather than silently applying a different number set.
    pub fn from_json(bytes: &[u8]) -> Result<Self, crate::Rejection> {
        let limits: Self = serde_json::from_slice(bytes).map_err(|_| {
            crate::Rejection::new(
                crate::error::ErrorCode::InvalidRequest,
                "limits document does not match the contract",
            )
        })?;
        if limits.schema_version != crate::CONTRACT_VERSION {
            return Err(crate::Rejection::new(
                crate::error::ErrorCode::InvalidRequest,
                "unsupported limits schema version",
            ));
        }
        limits.check_invariants()?;
        Ok(limits)
    }

    /// Cross-field invariants that a schema cannot express per-field.
    pub fn check_invariants(&self) -> Result<(), crate::Rejection> {
        if self.page.default_size > self.page.max_size {
            return Err(crate::Rejection::at(
                crate::error::ErrorCode::InvalidRequest,
                "default page size exceeds the maximum",
                "page.defaultSize",
            ));
        }
        if self.task.heartbeat_seconds >= self.task.lease_seconds {
            return Err(crate::Rejection::at(
                crate::error::ErrorCode::InvalidRequest,
                "heartbeat interval must be shorter than the lease",
                "task.heartbeatSeconds",
            ));
        }
        if self.query.max_concurrency != 1 {
            return Err(crate::Rejection::at(
                crate::error::ErrorCode::InvalidRequest,
                "analysis query concurrency is fixed at one on the constrained node",
                "query.maxConcurrency",
            ));
        }
        if self.event.max_attribute_value_bytes > self.result.max_bytes {
            return Err(crate::Rejection::at(
                crate::error::ErrorCode::InvalidRequest,
                "attribute value bound exceeds the result byte bound",
                "event.maxAttributeValueBytes",
            ));
        }
        Ok(())
    }

    /// Effective page size for a requested size, clamped to the maximum.
    pub fn page_size(&self, requested: Option<usize>) -> usize {
        requested
            .unwrap_or(self.page.default_size)
            .min(self.page.max_size)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DOC: &str = include_str!("../../../../contracts/limits.json");

    #[test]
    fn shipped_limits_document_parses_and_holds_invariants() {
        let limits = Limits::from_json(DOC.as_bytes()).expect("shipped limits parse");
        limits
            .check_invariants()
            .expect("shipped limits are consistent");
        assert_eq!(limits.query.max_concurrency, 1);
    }

    #[test]
    fn page_size_is_clamped_to_the_maximum() {
        let limits = Limits::from_json(DOC.as_bytes()).expect("limits");
        assert_eq!(limits.page_size(None), limits.page.default_size);
        assert_eq!(limits.page_size(Some(usize::MAX)), limits.page.max_size);
    }

    #[test]
    fn unsupported_schema_version_is_rejected() {
        let mut value: serde_json::Value = serde_json::from_str(DOC).expect("json");
        value["schemaVersion"] = serde_json::json!("2");
        let err = Limits::from_json(value.to_string().as_bytes()).expect_err("version rejected");
        assert_eq!(err.code, crate::error::ErrorCode::InvalidRequest);
    }

    #[test]
    fn heartbeat_longer_than_lease_is_rejected() {
        let mut value: serde_json::Value = serde_json::from_str(DOC).expect("json");
        value["task"]["heartbeatSeconds"] = value["task"]["leaseSeconds"].clone();
        let err = Limits::from_json(value.to_string().as_bytes()).expect_err("invariant");
        assert_eq!(err.field, Some("task.heartbeatSeconds"));
    }
}
