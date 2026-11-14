//! HTTP-boundary tests for the service layer.
//!
//! These exercise the request/response contract a Spin component would expose — status
//! codes, error objects, authorization and dedup — without a Wasm host. They prove the
//! semantics; they do not prove the Spin runtime.

use blueprint_contracts::error::Scope;
use blueprint_contracts::event::EventSource;
use blueprint_contracts::limits::Limits;
use blueprint_service::{Principal, Request, Service};
use blueprint_state::Store;

const LIMITS: &str = include_str!("../../../../contracts/limits.json");

fn service() -> Service {
    let limits = Limits::from_json(LIMITS.as_bytes()).expect("limits");
    let store = Store::open_in_memory(limits.clone()).expect("store");
    Service::new(store, limits)
}

fn operator() -> Principal {
    Principal {
        name: "operator".to_owned(),
        scopes: vec![Scope::TaskRead, Scope::TaskWrite, Scope::HealthRead],
        event_source: None,
    }
}

fn publisher() -> Principal {
    Principal {
        name: "gateway".to_owned(),
        scopes: vec![Scope::TelemetryWrite],
        event_source: Some(EventSource::Application),
    }
}

fn reader() -> Principal {
    Principal {
        name: "observer".to_owned(),
        scopes: vec![Scope::HealthRead],
        event_source: None,
    }
}

fn event_body(event_id: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "schemaVersion": "1",
        "eventID": event_id,
        "source": "application",
        "time": "2026-11-14T10:00:00Z",
        "type": "request.completed",
        "service": "gateway"
    }))
    .expect("encode")
}

#[test]
fn liveness_and_readiness_are_unauthenticated() {
    let mut service = service();
    let anonymous = Principal {
        name: String::new(),
        scopes: Vec::new(),
        event_source: None,
    };
    assert_eq!(
        service
            .handle(&Request::new("GET", "/healthz", Vec::new()), &anonymous)
            .status,
        204
    );
    assert_eq!(
        service
            .handle(&Request::new("GET", "/readyz", Vec::new()), &anonymous)
            .status,
        204
    );
}

#[test]
fn event_ingestion_reports_duplicate_on_redelivery() {
    let mut service = service();
    let body = event_body("req-00000001");
    let first = service.handle(
        &Request::new("POST", "/v1/events", body.clone()),
        &publisher(),
    );
    assert_eq!(first.status, 201);
    assert_eq!(
        first.json_value().expect("json")["duplicate"],
        serde_json::json!(false)
    );
    let second = service.handle(&Request::new("POST", "/v1/events", body), &publisher());
    assert_eq!(second.status, 200);
    assert_eq!(
        second.json_value().expect("json")["duplicate"],
        serde_json::json!(true)
    );
}

#[test]
fn publishing_without_the_write_scope_is_forbidden() {
    let mut service = service();
    let reply = service.handle(
        &Request::new("POST", "/v1/events", event_body("req-00000002")),
        &reader(),
    );
    assert_eq!(reply.status, 403);
    let body = reply.json_value().expect("json");
    assert_eq!(body["code"], serde_json::json!("forbidden_scope"));
    assert_eq!(body["retryable"], serde_json::json!(false));
}

#[test]
fn a_body_claiming_another_source_is_unauthorized() {
    let mut service = service();
    // The authenticated principal is `application`; the body claims `webhook`.
    let mut body: serde_json::Value =
        serde_json::from_slice(&event_body("req-00000003")).expect("json");
    body["source"] = serde_json::json!("webhook");
    let reply = service.handle(
        &Request::new(
            "POST",
            "/v1/events",
            serde_json::to_vec(&body).expect("encode"),
        ),
        &publisher(),
    );
    assert_eq!(reply.status, 401);
    assert_eq!(
        reply.json_value().expect("json")["code"],
        serde_json::json!("unauthorized")
    );
}

