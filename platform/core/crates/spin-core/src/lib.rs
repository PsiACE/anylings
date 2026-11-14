//! Spin HTTP component for the shared platform core.
//!
//! This crate is the Wasm adapter and nothing else: it translates Spin's request and
//! response types into `blueprint_service::{Request, Reply}`. Routing, authorization,
//! validation and every durable decision live in the host-independent crates, where
//! native tests exercise them against real SQLite.
//!
//! # Two blockers, stated exactly
//!
//! This component is compiled but **not runnable** in this environment. Neither blocker
//! is hidden behind a substitute:
//!
//! 1. **No Wasm target or Spin CLI on the host.** `rustup target list --installed`
//!    reports only `wasm32-unknown-unknown`, `x86_64-unknown-linux-gnu` and
//!    `x86_64-unknown-linux-musl`; `spin` and `wasmtime` are absent from `PATH`.
//!    Adding a target or installing a CLI mutates the host toolchain, which this task
//!    may not do. `spin-sdk` builds only for wasm, so it is a `cfg(target_family =
//!    "wasm")` dependency and off-Wasm this crate compiles to the stub below.
//! 2. **No Spin-SQLite executor in the state layer yet.** `spin_sdk::sqlite` hands out
//!    an *async* `Connection` over a WIT interface, not a `rusqlite::Connection`; the
//!    two cannot be swapped. Serving real requests therefore needs the state layer's
//!    statements behind a second executor, with the same `BEGIN IMMEDIATE` ordering.
//!    Writing that executor blind — with no way to compile or run it — would produce
//!    unverifiable code that looks finished. It is left as explicit remaining work in
//!    `docs/explanation/blueprint-task-map.md` instead.
//!
//! Because of (2), the component refuses stateful routes with `503 unavailable` rather
//! than opening a different database or answering from memory. A visible refusal is
//! honest; a silent fallback would mean the running process is not the thing under
//! test.
//!
//! # Building it once the target is available
//!
//! ```text
//! rustup target add wasm32-wasip2
//! cd platform/core && cargo build -p spin-core --release --target wasm32-wasip2
//! ```
//!
//! `platform/spin.toml` is the reviewed manifest and carries the route table, the
//! database handle and the filesystem grant; the module it loads is a build output and
//! is not committed.

/// Contract version this build implements, so a diagnostics endpoint can answer
/// without touching storage.
pub fn contract_version() -> &'static str {
    blueprint_contracts::CONTRACT_VERSION
}

#[cfg(target_family = "wasm")]
mod component {
    use spin_sdk::http::{IntoResponse as _, Request, Response};
    use spin_sdk::http_component;

    /// Liveness and readiness only. A ready component additionally needs a state
    /// backend, which is blocker 2 in the crate documentation.
    #[http_component]
    fn handle(request: Request) -> Response {
        match request.uri().path() {
            // The process is alive. This says nothing about the platform's health and
            // deliberately does not depend on storage.
            "/healthz" => Response::new(204, ()),
            // Not ready: a request that needs state cannot be served correctly.
            "/readyz" => Response::new(503, ()),
            _ => {
                let body = serde_json::json!({
                    "schemaVersion": blueprint_contracts::CONTRACT_VERSION,
                    "code": "unavailable",
                    "message": "state backend is not wired into this component build",
                    "retryable": true,
                });
                Response::builder()
                    .status(503)
                    .header("content-type", "application/json")
                    .body(body.to_string())
                    .build()
                    .into_response()
            }
        }
    }
}

#[cfg(not(target_family = "wasm"))]
#[allow(dead_code)]
fn native_note() -> &'static str {
    // A compile-time statement of intent: there is no native HTTP entry point here, so
    // `cargo run` cannot be mistaken for running Spin.
    "spin-core has no native entry point; run `cargo test -p blueprint-service`"
}
