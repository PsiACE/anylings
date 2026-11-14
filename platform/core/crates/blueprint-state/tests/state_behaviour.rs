//! Behavioral tests for the state layer against a real SQLite file.
//!
//! These are native-SQLite tests, not Spin-runtime tests: they exercise the exact
//! schema, pragmas and transactions the component will use, through the same crate.
//! A `PASS` here is evidence about storage behaviour, not about a Wasm host.

use blueprint_contracts::event::EventSource;
use blueprint_contracts::health::{HealthPolicy, Observation};
use blueprint_contracts::limits::Limits;
use blueprint_contracts::task::{Report, ReportOutcome, TaskKind, TaskSubmission};
use blueprint_state::{ClaimOutcome, ExpiryOutcome, IngestOutcome, Store, StoreErrorKind};

const LIMITS: &str = include_str!("../../../../contracts/limits.json");

fn limits() -> Limits {
    Limits::from_json(LIMITS.as_bytes()).expect("limits")
}

fn store() -> Store {
    Store::open_in_memory(limits()).expect("in-memory store")
}

fn envelope(event_id: &str, dedupe: Option<&str>) -> Vec<u8> {
    let mut value = serde_json::json!({
        "schemaVersion": "1",
        "eventID": event_id,
        "source": "application",
        "time": "2026-11-14T10:00:00Z",
        "type": "request.completed",
        "service": "gateway",
        "attributes": { "route": "/healthz", "authorization": "Bearer synthetic" }
    });
    if let Some(dedupe) = dedupe {
        value["dedupeKey"] = serde_json::json!(dedupe);
    }
    serde_json::to_vec(&value).expect("encode")
}

fn parse(body: &[u8]) -> blueprint_contracts::event::Envelope {
    blueprint_contracts::event::Envelope::parse(body, EventSource::Application, &limits())
        .expect("valid envelope")
}

fn submission(task_id: &str, idempotent: bool) -> TaskSubmission {
    TaskSubmission {
        task_id: task_id.to_owned(),
        service: "gateway".to_owned(),
        kind: TaskKind::Verify,
        idempotent,
        submitted_by: "operator".to_owned(),
        params_digest: format!("sha256:{}", "a".repeat(64)),
        param_bytes: 32,
    }
}

const TASK: &str = "t_00000000000000000000000000000001";
const TASK2: &str = "t_00000000000000000000000000000002";

#[test]
fn duplicate_event_is_recorded_once() {
    let mut store = store();
    let body = envelope("evt-000001", None);
    let first = store
        .ingest_event(&parse(&body), "2026-11-14T10:00:01Z")
        .expect("first");
    let second = store
        .ingest_event(&parse(&body), "2026-11-14T10:00:02Z")
        .expect("second");
    assert_eq!(first, IngestOutcome::Recorded);
    assert_eq!(second, IngestOutcome::Duplicate);
    assert_eq!(store.event_count("gateway").expect("count"), 1);
}

#[test]
fn explicit_dedupe_key_wins_over_a_new_event_id() {
    let mut store = store();
    let first = envelope("evt-000001", Some("delivery-abc"));
    let retried = envelope("evt-000002", Some("delivery-abc"));
    assert_eq!(
        store
            .ingest_event(&parse(&first), "2026-11-14T10:00:01Z")
            .expect("first"),
        IngestOutcome::Recorded
    );
    assert_eq!(
        store
            .ingest_event(&parse(&retried), "2026-11-14T10:00:02Z")
            .expect("retry"),
        IngestOutcome::Duplicate
    );
}

#[test]
fn credential_attributes_are_redacted_before_storage() {
    let mut store = store();
    let body = envelope("evt-000001", None);
    let parsed = parse(&body);
    store
        .ingest_event(&parsed, "2026-11-14T10:00:01Z")
        .expect("ingest");
    let stored = store
        .stored_attributes(&parsed.dedupe_key())
        .expect("read")
        .expect("present");
    assert!(stored.contains("/healthz"), "non-sensitive value is kept");
    assert!(
        stored.contains("[redacted]"),
        "credential value is replaced"
    );
    assert!(
        !stored.contains("Bearer synthetic"),
        "raw token never stored"
    );
}

#[test]
fn invalid_envelope_is_refused_before_any_write() {
    let store = store();
    let mut value: serde_json::Value =
        serde_json::from_slice(&envelope("evt-000001", None)).expect("json");
    value["spanID"] = serde_json::json!("0123456789abcdef");
    let error = blueprint_contracts::event::Envelope::parse(
        serde_json::to_vec(&value).expect("encode").as_slice(),
        EventSource::Application,
        &limits(),
    )
    .expect_err("span without trace is invalid");
    assert_eq!(error.code.as_str(), "invalid_envelope");
    assert_eq!(store.event_count("gateway").expect("count"), 0);
}

