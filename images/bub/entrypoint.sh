#!/usr/bin/env bash
# Start the Bub runtime's web surface: the T3 Code web server, with the Bub ACP
# executable available to it as the provider it launches.
#
# Every input here is generic: a state directory, a workspace directory and a
# bind address. Which clients reach the server, and which provider T3 launches,
# are instance decisions and are not encoded in the image.
#
# The T3CODE_* variables below are the upstream web server's own contract, not a
# leftover name: `t3` reads them and the existing claim's layout depends on
# T3CODE_HOME. They stay as upstream defines them.
set -euo pipefail

log() { printf '[bub] %s\n' "$*" >&2; }
die() {
    log "error: $*"
    exit 1
}

T3CODE_HOME="${T3CODE_HOME:-/home/t3/.t3}"
T3CODE_PROJECT_ROOT="${T3CODE_PROJECT_ROOT:-/home/t3/workspace}"
T3CODE_HOST="${T3CODE_HOST:-0.0.0.0}"
T3CODE_PORT="${T3CODE_PORT:-3773}"

command -v t3 >/dev/null 2>&1 || die "the t3 executable is not on PATH"
command -v bub >/dev/null 2>&1 || die "the bub executable is not on PATH"

# Running as root would create state files that the mounted volume's real owner
# cannot then write, so refuse instead of starting with the wrong owner.
[ "$(id -u)" -ne 0 ] || die "refusing to run as root; use the image's nonroot account"

# T3 keeps its database, sessions and provider runtime state under the data
# directory and writes project checkouts under the workspace root. A volume
# mounted over their parent starts empty, so create them here rather than
# requiring the operator to pre-populate a fresh claim.
for directory in "$T3CODE_HOME" "$T3CODE_PROJECT_ROOT"; do
    [ -d "$directory" ] || mkdir -p "$directory" 2>/dev/null || true
    # Checked after creation: a read-only or wrongly owned mount then fails
    # here, at startup, instead of at the first request.
    [ -d "$directory" ] || die "required directory is missing: $directory"
    [ -w "$directory" ] || die "required directory is not writable: $directory"
done

case "${T3CODE_AUTO_BOOTSTRAP_PROJECT:-false}" in
    true | 1) bootstrap_project=true ;;
    false | 0 | "") bootstrap_project=false ;;
    *) die "T3CODE_AUTO_BOOTSTRAP_PROJECT must be true or false, got: $T3CODE_AUTO_BOOTSTRAP_PROJECT" ;;
esac

args=(
    serve
    --mode web
    --host "$T3CODE_HOST"
    --port "$T3CODE_PORT"
    --base-dir "$T3CODE_HOME"
    --no-browser
)
# Registering the mounted workspace as a project is opt-in: a workspace that
# already carries T3 state should not gain a project on every restart.
[ "$bootstrap_project" = true ] && args+=(--auto-bootstrap-project-from-cwd)
[ -n "${T3CODE_LOG_LEVEL:-}" ] && args+=(--log-level "$T3CODE_LOG_LEVEL")

log "Bub $(bub --version 2>/dev/null | head -1) with T3 Code $(t3 --version 2>/dev/null | head -1)"
log "state ${T3CODE_HOME}, workspace ${T3CODE_PROJECT_ROOT}, bind ${T3CODE_HOST}:${T3CODE_PORT}"

# The workspace root is the positional cwd, so T3 starts with the project the
# operator mounted rather than with the image's build directory.
cd "$T3CODE_PROJECT_ROOT"
exec t3 "${args[@]}" "$T3CODE_PROJECT_ROOT"
