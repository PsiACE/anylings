//! The platform event envelope, its validation and its redaction pass.

use serde::{Deserialize, Serialize};

use crate::limits::Limits;
use crate::{Rejection, error::ErrorCode};

/// Authenticated producer identity. A body may claim it, but the ingestion path
/// compares it to the authenticated principal and rejects a mismatch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EventSource {
    Webhook,
    Probe,
    Application,
    Scheduler,
    Operator,
}

impl EventSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Webhook => "webhook",
            Self::Probe => "probe",
            Self::Application => "application",
            Self::Scheduler => "scheduler",
            Self::Operator => "operator",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Info,
    Warning,
    Error,
    Critical,
}

/// A validated envelope. Construction is the only way to obtain one, so anything
/// downstream may assume every bound and correlation rule already held.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Envelope {
    pub event_id: String,
    pub source: EventSource,
    pub time: String,
    pub event_type: String,
    pub service: String,
    pub severity: Option<Severity>,
    pub trace_id: Option<String>,
    pub span_id: Option<String>,
    pub attributes: Vec<(String, String)>,
    pub dedupe_key: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawEnvelope {
    schema_version: String,
    // Acronyms in the published contract are upper-case (`eventID`), which
    // `rename_all` cannot produce, so each one is named explicitly.
    #[serde(rename = "eventID")]
    event_id: String,
    source: EventSource,
    time: String,
    #[serde(rename = "type")]
    event_type: String,
    service: String,
    #[serde(default)]
    severity: Option<Severity>,
    #[serde(default, rename = "traceID")]
    trace_id: Option<String>,
    #[serde(default, rename = "spanID")]
    span_id: Option<String>,
    #[serde(default)]
    attributes: std::collections::BTreeMap<String, String>,
    #[serde(default)]
    dedupe_key: Option<String>,
}

impl Envelope {
    /// Parse and validate an envelope. `authenticated_source` is the principal the
    /// transport proved; a body naming another source is refused rather than
    /// trusted, because source is the idempotency namespace.
    pub fn parse(
        body: &[u8],
        authenticated_source: EventSource,
        limits: &Limits,
    ) -> Result<Self, Rejection> {
        if body.len() > limits.request.max_body_bytes {
            return Err(Rejection::new(
                ErrorCode::PayloadTooLarge,
                "event body exceeds the configured maximum",
            ));
        }
        let raw: RawEnvelope = serde_json::from_slice(body).map_err(|_| {
            Rejection::new(
                ErrorCode::InvalidEnvelope,
                "event envelope is not valid JSON",
            )
        })?;
        if raw.schema_version != crate::CONTRACT_VERSION {
            return Err(Rejection::at(
                ErrorCode::InvalidEnvelope,
                "unsupported envelope schema version",
                "schemaVersion",
            ));
        }
        if raw.source != authenticated_source {
            return Err(Rejection::new(
                ErrorCode::Unauthorized,
                "declared source does not match the authenticated principal",
            ));
        }
        check_identifier(&raw.event_id, 8, 128, "eventID")?;
        check_identifier(&raw.time, 20, 40, "time")?;
        if !is_rfc3339(&raw.time) {
            return Err(Rejection::at(
                ErrorCode::InvalidEnvelope,
                "time must be RFC 3339 with an explicit offset",
                "time",
            ));
        }
        if !is_event_type(&raw.event_type) {
            return Err(Rejection::at(
                ErrorCode::InvalidEnvelope,
                "type must be a dotted lowercase event type",
                "type",
            ));
        }
        if !is_service(&raw.service) {
            return Err(Rejection::at(
                ErrorCode::InvalidEnvelope,
                "service must be a lowercase DNS label",
                "service",
            ));
        }
        let trace_id = raw
            .trace_id
            .map(|id| {
                if !is_lower_hex(&id, 32) {
                    Err(Rejection::at(
                        ErrorCode::InvalidEnvelope,
                        "traceID must be 32 lowercase hex characters",
                        "traceID",
                    ))
                } else {
                    Ok(id)
                }
            })
            .transpose()?;
        let span_id = raw
            .span_id
            .map(|id| {
                if !is_lower_hex(&id, 16) {
                    Err(Rejection::at(
                        ErrorCode::InvalidEnvelope,
                        "spanID must be 16 lowercase hex characters",
                        "spanID",
                    ))
                } else {
                    Ok(id)
                }
            })
            .transpose()?;
        if span_id.is_some() && trace_id.is_none() {
            return Err(Rejection::at(
                ErrorCode::InvalidEnvelope,
                "spanID requires a traceID to be correlatable",
                "spanID",
            ));
        }
        if raw.attributes.len() > limits.event.max_attributes {
            return Err(Rejection::new(
                ErrorCode::PayloadTooLarge,
                "event attribute count exceeds the configured maximum",
            ));
        }
        let mut attributes = Vec::with_capacity(raw.attributes.len());
        for (key, value) in raw.attributes {
            if key.len() > limits.event.max_attribute_key_bytes {
                return Err(Rejection::at(
                    ErrorCode::PayloadTooLarge,
                    "attribute key exceeds the configured maximum",
                    "attributes",
                ));
            }
            if !is_attribute_key(&key) {
                return Err(Rejection::at(
                    ErrorCode::InvalidRequest,
                    "attribute key must be lowercase dotted or underscored",
                    "attributes",
                ));
            }
            if value.len() > limits.event.max_attribute_value_bytes {
                return Err(Rejection::at(
                    ErrorCode::PayloadTooLarge,
                    "attribute value exceeds the configured maximum",
                    "attributes",
                ));
            }
            let stored = redact(&key, &value).into_owned();
            attributes.push((key, stored));
        }
        if let Some(key) = &raw.dedupe_key {
            check_identifier(key, 8, 256, "dedupeKey")?;
        }
        Ok(Self {
            event_id: raw.event_id,
            source: raw.source,
            time: raw.time,
            event_type: raw.event_type,
            service: raw.service,
            severity: raw.severity,
            trace_id,
            span_id,
            attributes,
            dedupe_key: raw.dedupe_key,
        })
    }