#[test]
fn oversized_body_is_refused_with_a_contract_error() {
    let mut service = service();
    let mut body = event_body("req-00000004");
    body.resize(service.limits().request.max_body_bytes + 1, b' ');
    let reply = service.handle(&Request::new("POST", "/v1/events", body), &publisher());
    assert_eq!(reply.status, 413);
    assert_eq!(
        reply.json_value().expect("json")["code"],
        serde_json::json!("payload_too_large")
    );
}

#[test]
fn malformed_json_is_invalid_rather_than_a_server_error() {
    let mut service = service();
    let reply = service.handle(
        &Request::new("POST", "/v1/events", b"{not json".to_vec()),
        &publisher(),
    );
    assert_eq!(reply.status, 400);
    assert_eq!(
        reply.json_value().expect("json")["code"],
        serde_json::json!("invalid_envelope")
    );
}

#[test]
fn task_submission_is_idempotent_per_request_id() {
    let mut service = service();
    let body = serde_json::to_vec(&serde_json::json!({
        "service": "gateway",
        "kind": "verify",
        "idempotent": true,
        "params": { "target": "/healthz" }
    }))
    .expect("encode");
    let request = Request::new("POST", "/v1/tasks", body.clone()).with_request_id("req-7f00aa01");
    let first = service.handle(&request, &operator());
    assert_eq!(first.status, 201);
    let task_id = first.json_value().expect("json")["taskID"]
        .as_str()
        .expect("task id")
        .to_owned();

    let retry = service.handle(&request, &operator());
    assert_eq!(retry.status, 200, "a retry is not an error");
    assert_eq!(
        retry.json_value().expect("json")["taskID"].as_str(),
        Some(task_id.as_str()),
        "the same request id yields the same task"
    );
}

#[test]
fn a_submission_without_a_request_id_is_refused() {
    let mut service = service();
    let body = serde_json::to_vec(&serde_json::json!({
        "service": "gateway",
        "kind": "verify",
        "idempotent": true,
        "params": {}
    }))
    .expect("encode");
    let reply = service.handle(&Request::new("POST", "/v1/tasks", body), &operator());
    assert_eq!(reply.status, 400);
}

#[test]
fn claim_then_report_round_trips_with_generation() {
    let mut service = service();
    let submit = serde_json::to_vec(&serde_json::json!({
        "service": "gateway",
        "kind": "probe",
        "idempotent": true,
        "params": { "target": "http://127.0.0.1:1/healthz" }
    }))
    .expect("encode");
    let submitted = service.handle(
        &Request::new("POST", "/v1/tasks", submit).with_request_id("req-7f00aa02"),
        &operator(),
    );
    let task_id = submitted.json_value().expect("json")["taskID"]
        .as_str()
        .expect("id")
        .to_owned();

    let claim = serde_json::to_vec(&serde_json::json!({
        "service": "gateway",
        "workerID": "worker-local-01"
    }))
    .expect("encode");
    let claimed = service.handle(&Request::new("POST", "/v1/tasks/claim", claim), &operator());
    assert_eq!(claimed.status, 200);
    let value = claimed.json_value().expect("json");
    assert_eq!(value["taskID"].as_str(), Some(task_id.as_str()));
    let generation = value["generation"].as_u64().expect("generation");
    assert_eq!(generation, 1);

    let report = serde_json::to_vec(&serde_json::json!({
        "taskID": task_id,
        "generation": generation,
        "workerID": "worker-local-01",
        "outcome": "succeeded"
    }))
    .expect("encode");
    let reported = service.handle(
        &Request::new("POST", "/v1/tasks/report", report.clone()),
        &operator(),
    );
    assert_eq!(reported.status, 200);
    assert_eq!(
        reported.json_value().expect("json")["state"],
        serde_json::json!("succeeded")
    );

    // The same report again is a duplicate, not a conflict.
    let again = service.handle(
        &Request::new("POST", "/v1/tasks/report", report),
        &operator(),
    );
    assert_eq!(again.status, 200);
    assert_eq!(
        again.json_value().expect("json")["duplicate"],
        serde_json::json!(true)
    );
}