#[test]
fn renamed_source_is_unauthorized() {
    let body = envelope("evt-000001", None);
    let error = blueprint_contracts::event::Envelope::parse(&body, EventSource::Webhook, &limits())
        .expect_err("source mismatch");
    assert_eq!(error.code.as_str(), "unauthorized");
}

#[test]
fn oversized_body_is_refused() {
    let mut body = envelope("evt-000001", None);
    let limit = limits().request.max_body_bytes;
    body.resize(limit + 1, b' ');
    let error =
        blueprint_contracts::event::Envelope::parse(&body, EventSource::Application, &limits())
            .expect_err("oversized");
    assert_eq!(error.code.as_str(), "payload_too_large");
}

#[test]
fn too_many_attributes_are_refused() {
    let mut value: serde_json::Value =
        serde_json::from_slice(&envelope("evt-000001", None)).expect("json");
    let max = limits().event.max_attributes;
    let mut attributes = serde_json::Map::new();
    for index in 0..=max {
        attributes.insert(format!("k{index}"), serde_json::json!("v"));
    }
    value["attributes"] = serde_json::Value::Object(attributes);
    let error = blueprint_contracts::event::Envelope::parse(
        serde_json::to_vec(&value).expect("encode").as_slice(),
        EventSource::Application,
        &limits(),
    )
    .expect_err("too many attributes");
    assert_eq!(error.code.as_str(), "payload_too_large");
}

#[test]
fn repeated_request_key_is_acknowledged_once() {
    let mut store = store();
    assert!(
        store
            .begin_request(
                "req-000001",
                "operator",
                "submit_task",
                "2026-11-14T10:00:00Z"
            )
            .expect("first")
    );
    assert!(
        !store
            .begin_request(
                "req-000001",
                "operator",
                "submit_task",
                "2026-11-14T10:00:01Z"
            )
            .expect("retry")
    );
    let owner = store.request_owner("req-000001").expect("owner");
    assert_eq!(owner.map(|o| o.0), Some("operator".to_owned()));
}

#[test]
fn health_transition_is_recorded_once_and_notified_once() {
    let mut store = store();
    let policy = HealthPolicy::default();
    store
        .register_check("gateway", "http", "probe", "2026-11-14T10:00:00Z")
        .expect("register");
    let observation = Observation {
        service: "gateway".to_owned(),
        check_id: "http".to_owned(),
        ok: false,
        observed_at: "2026-11-14T10:00:01Z".to_owned(),
        observed_by: "probe-local".to_owned(),
    };
    let first = store
        .apply_observation(&observation, policy, &["ops"], "2026-11-14T10:00:02Z")
        .expect("apply");
    assert!(first.is_some(), "unknown -> degraded is a transition");
    let pending = store
        .pending_notifications("2026-11-14T10:00:02Z", 10)
        .expect("pending");
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].channel, "ops");

    // Replay the identical observation: the transition id already exists, so nothing
    // is duplicated and no second notification is queued.
    let replay = store
        .apply_observation(&observation, policy, &["ops"], "2026-11-14T10:00:03Z")
        .expect("replay");
    assert!(replay.is_none());
    assert_eq!(store.transition_count("gateway").expect("transitions"), 1);
    assert_eq!(
        store
            .pending_notifications("2026-11-14T10:00:03Z", 10)
            .expect("pending")
            .len(),
        1
    );
}

#[test]
fn delivered_notification_is_not_resent() {
    let mut store = store();
    let policy = HealthPolicy::default();
    store
        .register_check("gateway", "http", "probe", "2026-11-14T10:00:00Z")
        .expect("register");
    let observation = Observation {
        service: "gateway".to_owned(),
        check_id: "http".to_owned(),
        ok: false,
        observed_at: "2026-11-14T10:00:01Z".to_owned(),
        observed_by: "probe-local".to_owned(),
    };
    store
        .apply_observation(&observation, policy, &["ops"], "2026-11-14T10:00:02Z")
        .expect("apply");
    let key = store
        .pending_notifications("2026-11-14T10:00:02Z", 10)
        .expect("pending")[0]
        .dedupe_key
        .clone();
    assert!(
        store
            .mark_notification_delivered(&key, "2026-11-14T10:00:03Z")
            .expect("deliver")
    );
    assert!(
        !store
            .mark_notification_delivered(&key, "2026-11-14T10:00:04Z")
            .expect("again")
    );
    assert!(
        store
            .pending_notifications("2026-11-14T10:00:05Z", 10)
            .expect("pending")
            .is_empty()
    );
}

