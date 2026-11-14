//! Ids, digests and time strings that appear in more than one contract.
//!
//! Ids are derived from stable inputs so the same logical fact always produces the
//! same id. That is what makes a redelivery a duplicate instead of a second row.

use sha2::{Digest as _, Sha256};

/// Lowercase-hex sha256 of the joined parts, `:`-separated, with a length prefix
/// per part so two different part lists cannot hash to the same bytes.
pub fn digest(parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update((part.len() as u64).to_be_bytes());
        hasher.update(part.as_bytes());
    }
    format!("sha256:{}", hex(&hasher.finalize()))
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// Prefixed id of fixed width, taken from the digest body. The prefix keeps ids
/// of different kinds from being mistaken for one another in a log.
pub fn prefixed_id(prefix: &str, parts: &[&str]) -> String {
    let body = digest(parts);
    let hex = body.strip_prefix("sha256:").unwrap_or(&body);
    format!("{prefix}_{}", &hex[..32])
}

/// Event idempotency key. An explicit producer dedupe key wins over the event id,
/// because a repeated webhook delivery carries a new event id in some producers.
pub fn event_dedupe_key(source: &str, event_id: &str, dedupe_key: Option<&str>) -> String {
    match dedupe_key {
        Some(explicit) => format!("{source}\u{1f}{explicit}"),
        None => format!("{source}\u{1f}{event_id}"),
    }
}

/// Health transition id over (service, check, from, to, at). A real recovery is a
/// different `to`/`at`, so it gets its own id and its own notification.
pub fn health_transition_id(
    service: &str,
    check_id: &str,
    from: &str,
    to: &str,
    at: &str,
) -> String {
    prefixed_id("h", &[service, check_id, from, to, at])
}

/// Upload batch key digest over the canonical descriptor. Content-independent by
/// design: it identifies the logical batch, while `objectDigest` identifies bytes.
pub fn batch_key_digest(
    dataset: &str,
    service: &str,
    signal: &str,
    window_start: &str,
    seq: u64,
) -> String {
    digest(&[dataset, service, signal, window_start, &seq.to_string()])
}
