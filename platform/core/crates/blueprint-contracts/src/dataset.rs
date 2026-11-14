//! Dataset catalog and immutable upload batches.
//!
//! The catalog is server-owned. A caller sends a dataset *name*; the core looks up
//! the storage key, partition layout and required scope. No request field accepts a
//! path, bucket, prefix, SQL fragment or filesystem location, so there is no
//! caller-controlled write target to escape from.

use serde::{Deserialize, Serialize};

use crate::{Rejection, error::ErrorCode, ids};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Signal {
    Metrics,
    Logs,
    Traces,
    Events,
}

impl Signal {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Metrics => "metrics",
            Self::Logs => "logs",
            Self::Traces => "traces",
            Self::Events => "events",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DatasetState {
    Active,
    Frozen,
    Retired,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CatalogEntry {
    pub name: String,
    pub service: String,
    pub signal: Signal,
    pub retention_days: u32,
    pub authorization_scope: crate::error::Scope,
    #[serde(default)]
    pub max_scan_bytes: Option<u64>,
    #[serde(default)]
    pub state: Option<DatasetState>,
}

/// Batch lifecycle. `pending` holds a reserved slot with no object; expiry releases
/// the slot so a failed uploader cannot pin local capacity forever.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BatchState {
    Pending,
    Uploaded,
    Committed,
    Abandoned,
}

impl BatchState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Uploaded => "uploaded",
            Self::Committed => "committed",
            Self::Abandoned => "abandoned",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct UploadBatch {
    pub batch_id: String,
    pub key_digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_digest: Option<String>,
    pub state: BatchState,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<String>,
    #[serde(default)]
    pub bytes: u64,
}

impl UploadBatch {
    /// Announce a batch. The batch id and key digest are derived from the descriptor,
    /// so announcing the same logical batch twice mints the same identity and the
    /// state layer can answer with the existing row.
    pub fn announce(
        dataset: &str,
        service: &str,
        signal: Signal,
        window_start: &str,
        seq: u64,
        now: &str,
        expires_at: &str,
    ) -> Self {
        let key_digest =
            ids::batch_key_digest(dataset, service, signal.as_str(), window_start, seq);
        let batch_id = ids::prefixed_id(
            "b",
            &[
                dataset,
                service,
                signal.as_str(),
                window_start,
                &seq.to_string(),
            ],
        );
        Self {
            batch_id,
            key_digest,
            object_digest: None,
            state: BatchState::Pending,
            created_at: now.to_owned(),
            expires_at: Some(expires_at.to_owned()),
            bytes: 0,
        }
    }

    /// Verify a content digest before the batch is allowed to leave `pending`.
    pub fn attach_object(
        &mut self,
        content_digest: &str,
        bytes: u64,
        now: &str,
    ) -> Result<(), Rejection> {
        if self.state != BatchState::Pending {
            return Err(Rejection::new(
                ErrorCode::Conflict,
                "only a pending batch accepts an object",
            ));
        }
        if !content_digest.starts_with("sha256:") || content_digest.len() != 71 {
            return Err(Rejection::at(
                ErrorCode::InvalidRequest,
                "object digest must be a sha256 digest",
                "batch.objectDigest",
            ));
        }
        if bytes == 0 {
            return Err(Rejection::at(
                ErrorCode::InvalidRequest,
                "an upload batch must carry content",
                "batch.bytes",
            ));
        }
        let _ = now;
        self.object_digest = Some(content_digest.to_owned());
        self.state = BatchState::Uploaded;
        self.bytes = bytes;
        Ok(())
    }
}

/// Resolve a public dataset name to its catalog entry, refusing an unknown name
/// rather than treating it as a path to be created.
pub fn resolve<'a>(
    catalog: &'a [CatalogEntry],
    name: &str,
    required: crate::error::Scope,
) -> Result<&'a CatalogEntry, Rejection> {
    let entry = catalog
        .iter()
        .find(|entry| entry.name == name)
        .ok_or_else(|| Rejection::at(ErrorCode::NotFound, "dataset is not cataloged", "dataset"))?;
    if entry.state == Some(DatasetState::Retired) {
        return Err(Rejection::at(
            ErrorCode::Conflict,
            "dataset is retired",
            "dataset",
        ));
    }
    if entry.authorization_scope != required {
        return Err(Rejection::new(
            ErrorCode::ForbiddenScope,
            "dataset requires a different scope",
        ));
    }
    Ok(entry)
}

/// Partition key for a batch. Server-computed, never request-supplied.
pub fn partition_key(service: &str, signal: Signal, day: &str) -> String {
    format!("{}/signal={}/day={}", service, signal.as_str(), day)
}