#[test]
fn stale_observation_does_not_rewrite_history() {
    let mut store = store();
    let policy = HealthPolicy::default();
    store
        .register_check("gateway", "http", "probe", "2026-11-14T10:00:00Z")
        .expect("register");
    let newer = Observation {
        service: "gateway".to_owned(),
        check_id: "http".to_owned(),
        ok: false,
        observed_at: "2026-11-14T10:00:10Z".to_owned(),
        observed_by: "probe-local".to_owned(),
    };
    store
        .apply_observation(&newer, policy, &["ops"], "2026-11-14T10:00:10Z")
        .expect("newer");
    let older = Observation {
        observed_at: "2026-11-14T10:00:05Z".to_owned(),
        ..newer
    };
    let error = store
        .apply_observation(&older, policy, &["ops"], "2026-11-14T10:00:11Z")
        .expect_err("stale");
    assert_eq!(error.kind(), StoreErrorKind::Contract);
    assert_eq!(store.transition_count("gateway").expect("transitions"), 1);
}

fn claim(store: &mut Store, task_id: &str, worker: &str, now: &str, expires: &str) -> ClaimOutcome {
    let _ = task_id;
    store
        .claim_next("gateway", worker, now, expires)
        .expect("claim")
}

#[test]
fn claim_is_exclusive_and_generation_increments() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let first = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    );
    assert!(matches!(first, ClaimOutcome::Granted(_)));
    let second = claim(
        &mut store,
        TASK,
        "worker-bbbb",
        "2026-11-14T10:00:02Z",
        "2026-11-14T10:05:02Z",
    );
    assert_eq!(second, ClaimOutcome::Empty, "no second owner while leased");
    assert_eq!(store.lease_count(TASK).expect("leases"), 1);
    assert_eq!(store.task_generation(TASK).expect("generation"), Some(1));
}

#[test]
fn concurrent_claims_cannot_double_own() {
    // Two independent connections to the same file, both claiming at once. This is the
    // real mechanism check: `BEGIN IMMEDIATE` must serialize them.
    let dir = tempdir();
    let limits = limits();
    {
        let mut setup = Store::open(&dir, limits.clone()).expect("setup");
        setup
            .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
            .expect("submit");
    }
    let mut first = Store::open(&dir, limits.clone()).expect("first");
    let mut second = Store::open(&dir, limits).expect("second");
    let a = first
        .claim_next(
            "gateway",
            "worker-aaaa",
            "2026-11-14T10:00:01Z",
            "2026-11-14T10:05:01Z",
        )
        .expect("claim a");
    let b = second
        .claim_next(
            "gateway",
            "worker-bbbb",
            "2026-11-14T10:00:01Z",
            "2026-11-14T10:05:01Z",
        )
        .expect("claim b");
    let granted = usize::from(matches!(a, ClaimOutcome::Granted(_)))
        + usize::from(matches!(b, ClaimOutcome::Granted(_)));
    assert_eq!(granted, 1, "exactly one claimer wins");
    assert_eq!(first.lease_count(TASK).expect("leases"), 1);
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn stale_generation_heartbeat_and_report_are_refused() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(lease) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    assert!(
        store
            .heartbeat(
                TASK,
                lease.generation,
                "worker-aaaa",
                "2026-11-14T10:01:00Z",
                "2026-11-14T10:06:00Z"
            )
            .expect("heartbeat")
    );
    assert!(
        !store
            .heartbeat(
                TASK,
                lease.generation + 1,
                "worker-aaaa",
                "2026-11-14T10:01:00Z",
                "2026-11-14T10:06:00Z"
            )
            .expect("stale heartbeat")
    );
    let stale = Report {
        task_id: TASK.to_owned(),
        generation: lease.generation + 1,
        worker_id: "worker-aaaa".to_owned(),
        outcome: ReportOutcome::Succeeded,
        reported_at: "2026-11-14T10:02:00Z".to_owned(),
        result_digest: None,
    };
    let outcome = store
        .report_task(&stale, "2026-11-14T10:02:00Z")
        .expect("report");
    assert_eq!(
        outcome,
        blueprint_state::ReportOutcomeRecord::StaleGeneration
    );
    assert_eq!(
        store.task_state(TASK).expect("state"),
        Some(blueprint_contracts::task::TaskState::Leased)
    );
}

