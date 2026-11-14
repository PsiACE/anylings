//! Wire-name conformance for request bodies.
//!
//! The published contracts use upper-case acronyms (`eventID`, `taskID`, `workerID`,
//! `checkID`, `traceID`, `spanID`, `requestID`), which `#[serde(rename_all =
//! "camelCase")]` silently renders as `Id`. That mismatch is invisible until a request
//! is rejected in production, so this test pins the exact wire names of every decoded
//! request body.

use blueprint_contracts::error::Scope;
use blueprint_contracts::limits::Limits;
use blueprint_service::{Principal, Request, Service};
use blueprint_state::Store;

const LIMITS: &str = include_str!("../../../../contracts/limits.json");

fn service() -> Service {
    let limits = Limits::from_json(LIMITS.as_bytes()).expect("limits");
    Service::new(
        Store::open_in_memory(limits.clone()).expect("store"),
        limits,
    )
}

fn principal() -> Principal {
    Principal {
        name: "operator".to_owned(),
        scopes: vec![Scope::TaskRead, Scope::TaskWrite, Scope::HealthRead],
        event_source: None,
    }
}

/// Posting `body` to `path` must not fail with `invalid_request`; anything else
/// (authorization, conflict, empty queue) is acceptable and irrelevant here.
fn accepted_shape(service: &mut Service, path: &str, body: serde_json::Value) -> u16 {
    let reply = service.handle(
        &Request::new("POST", path, serde_json::to_vec(&body).expect("encode"))
            .with_request_id("req-namecheck01"),
        &principal(),
    );
    reply.status
}

#[test]
fn claim_body_uses_worker_id_capitalization() {
    let mut service = service();
    let status = accepted_shape(
        &mut service,
        "/v1/tasks/claim",
        serde_json::json!({ "service": "gateway", "workerID": "worker-local-01" }),
    );
    assert_ne!(status, 400, "workerID is the published name");
    // The `Id` spelling the derive would have produced is not accepted.
    let wrong = accepted_shape(
        &mut service,
        "/v1/tasks/claim",
        serde_json::json!({ "service": "gateway", "workerId": "worker-local-01" }),
    );
    assert_eq!(wrong, 400, "workerId is not part of the contract");
}

#[test]
fn observation_body_uses_check_id_capitalization() {
    let mut service = service();
    let status = accepted_shape(
        &mut service,
        "/v1/health/observe",
        serde_json::json!({ "service": "gateway", "checkID": "http", "ok": true }),
    );
    assert_ne!(status, 400, "checkID is the published name");
    let wrong = accepted_shape(
        &mut service,
        "/v1/health/observe",
        serde_json::json!({ "service": "gateway", "checkId": "http", "ok": true }),
    );
    assert_eq!(wrong, 400, "checkId is not part of the contract");
}

#[test]
fn report_body_uses_task_id_and_result_digest_names() {
    let mut service = service();
    // A well-formed body for a task that does not exist is `not_found`, not `invalid`,
    // which is exactly what proves the field names decoded.
    let status = accepted_shape(
        &mut service,
        "/v1/tasks/report",
        serde_json::json!({
            "taskID": format!("t_{}", "a".repeat(32)),
            "generation": 1,
            "workerID": "worker-local-01",
            "outcome": "succeeded",
            "resultDigest": format!("sha256:{}", "b".repeat(64))
        }),
    );
    assert_eq!(
        status, 404,
        "the body decoded; the task simply does not exist"
    );
}

#[test]
fn event_envelope_uses_event_id_and_trace_id_names() {
    let mut service = service();
    let publisher = Principal {
        name: "gateway".to_owned(),
        scopes: vec![Scope::TelemetryWrite],
        event_source: Some(blueprint_contracts::event::EventSource::Application),
    };
    let body = serde_json::json!({
        "schemaVersion": "1",
        "eventID": "req-namecheck02",
        "source": "application",
        "time": "2026-11-14T10:00:00Z",
        "type": "request.completed",
        "service": "gateway",
        "traceID": "4bf92f3577b34da6a3ce929d0e0e4736",
        "spanID": "00f067aa0ba902b7"
    });
    let reply = service.handle(
        &Request::new(
            "POST",
            "/v1/events",
            serde_json::to_vec(&body).expect("encode"),
        ),
        &publisher,
    );
    assert_eq!(
        reply.status, 201,
        "eventID/traceID/spanID are the published names"
    );
}
