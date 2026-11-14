#!/usr/bin/env bash
# Offline checks for the shared platform core.
#
# Everything here runs on a workstation with no cluster, no network and no
# credentials: schema conformance, SQLite behaviour, restart durability and the HTTP
# request contract. It deliberately does not build or run the Spin component, because
# that needs a wasm target and the Spin CLI; `tests/blueprint/core/README.md` records
# precisely what is therefore NOT-RUN.
set -euo pipefail

cd "$(dirname "$0")/../../.."

core="platform/core"
log() { printf '[blueprint-core] %s\n' "$*" >&2; }

# A project-local cargo home keeps the check hermetic: nothing is installed into HOME
# and the resolved graph is the committed lockfile.
export CARGO_HOME="${CARGO_HOME:-$PWD/.local/cargo-home}"
mkdir -p "$CARGO_HOME"

[ -f "$core/Cargo.lock" ] || { log 'error: platform/core/Cargo.lock is missing'; exit 1; }

log 'format check'
cargo fmt --manifest-path "$core/Cargo.toml" --all -- --check

log 'lint'
cargo clippy --manifest-path "$core/Cargo.toml" --all-targets -- -D warnings

log 'contracts, state and request contract'
cargo test --manifest-path "$core/Cargo.toml" --offline

log 'contract schema inventory'
missing=0
for schema in event error limits dataset task health; do
    [ -f "platform/contracts/v1/$schema.schema.json" ] || { log "error: missing $schema schema"; missing=1; }
done
[ "$missing" -eq 0 ] || exit 1

# The limits document is the single source of the numbers; the code asserts equality in
# its own tests, so a JSON edit that the code did not follow fails above.
[ -f platform/contracts/limits.json ] || { log 'error: limits.json is missing'; exit 1; }

log 'spin manifest source resolves inside the repository'
manifest="platform/spin.toml"
[ -f "$manifest" ] || { log 'error: platform/spin.toml is missing'; exit 1; }
source_path="$(sed -n 's/^source = "\(.*\)"$/\1/p' "$manifest" | head -1)"
[ -n "$source_path" ] || { log 'error: no component source in the manifest'; exit 1; }
case "$source_path" in
    /*) log "error: component source must be relative, got $source_path"; exit 1 ;;
esac
# Spin resolves `source` against the manifest's own directory, so the guard must too.
resolved="$(dirname "$manifest")/$source_path"
case "$(realpath -m "$resolved")" in
    "$PWD"/*) ;;
    *) log "error: component source escapes the repository: $source_path"; exit 1 ;;
esac
case "$source_path" in
    ../*) log "error: component source walks above the manifest directory: $source_path"; exit 1 ;;
esac

log 'ok'
