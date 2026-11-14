//! Restart and durability behaviour on a real on-disk database.
//!
//! These are the tests that make the storage claims checkable: a state change survives
//! a process restart, the WAL sidecar is checkpointed rather than left to grow, the
//! directory permissions of the database file are what the operator set, and a restored
//! copy opens and passes integrity checks. They run as an external test binary, so the
//! "restart" is a genuinely new `Store` against the same directory.

use blueprint_contracts::limits::Limits;
use blueprint_state::Store;

const LIMITS: &str = include_str!("../../../../contracts/limits.json");

fn limits() -> Limits {
    Limits::from_json(LIMITS.as_bytes()).expect("limits")
}

fn scratch(label: &str) -> std::path::PathBuf {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "blueprint-restart-{label}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    std::fs::create_dir_all(&path).expect("create scratch dir");
    path
}

fn seed_event(store: &mut Store, event_id: &str) {
    let body = serde_json::to_vec(&serde_json::json!({
        "schemaVersion": "1",
        "eventID": event_id,
        "source": "application",
        "time": "2026-11-14T10:00:00Z",
        "type": "request.completed",
        "service": "gateway"
    }))
    .expect("encode");
    let envelope = blueprint_contracts::event::Envelope::parse(
        &body,
        blueprint_contracts::event::EventSource::Application,
        &limits(),
    )
    .expect("valid");
    store
        .ingest_event(&envelope, "2026-11-14T10:00:01Z")
        .expect("ingest");
}

#[test]
fn events_survive_a_restart_and_stay_deduped() {
    let dir = scratch("events");
    seed_event(
        &mut Store::open(&dir, limits()).expect("first"),
        "req-aaaaaaaa",
    );
    // New process, same directory.
    let mut store = Store::open(&dir, limits()).expect("second");
    assert_eq!(store.event_count("gateway").expect("count"), 1);
    // The dedup key survived, so a redelivery after restart is still a duplicate.
    let body = serde_json::to_vec(&serde_json::json!({
        "schemaVersion": "1",
        "eventID": "req-aaaaaaaa",
        "source": "application",
        "time": "2026-11-14T10:00:00Z",
        "type": "request.completed",
        "service": "gateway"
    }))
    .expect("encode");
    let envelope = blueprint_contracts::event::Envelope::parse(
        &body,
        blueprint_contracts::event::EventSource::Application,
        &limits(),
    )
    .expect("valid");
    assert_eq!(
        store
            .ingest_event(&envelope, "2026-11-14T10:00:02Z")
            .expect("retry"),
        blueprint_state::IngestOutcome::Duplicate
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_checkpoint_leaves_no_unbounded_wal_and_the_file_reopens() {
    let dir = scratch("wal");
    {
        let mut store = Store::open(&dir, limits()).expect("open");
        for index in 0..64 {
            seed_event(&mut store, &format!("req-{index:08}"));
        }
        store.checkpoint_truncate().expect("checkpoint");
        store.check_integrity().expect("integrity");
    }
    let wal = dir.join("state.db-wal");
    let wal_bytes = std::fs::metadata(&wal).map(|meta| meta.len()).unwrap_or(0);
    assert_eq!(wal_bytes, 0, "a truncating checkpoint empties the WAL");
    let store = Store::open(&dir, limits()).expect("reopen");
    assert_eq!(store.event_count("gateway").expect("count"), 64);
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn the_database_file_keeps_the_operator_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let dir = scratch("perms");
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).expect("chmod");
    {
        let mut store = Store::open(&dir, limits()).expect("open");
        seed_event(&mut store, "req-perm0001");
        store.checkpoint_truncate().expect("checkpoint");
    }
    let mode = std::fs::metadata(dir.join("state.db"))
        .expect("stat")
        .permissions()
        .mode()
        & 0o777;
    // The core never widens permissions; a private directory yields a private file.
    assert_eq!(mode & 0o077, 0, "no group or other access, got {mode:o}");
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_copied_checkpointed_database_restores_into_a_new_directory() {
    // This is the restore contract P2's Litestream path must satisfy: stop writers,
    // checkpoint, copy, remove stale sidecars, open, verify.
    let source = scratch("restore-src");
    {
        let mut store = Store::open(&source, limits()).expect("open");
        for index in 0..16 {
            seed_event(&mut store, &format!("req-r{index:07}"));
        }
        store.checkpoint_truncate().expect("checkpoint");
    }
    let target = scratch("restore-dst");
    std::fs::copy(source.join("state.db"), target.join("state.db")).expect("copy");
    // Stale sidecars must never travel with a restore.
    for sidecar in ["state.db-wal", "state.db-shm"] {
        let stale = target.join(sidecar);
        if stale.exists() {
            std::fs::remove_file(stale).expect("remove stale sidecar");
        }
    }
    let store = Store::open(&target, limits()).expect("open restored");
    store.check_integrity().expect("restored integrity");
    assert_eq!(store.event_count("gateway").expect("count"), 16);

    // And the restored copy is dedup-consistent with the original.
    let mut restored = store;
    let body = serde_json::to_vec(&serde_json::json!({
        "schemaVersion": "1",
        "eventID": "req-r0000000",
        "source": "application",
        "time": "2026-11-14T10:00:00Z",
        "type": "request.completed",
        "service": "gateway"
    }))
    .expect("encode");
    let envelope = blueprint_contracts::event::Envelope::parse(
        &body,
        blueprint_contracts::event::EventSource::Application,
        &limits(),
    )
    .expect("valid");
    assert_eq!(
        restored
            .ingest_event(&envelope, "2026-11-14T10:00:05Z")
            .expect("retry"),
        blueprint_state::IngestOutcome::Duplicate
    );
    std::fs::remove_dir_all(&source).ok();
    std::fs::remove_dir_all(&target).ok();
}

#[test]
fn a_live_lease_survives_restart_and_still_blocks_a_second_owner() {
    let dir = scratch("lease");
    let task_id = format!("t_{}", "e".repeat(32));
    {
        let mut store = Store::open(&dir, limits()).expect("open");
        store
            .submit_task(
                &blueprint_contracts::task::TaskSubmission {
                    task_id: task_id.clone(),
                    service: "gateway".to_owned(),
                    kind: blueprint_contracts::task::TaskKind::Verify,
                    idempotent: false,
                    submitted_by: "operator".to_owned(),
                    params_digest: format!("sha256:{}", "f".repeat(64)),
                    param_bytes: 8,
                },
                "{}",
                "2026-11-14T10:00:00Z",
            )
            .expect("submit");
        store
            .claim_next(
                "gateway",
                "worker-local-01",
                "2026-11-14T10:00:01Z",
                "2026-11-14T10:05:01Z",
            )
            .expect("claim");
    }
    let mut store = Store::open(&dir, limits()).expect("reopen");
    assert_eq!(
        store.task_state(&task_id).expect("state"),
        Some(blueprint_contracts::task::TaskState::Leased),
        "the lease is durable, not process memory"
    );
    // A competing worker after restart still cannot take it.
    assert_eq!(
        store
            .claim_next(
                "gateway",
                "worker-local-02",
                "2026-11-14T10:00:02Z",
                "2026-11-14T10:05:02Z"
            )
            .expect("claim"),
        blueprint_state::ClaimOutcome::Empty
    );
    std::fs::remove_dir_all(&dir).ok();
}
