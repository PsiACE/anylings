//! Routing and per-route behaviour.
//!
//! The route table is closed and small on purpose. Each route states the scope it
//! needs; a request that names a resource it lacks scope for is refused, not filtered,
//! so a caller cannot infer existence from a partial result.

use blueprint_contracts::error::Scope;
use blueprint_contracts::health::{HealthPolicy, Observation};
use blueprint_contracts::task::{Report, ReportOutcome, TaskKind, TaskSubmission};

use crate::{Failure, Principal, Service};

/// A host-independent request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub method: String,
    /// Path only, no query string.
    pub path: String,
    pub body: Vec<u8>,
    /// Request id used for retry dedup; supplied by the transport, never by the body.
    pub request_id: Option<String>,
}

impl Request {
    pub fn new(method: &str, path: &str, body: Vec<u8>) -> Self {
        Self {
            method: method.to_owned(),
            path: path.to_owned(),
            body,
            request_id: None,
        }
    }

    pub fn with_request_id(mut self, request_id: &str) -> Self {
        self.request_id = Some(request_id.to_owned());
        self
    }
}

/// A host-independent response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reply {
    pub status: u16,
    pub body: Vec<u8>,
}

impl Reply {
    pub fn json(status: u16, value: serde_json::Value) -> Self {
        Self {
            status,
            body: serde_json::to_vec(&value).unwrap_or_else(|_| b"{}".to_vec()),
        }
    }

    pub fn empty(status: u16) -> Self {
        Self {
            status,
            body: Vec::new(),
        }
    }

    pub fn json_value(&self) -> Option<serde_json::Value> {
        serde_json::from_slice(&self.body).ok()
    }
}

/// Dispatch one request. `/healthz` and `/readyz` are unauthenticated by design: they
/// report process liveness, not platform state, and expose nothing but a status code.
pub(crate) fn dispatch(
    service: &mut Service,
    request: &Request,
    principal: &Principal,
) -> Result<Reply, Failure> {
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/healthz") => Ok(Reply::empty(204)),
        ("GET", "/readyz") => readiness(service),
        ("POST", "/v1/events") => {
            let received_at = clock(service);
            service.ingest_event(&request.body, principal, &received_at)
        }
        ("POST", "/v1/tasks") => submit_task(service, request, principal),
        ("POST", "/v1/tasks/claim") => claim_task(service, request, principal),
        ("POST", "/v1/tasks/report") => report_task(service, request, principal),
        ("POST", "/v1/health/observe") => observe_health(service, request, principal),
        ("GET", "/v1/health") => read_health(service, principal),
        ("GET", "/v1/notifications/pending") => pending_notifications(service, principal),
        _ => Err(Failure::not_found("no such route")),
    }
}

/// Serialize the request body into `T`, refusing an oversized or malformed body with a
/// contract error rather than a host-level failure.
fn decode<T: serde::de::DeserializeOwned>(
    service: &Service,
    body: &[u8],
    what: &'static str,
) -> Result<T, Failure> {
    if body.len() > service.limits().request.max_body_bytes {
        return Err(Failure::from(blueprint_contracts::Rejection::new(
            blueprint_contracts::error::ErrorCode::PayloadTooLarge,
            "request body exceeds the configured maximum",
        )));
    }
    serde_json::from_slice(body).map_err(|_| {
        Failure::invalid(match what {
            "event" => "event envelope is not valid JSON",
            _ => "request body is not valid JSON",
        })
    })
}

fn clock(service: &Service) -> String {
    let _ = service;
    blueprint_state::format_utc_seconds(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
    )
}

