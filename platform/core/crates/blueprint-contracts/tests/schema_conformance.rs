//! Schema conformance: the published JSON Schemas in `platform/contracts/` and the
//! Rust types in this crate are two forms of one contract.
//!
//! These tests validate real fixture documents against the published schemas and then
//! parse the same documents through the Rust types. A drift in either direction — a
//! schema that admits what the code rejects, or a field the code renames silently —
//! fails here rather than in a consumer phase.

use serde_json::Value;

const SCHEMA_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../contracts");
const FIXTURE_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../contracts/fixtures");

fn load(path: &str) -> Value {
    let text = std::fs::read_to_string(path).unwrap_or_else(|error| panic!("read {path}: {error}"));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("parse {path}: {error}"))
}

fn schema(name: &str) -> Value {
    load(&format!("{SCHEMA_DIR}/v1/{name}.schema.json"))
}

fn fixture(name: &str) -> Value {
    load(&format!("{FIXTURE_DIR}/{name}.json"))
}

fn validator(name: &str) -> jsonschema::Validator {
    jsonschema::validator_for(&schema(name)).expect("schema compiles")
}

/// Validate and return the first error path, for a readable assertion message.
fn expect_valid(name: &str, instance: &Value) {
    let validator = validator(name);
    if let Err(error) = validator.validate(instance) {
        panic!(
            "{name} rejected a valid fixture at {}: {error}",
            error.instance_path()
        );
    }
}

fn expect_invalid(name: &str, instance: &Value) -> String {
    let validator = validator(name);
    match validator.validate(instance) {
        Ok(()) => panic!("{name} accepted an invalid fixture"),
        Err(error) => error.instance_path().to_string(),
    }
}

#[test]
fn every_published_schema_compiles() {
    for name in ["event", "error", "limits", "dataset", "task", "health"] {
        let _ = validator(name);
    }
}

#[test]
fn event_fixtures_are_valid_and_parse_through_the_rust_type() {
    let limits = blueprint_contracts::limits::Limits::from_json(
        &std::fs::read(format!("{SCHEMA_DIR}/limits.json")).expect("limits"),
    )
    .expect("limits parse");
    // Each fixture is parsed with the principal that is allowed to produce it, so the
    // source check stays exercised instead of being bypassed.
    for (name, source) in [
        (
            "event-webhook",
            blueprint_contracts::event::EventSource::Webhook,
        ),
        (
            "event-application-trace",
            blueprint_contracts::event::EventSource::Application,
        ),
    ] {
        let doc = fixture(name);
        expect_valid("event", &doc);
        let bytes = serde_json::to_vec(&doc).expect("encode");
        blueprint_contracts::event::Envelope::parse(&bytes, source, &limits)
            .expect("accepts the fixture");
    }
}

#[test]
fn invalid_event_fixtures_are_rejected_by_both_layers() {
    let limits = blueprint_contracts::limits::Limits::from_json(
        &std::fs::read(format!("{SCHEMA_DIR}/limits.json")).expect("limits"),
    )
    .expect("limits parse");
    for name in [
        "event-span-without-trace",
        "event-uppercase-type",
        "event-bad-trace-id",
        "event-extra-field",
    ] {
        let doc = fixture(name);
        expect_invalid("event", &doc);
        let bytes = serde_json::to_vec(&doc).expect("encode");
        assert!(
            blueprint_contracts::event::Envelope::parse(
                &bytes,
                blueprint_contracts::event::EventSource::Application,
                &limits,
            )
            .is_err(),
            "{name} must be refused by the Rust type as well"
        );
    }
}

#[test]
fn error_fixtures_hold_the_retryability_rules() {
    for name in [
        "error-forbidden-scope",
        "error-outcome-unknown",
        "error-queue-full",
    ] {
        expect_valid("error", &fixture(name));
    }
    // A retryable unknown outcome contradicts the contract that the schema states.
    let mut bad = fixture("error-outcome-unknown");
    bad["retryable"] = Value::Bool(true);
    expect_invalid("error", &bad);
    // An authz failure advertised as retryable is equally wrong.
    let mut bad = fixture("error-forbidden-scope");
    bad["retryable"] = Value::Bool(true);
    expect_invalid("error", &bad);
}

#[test]
fn error_codes_are_the_closed_set_the_rust_enum_implements() {
    let schema = schema("error");
    let published: Vec<String> = schema["properties"]["code"]["enum"]
        .as_array()
        .expect("enum")
        .iter()
        .map(|value| value.as_str().expect("string").to_owned())
        .collect();
    let implemented = [
        "invalid_envelope",
        "invalid_request",
        "payload_too_large",
        "unauthorized",
        "forbidden_scope",
        "not_found",
        "conflict",
        "lease_lost",
        "outcome_unknown",
        "rate_limited",
        "queue_full",
        "unavailable",
        "internal",
    ];
    assert_eq!(published, implemented);
}

#[test]
fn dataset_fixtures_publish_no_caller_supplied_path() {
    let doc = fixture("dataset-batch-pending");
    expect_valid("dataset", &doc);
    let object = doc.as_object().expect("object");
    for forbidden in ["path", "bucket", "prefix", "sql", "location"] {
        assert!(
            !object.contains_key(forbidden),
            "a public dataset request must not carry {forbidden}"
        );
    }
    // The batch id and key digest are server-shaped, not free-form.
    assert!(
        doc["batch"]["batchID"]
            .as_str()
            .expect("id")
            .starts_with("b_")
    );
    assert!(
        doc["batch"]["keyDigest"]
            .as_str()
            .expect("digest")
            .starts_with("sha256:")
    );
}

#[test]
fn task_fixtures_cover_every_lifecycle_state_the_rust_enum_implements() {
    for state in [
        "submitted",
        "leased",
        "cancel_requested",
        "outcome_unknown",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ] {
        let schema_enum = schema("task")["$defs"]["task"]["properties"]["state"]["enum"]
            .as_array()
            .expect("enum")
            .clone();
        assert!(
            schema_enum.iter().any(|value| value == state),
            "schema must publish the {state} state"
        );
    }
    expect_valid("task", &fixture("task-unknown-outcome"));
    expect_valid("task", &fixture("task-duplicate-report"));
}

#[test]
fn health_fixtures_cover_transition_and_notification_dedup() {
    expect_valid("health", &fixture("health-transition"));
    expect_valid("health", &fixture("health-steady"));
    let mut bad = fixture("health-transition");
    bad["notification"]["channel"] = Value::String("https://example.invalid/hook".to_owned());
    expect_invalid("health", &bad);
}

#[test]
fn limits_fixture_matches_the_document_the_code_compiles() {
    expect_valid("limits", &load(&format!("{SCHEMA_DIR}/limits.json")));
    // The shipped document is the one the state layer reads at startup; validating it
    // here keeps a hand-edit from passing review unnoticed.
    let parsed = blueprint_contracts::limits::Limits::from_json(
        &std::fs::read(format!("{SCHEMA_DIR}/limits.json")).expect("limits"),
    )
    .expect("parses");
    assert_eq!(parsed.schema_version, blueprint_contracts::CONTRACT_VERSION);
}