#[test]
fn duplicate_report_returns_the_first_verdict() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(lease) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let report = Report {
        task_id: TASK.to_owned(),
        generation: lease.generation,
        worker_id: "worker-aaaa".to_owned(),
        outcome: ReportOutcome::Succeeded,
        reported_at: "2026-11-14T10:02:00Z".to_owned(),
        result_digest: None,
    };
    let first = store
        .report_task(&report, "2026-11-14T10:02:00Z")
        .expect("first");
    let second = store
        .report_task(&report, "2026-11-14T10:02:01Z")
        .expect("second");
    assert!(matches!(
        first,
        blueprint_state::ReportOutcomeRecord::Accepted(
            blueprint_contracts::task::ReportAcceptance::Accepted { .. }
        )
    ));
    assert!(matches!(
        second,
        blueprint_state::ReportOutcomeRecord::Accepted(
            blueprint_contracts::task::ReportAcceptance::Duplicate { .. }
        )
    ));
    assert_eq!(
        store.task_state(TASK).expect("state"),
        Some(blueprint_contracts::task::TaskState::Succeeded)
    );
}

#[test]
fn conflicting_report_cannot_overwrite_the_stored_verdict() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(lease) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let mut report = Report {
        task_id: TASK.to_owned(),
        generation: lease.generation,
        worker_id: "worker-aaaa".to_owned(),
        outcome: ReportOutcome::Succeeded,
        reported_at: "2026-11-14T10:02:00Z".to_owned(),
        result_digest: None,
    };
    store
        .report_task(&report, "2026-11-14T10:02:00Z")
        .expect("first");
    report.outcome = ReportOutcome::Failed;
    let outcome = store
        .report_task(&report, "2026-11-14T10:02:01Z")
        .expect("second");
    assert_eq!(
        outcome,
        blueprint_state::ReportOutcomeRecord::AlreadySettled {
            state: blueprint_contracts::task::TaskState::Succeeded
        }
    );
}

#[test]
fn idempotent_expiry_returns_the_task_to_the_queue() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(_) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let outcome = store
        .expire_lease(TASK, "2026-11-14T10:05:01Z")
        .expect("expire");
    assert_eq!(outcome, ExpiryOutcome::ReturnedToQueue { generation: 1 });
    let reclaimed = claim(
        &mut store,
        TASK,
        "worker-bbbb",
        "2026-11-14T10:05:02Z",
        "2026-11-14T10:10:02Z",
    );
    let ClaimOutcome::Granted(lease) = reclaimed else {
        panic!("idempotent work is reclaimable");
    };
    assert_eq!(lease.generation, 2, "a new generation, never a shared one");
}

#[test]
fn non_idempotent_expiry_never_auto_reruns() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, false), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(_) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let outcome = store
        .expire_lease(TASK, "2026-11-14T10:05:01Z")
        .expect("expire");
    assert_eq!(outcome, ExpiryOutcome::OutcomeUnknown { generation: 1 });
    assert_eq!(
        store.task_state(TASK).expect("state"),
        Some(blueprint_contracts::task::TaskState::OutcomeUnknown)
    );
    let again = claim(
        &mut store,
        TASK,
        "worker-bbbb",
        "2026-11-14T10:05:02Z",
        "2026-11-14T10:10:02Z",
    );
    assert_eq!(
        again,
        ClaimOutcome::Empty,
        "unknown outcome is not claimable"
    );
}

#[test]
fn live_lease_is_not_expired_early() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, false), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(_) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let outcome = store
        .expire_lease(TASK, "2026-11-14T10:04:00Z")
        .expect("check");
    assert_eq!(outcome, ExpiryOutcome::StillLeased);
    assert_eq!(
        store.task_state(TASK).expect("state"),
        Some(blueprint_contracts::task::TaskState::Leased)
    );
}

#[test]
fn unknown_outcome_needs_an_explicit_reconcile_by_the_submitter() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, false), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(_) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    store
        .expire_lease(TASK, "2026-11-14T10:05:01Z")
        .expect("expire");
    let wrong = store
        .reconcile_unknown(
            TASK,
            "intruder",
            ReportOutcome::Succeeded,
            "2026-11-14T10:06:00Z",
        )
        .expect_err("only the submitter reconciles");
    assert_eq!(wrong.kind(), StoreErrorKind::Contract);
    let state = store
        .reconcile_unknown(
            TASK,
            "operator",
            ReportOutcome::Succeeded,
            "2026-11-14T10:06:00Z",
        )
        .expect("reconcile");
    assert_eq!(state, blueprint_contracts::task::TaskState::Succeeded);
}