fn submit_task(
    service: &mut Service,
    request: &Request,
    principal: &Principal,
) -> Result<Reply, Failure> {
    if !principal.has(Scope::TaskWrite) {
        return Err(Failure::forbidden("principal does not hold task:write"));
    }
    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct Submit {
        service: String,
        kind: TaskKind,
        idempotent: bool,
        params: serde_json::Value,
    }
    let body: Submit = decode(service, &request.body, "task")?;
    if body.service != body.service.to_lowercase() {
        return Err(Failure::invalid("service must be a lowercase DNS label"));
    }
    let params = serde_json::to_string(&body.params).map_err(|_| Failure::invalid("params"))?;
    let submitted_at = clock(service);
    // The task id is derived from the caller's request id, so a retried submission is
    // the same task rather than a second one.
    let request_id = request
        .request_id
        .as_deref()
        .ok_or_else(|| Failure::invalid("a submission requires a request id"))?;
    let task_id = blueprint_contracts::ids::prefixed_id(
        "t",
        &[&body.service, request_id, principal.name.as_str()],
    );
    let submission = TaskSubmission {
        task_id: task_id.clone(),
        service: body.service,
        kind: body.kind,
        idempotent: body.idempotent,
        submitted_by: principal.name.clone(),
        params_digest: blueprint_contracts::ids::digest(&[&params]),
        param_bytes: params.len(),
    };
    match service
        .store_mut()
        .submit_task(&submission, &params, &submitted_at)
    {
        Ok(()) => Ok(Reply::json(
            201,
            serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "taskID": task_id,
                "paramsDigest": submission.params_digest,
                "state": "submitted",
            }),
        )),
        // A retry of the same request is not an error: report the existing task.
        Err(blueprint_state::StateError::Contract(error))
            if error.code == blueprint_contracts::error::ErrorCode::Conflict =>
        {
            Ok(Reply::json(
                200,
                serde_json::json!({
                    "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                    "taskID": task_id,
                    "duplicate": true,
                }),
            ))
        }
        Err(error) => Err(Failure::from(error)),
    }
}

fn claim_task(
    service: &mut Service,
    request: &Request,
    principal: &Principal,
) -> Result<Reply, Failure> {
    if !principal.has(Scope::TaskWrite) {
        return Err(Failure::forbidden("principal does not hold task:write"));
    }
    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct Claim {
        service: String,
        #[serde(rename = "workerID")]
        worker_id: String,
    }
    let body: Claim = decode(service, &request.body, "claim")?;
    let now = clock(service);
    let lease_seconds = service.limits().task.lease_seconds;
    let expires_at = add_seconds(&now, lease_seconds);
    match service
        .store_mut()
        .claim_next(&body.service, &body.worker_id, &now, &expires_at)
        .map_err(Failure::from)?
    {
        blueprint_state::ClaimOutcome::Granted(lease) => Ok(Reply::json(
            200,
            serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "taskID": lease.task_id,
                "generation": lease.generation,
                "expiresAt": lease.expires_at,
            }),
        )),
        blueprint_state::ClaimOutcome::Empty => Ok(Reply::empty(204)),
        blueprint_state::ClaimOutcome::NotClaimable { reason } => {
            Err(Failure::from(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::Conflict,
                match reason {
                    "task state changed while claiming" => "task state changed while claiming",
                    _ => "task is not claimable",
                },
            )))
        }
    }
}

fn report_task(
    service: &mut Service,
    request: &Request,
    principal: &Principal,
) -> Result<Reply, Failure> {
    if !principal.has(Scope::TaskWrite) {
        return Err(Failure::forbidden("principal does not hold task:write"));
    }
    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct Body {
        #[serde(rename = "taskID")]
        task_id: String,
        generation: u32,
        #[serde(rename = "workerID")]
        worker_id: String,
        outcome: ReportOutcome,
        #[serde(rename = "resultDigest")]
        result_digest: Option<String>,
    }
    let body: Body = decode(service, &request.body, "report")?;
    let now = clock(service);
    let report = Report {
        task_id: body.task_id,
        generation: body.generation,
        worker_id: body.worker_id,
        outcome: body.outcome,
        reported_at: now.clone(),
        result_digest: body.result_digest,
    };
    match service
        .store_mut()
        .report_task(&report, &now)
        .map_err(Failure::from)?
    {
        blueprint_state::ReportOutcomeRecord::Accepted(acceptance) => Ok(Reply::json(
            200,
            serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "state": acceptance.state().as_str(),
                "duplicate": matches!(
                    acceptance,
                    blueprint_contracts::task::ReportAcceptance::Duplicate { .. }
                ),
            }),
        )),
        blueprint_state::ReportOutcomeRecord::StaleGeneration => {
            Err(Failure::from(blueprint_contracts::Rejection::new(
                blueprint_contracts::error::ErrorCode::LeaseLost,
                "report generation does not hold the live lease",
            )))
        }
        blueprint_state::ReportOutcomeRecord::AlreadySettled { state } => Ok(Reply::json(
            200,
            serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "state": state.as_str(),
                "duplicate": false,
                "settled": true,
            }),
        )),
    }
}