#[test]
fn a_stale_generation_report_is_lease_lost() {
    let mut service = service();
    let submit = serde_json::to_vec(&serde_json::json!({
        "service": "gateway", "kind": "probe", "idempotent": true, "params": {}
    }))
    .expect("encode");
    let submitted = service.handle(
        &Request::new("POST", "/v1/tasks", submit).with_request_id("req-7f00aa03"),
        &operator(),
    );
    let task_id = submitted.json_value().expect("json")["taskID"]
        .as_str()
        .expect("id")
        .to_owned();
    let claim = serde_json::to_vec(&serde_json::json!({
        "service": "gateway", "workerID": "worker-local-01"
    }))
    .expect("encode");
    service.handle(&Request::new("POST", "/v1/tasks/claim", claim), &operator());
    let report = serde_json::to_vec(&serde_json::json!({
        "taskID": task_id,
        "generation": 7,
        "workerID": "worker-local-01",
        "outcome": "succeeded"
    }))
    .expect("encode");
    let reply = service.handle(
        &Request::new("POST", "/v1/tasks/report", report),
        &operator(),
    );
    assert_eq!(reply.status, 409);
    assert_eq!(
        reply.json_value().expect("json")["code"],
        serde_json::json!("lease_lost")
    );
}

#[test]
fn claim_returns_no_content_when_the_queue_is_empty() {
    let mut service = service();
    let claim = serde_json::to_vec(&serde_json::json!({
        "service": "gateway", "workerID": "worker-local-01"
    }))
    .expect("encode");
    let reply = service.handle(&Request::new("POST", "/v1/tasks/claim", claim), &operator());
    assert_eq!(reply.status, 204);
    assert!(reply.body.is_empty());
}

#[test]
fn health_observation_and_readback_are_bounded() {
    let mut service = service();
    let observe = serde_json::to_vec(&serde_json::json!({
        "service": "gateway", "checkID": "http", "ok": false
    }))
    .expect("encode");
    let reply = service.handle(
        &Request::new("POST", "/v1/health/observe", observe),
        &operator(),
    );
    assert_eq!(reply.status, 200);
    assert_eq!(
        reply.json_value().expect("json")["state"],
        serde_json::json!("degraded")
    );

    let read = service.handle(&Request::new("GET", "/v1/health", Vec::new()), &operator());
    assert_eq!(read.status, 200);
    let body = read.json_value().expect("json");
    let checks = body["checks"].as_array().expect("checks");
    assert_eq!(checks.len(), 1);
    assert_eq!(checks[0]["state"], serde_json::json!("degraded"));

    let pending = service.handle(
        &Request::new("GET", "/v1/notifications/pending", Vec::new()),
        &operator(),
    );
    assert_eq!(pending.status, 200);
    let pending_body = pending.json_value().expect("json");
    assert_eq!(
        pending_body["pending"].as_array().expect("pending").len(),
        1
    );
    // A channel name is a configured label, never a URL a request supplied.
    let channel = pending_body["pending"][0]["channel"]
        .as_str()
        .expect("channel");
    assert!(!channel.contains("://"));
}

#[test]
fn unknown_route_is_not_found_and_does_not_enumerate() {
    let mut service = service();
    let reply = service.handle(&Request::new("GET", "/v1/secret", Vec::new()), &operator());
    assert_eq!(reply.status, 404);
    assert_eq!(
        reply.json_value().expect("json")["code"],
        serde_json::json!("not_found")
    );
}

#[test]
fn no_route_accepts_a_caller_supplied_path_or_sql() {
    let mut service = service();
    // A dataset/query route carrying SQL or a path must not exist at all.
    for path in ["/v1/query", "/v1/datasets/gateway-events"] {
        let reply = service.handle(
            &Request::new(
                "POST",
                path,
                br#"{"sql":"SELECT * FROM secrets","path":"/etc/passwd"}"#.to_vec(),
            ),
            &operator(),
        );
        assert_eq!(reply.status, 404, "{path} must not be routed");
    }
}