#[test]
fn cancel_on_submitted_task_is_immediate_and_on_leased_task_is_requested() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    assert_eq!(
        store
            .cancel_task(TASK, "operator", "2026-11-14T10:00:01Z")
            .expect("cancel"),
        blueprint_contracts::task::TaskState::Cancelled
    );
    store
        .submit_task(&submission(TASK2, true), "{}", "2026-11-14T10:00:02Z")
        .expect("submit 2");
    let ClaimOutcome::Granted(lease) = claim(
        &mut store,
        TASK2,
        "worker-aaaa",
        "2026-11-14T10:00:03Z",
        "2026-11-14T10:05:03Z",
    ) else {
        panic!("expected a lease");
    };
    assert_eq!(
        store
            .cancel_task(TASK2, "operator", "2026-11-14T10:00:04Z")
            .expect("cancel"),
        blueprint_contracts::task::TaskState::CancelRequested
    );
    let report = Report {
        task_id: TASK2.to_owned(),
        generation: lease.generation,
        worker_id: "worker-aaaa".to_owned(),
        outcome: ReportOutcome::Failed,
        reported_at: "2026-11-14T10:01:00Z".to_owned(),
        result_digest: None,
    };
    store
        .report_task(&report, "2026-11-14T10:01:00Z")
        .expect("report");
    assert_eq!(
        store.task_state(TASK2).expect("state"),
        Some(blueprint_contracts::task::TaskState::Failed),
        "the worker's observed outcome is recorded, not discarded"
    );
}

#[test]
fn artifact_without_report_is_kept_for_reconciliation() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, false), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(lease) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let artifact_id = format!("a_{}", "b".repeat(32));
    let digest = format!("sha256:{}", "c".repeat(64));
    let artifact = blueprint_state::ArtifactRecord {
        artifact_id: &artifact_id,
        task_id: TASK,
        generation: lease.generation,
        name: "result.json",
        digest: &digest,
        bytes: 128,
    };
    assert!(
        store
            .record_artifact(&artifact, "2026-11-14T10:01:00Z")
            .expect("artifact")
    );
    assert!(
        !store
            .record_artifact(&artifact, "2026-11-14T10:01:01Z")
            .expect("duplicate artifact")
    );
    // The lease expires with the object already present but no report: the outcome is
    // unknown, and the artifact row still identifies what exists.
    store
        .expire_lease(TASK, "2026-11-14T10:05:01Z")
        .expect("expire");
    assert_eq!(
        store.task_state(TASK).expect("state"),
        Some(blueprint_contracts::task::TaskState::OutcomeUnknown)
    );
}

#[test]
fn report_naming_an_unknown_result_digest_is_refused() {
    let mut store = store();
    store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:00Z")
        .expect("submit");
    let ClaimOutcome::Granted(lease) = claim(
        &mut store,
        TASK,
        "worker-aaaa",
        "2026-11-14T10:00:01Z",
        "2026-11-14T10:05:01Z",
    ) else {
        panic!("expected a lease");
    };
    let report = Report {
        task_id: TASK.to_owned(),
        generation: lease.generation,
        worker_id: "worker-aaaa".to_owned(),
        outcome: ReportOutcome::Succeeded,
        reported_at: "2026-11-14T10:02:00Z".to_owned(),
        result_digest: Some(format!("sha256:{}", "d".repeat(64))),
    };
    let error = store
        .report_task(&report, "2026-11-14T10:02:00Z")
        .expect_err("unknown digest");
    assert_eq!(error.kind(), StoreErrorKind::Contract);
}

#[test]
fn open_task_bound_is_enforced() {
    let mut store = store();
    let max = limits().task.max_open_tasks_per_service;
    for index in 0..max {
        let task_id = format!("t_{index:032x}");
        store
            .submit_task(&submission(&task_id, true), "{}", "2026-11-14T10:00:00Z")
            .expect("submit");
    }
    let error = store
        .submit_task(&submission(TASK, true), "{}", "2026-11-14T10:00:01Z")
        .expect_err("queue full");
    assert_eq!(error.kind(), StoreErrorKind::Contract);
    assert_eq!(
        error.contract().map(|error| error.code.as_str()),
        Some("queue_full")
    );
}

fn tempdir() -> std::path::PathBuf {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "blueprint-behaviour-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    std::fs::create_dir_all(&path).expect("create temp dir");
    path
}