fn observe_health(
    service: &mut Service,
    request: &Request,
    principal: &Principal,
) -> Result<Reply, Failure> {
    if !principal.has(Scope::HealthRead) {
        return Err(Failure::forbidden("principal does not hold health:read"));
    }
    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct Body {
        service: String,
        #[serde(rename = "checkID")]
        check_id: String,
        ok: bool,
        // Latency is accepted for parity with the observation contract but is not
        // persisted: health state is derived from outcomes, and storing a number the
        // state machine does not use would be unverifiable data.
        #[serde(default, rename = "latencyMs")]
        latency_ms: Option<u64>,
    }
    let body: Body = decode(service, &request.body, "observation")?;
    let _ = body.latency_ms;
    let now = clock(service);
    service
        .store_mut()
        .register_check(&body.service, &body.check_id, "probe", &now)
        .map_err(Failure::from)?;
    let observation = Observation {
        service: body.service,
        check_id: body.check_id,
        ok: body.ok,
        observed_at: now.clone(),
        observed_by: principal.name.clone(),
    };
    let transition = service
        .store_mut()
        .apply_observation(
            &observation,
            HealthPolicy::default(),
            &["ops-capture"],
            &now,
        )
        .map_err(Failure::from)?;
    Ok(Reply::json(
        200,
        match transition {
            Some(transition) => serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "state": transition.to.as_str(),
                "transitionID": transition.transition_id,
            }),
            None => serde_json::json!({
                "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                "state": "unchanged",
            }),
        },
    ))
}

fn read_health(service: &mut Service, principal: &Principal) -> Result<Reply, Failure> {
    if !principal.has(Scope::HealthRead) {
        return Err(Failure::forbidden("principal does not hold health:read"));
    }
    let limit = service.limits().page_size(None) as i64;
    let rows = service.store().list_checks(limit).map_err(Failure::from)?;
    Ok(Reply::json(
        200,
        serde_json::json!({
            "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
            "checks": rows,
        }),
    ))
}

fn pending_notifications(service: &mut Service, principal: &Principal) -> Result<Reply, Failure> {
    if !principal.has(Scope::HealthRead) {
        return Err(Failure::forbidden("principal does not hold health:read"));
    }
    let now = clock(service);
    let limit = service.limits().page_size(None);
    let pending = service
        .store()
        .pending_notifications(&now, limit)
        .map_err(Failure::from)?;
    Ok(Reply::json(
        200,
        serde_json::json!({
            "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
            "pending": pending.iter().map(|item| serde_json::json!({
                "dedupeKey": item.dedupe_key,
                "transitionID": item.transition_id,
                "channel": item.channel,
            })).collect::<Vec<_>>(),
        }),
    ))
}

/// `YYYY-MM-DDTHH:MM:SSZ` plus a whole number of seconds.
fn add_seconds(timestamp: &str, seconds: i64) -> String {
    blueprint_state::format_utc_seconds(parse_utc_seconds(timestamp) + seconds.max(0) as u64)
}

fn parse_utc_seconds(timestamp: &str) -> u64 {
    // Fixed-format parse of the only shape this module produces.
    let bytes = timestamp.as_bytes();
    if bytes.len() != 20 {
        return 0;
    }
    let year: u64 = timestamp[0..4].parse().unwrap_or(1970);
    let month: u64 = timestamp[5..7].parse().unwrap_or(1);
    let day: u64 = timestamp[8..10].parse().unwrap_or(1);
    let hour: u64 = timestamp[11..13].parse().unwrap_or(0);
    let minute: u64 = timestamp[14..16].parse().unwrap_or(0);
    let second: u64 = timestamp[17..19].parse().unwrap_or(0);
    days_from_civil(year as i64, month as u32, day as u32) as u64 * 86_400
        + hour * 3600
        + minute * 60
        + second
}

/// Inverse of `civil_from_days`, so a lease deadline can be computed without a
/// date-time dependency.
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let m = month as u64;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + day as u64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe as i64 - 719_468
}

/// `/readyz` reports whether the database answers, not whether the platform is healthy.
fn readiness(service: &mut Service) -> Result<Reply, Failure> {
    match service.store().check_integrity() {
        Ok(()) => Ok(Reply::empty(204)),
        Err(_) => Err(Failure::from(blueprint_contracts::Rejection::new(
            blueprint_contracts::error::ErrorCode::Unavailable,
            "state database is not ready",
        ))),
    }
}