    /// The idempotency key the state layer dedupes on.
    pub fn dedupe_key(&self) -> String {
        crate::ids::event_dedupe_key(
            self.source.as_str(),
            &self.event_id,
            self.dedupe_key.as_deref(),
        )
    }
}

/// Attribute names whose values are never stored verbatim. Redaction happens at the
/// boundary, before persistence, so a later query cannot reveal what was dropped.
const SENSITIVE_KEY_FRAGMENTS: [&str; 9] = [
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
];

/// Whether an attribute key names a credential and must be redacted.
pub fn is_sensitive_key(key: &str) -> bool {
    SENSITIVE_KEY_FRAGMENTS
        .iter()
        .any(|fragment| key.contains(fragment))
}

/// The value actually stored for an attribute: the original text, or a fixed
/// marker when the key names a credential. The key is kept so an operator can see
/// the field existed; the value is not.
pub fn redact<'a>(key: &str, value: &'a str) -> std::borrow::Cow<'a, str> {
    if is_sensitive_key(key) {
        std::borrow::Cow::Borrowed("[redacted]")
    } else {
        std::borrow::Cow::Borrowed(value)
    }
}

fn check_identifier(
    value: &str,
    min: usize,
    max: usize,
    field: &'static str,
) -> Result<(), Rejection> {
    if value.len() < min || value.len() > max {
        return Err(Rejection::at(
            ErrorCode::InvalidEnvelope,
            "identifier length is outside the contract range",
            field,
        ));
    }
    if !value
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'))
    {
        return Err(Rejection::at(
            ErrorCode::InvalidEnvelope,
            "identifier contains a character outside the contract alphabet",
            field,
        ));
    }
    Ok(())
}

fn is_rfc3339(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() < 20 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[10] != b'T' {
        return false;
    }
    value.ends_with('Z')
        || (value.len() >= 6
            && matches!(&value[value.len() - 6..], s if s.starts_with('+') || s.starts_with('-')))
}

fn is_event_type(value: &str) -> bool {
    let Some(first) = value.as_bytes().first() else {
        return false;
    };
    if !first.is_ascii_lowercase() || value.len() > 128 {
        return false;
    }
    value
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.')
        && value.contains('.')
        && !value.contains("..")
}

fn is_service(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value.as_bytes()[value.len() - 1].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

fn is_lower_hex(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn is_attribute_key(value: &str) -> bool {
    let Some(first) = value.as_bytes().first() else {
        return false;
    };
    first.is_ascii_lowercase()
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_'))
}
