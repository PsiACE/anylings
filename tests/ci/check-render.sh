#!/usr/bin/env bash
# The render assertions the platform render job makes, in one place.
#
# Both the pull-request workflow and tests/ci/local-gate.sh call this, so a
# confidentiality check cannot be present in one entry point and missing from the
# other. It runs only against generated output and synthetic fixtures.
#
# Usage: bash tests/ci/check-render.sh <base|cloudflare-tunnel> [<rendered dir>]
#
# The second argument defaults to $ANYLINGS_OUTPUT_DIR, then to the render
# recipe's own default, so a caller that set neither still checks what it built. The fixture mode is the
# input case, and it selects the assertions: nothing here reads real credentials
# or a user's configuration, and the synthetic tunnel token is written and
# grepped by this script, never taken from the environment.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# The only two supported workflow inputs; the mode is not a free-form switch.
SYNTHETIC_TOKEN="synthetic-ci-tunnel-token"

fail() { printf '%s\n' "$*" >&2; exit 1; }
log() { printf '[render-check] %s\n' "$*" >&2; }

config="${1:-}"
rendered="${2:-${ANYLINGS_OUTPUT_DIR:-$REPO_ROOT/.local/rendered}}"
case "$config" in
  base | cloudflare-tunnel) ;;
  *) fail "usage: bash tests/ci/check-render.sh base|cloudflare-tunnel [rendered-dir]" ;;
esac
rendered="$(realpath -m -- "$rendered")"
[ -f "$rendered/.anylings-rendered" ] \
  || fail "not an owned render output: $rendered"
[ -f "$rendered/kubernetes/bootstrap.yaml" ] \
  || fail "render output has no bootstrap entrypoint: $rendered"

if [ "$config" = cloudflare-tunnel ]; then
  log "assert the tunnel Secret is delivered from the published prefix and is not plaintext"
  # Decryption is the point of the assertion, so a missing identity must fail
  # with a reason instead of looking like a wrong token.
  [ -s "${SOPS_AGE_KEY_FILE:-}" ] \
    || fail "SOPS_AGE_KEY_FILE must name the isolated render identity"
  # The Git-file generator passes manifestPath to the Application's Git source;
  # exercise that published KSOPS path without exposing it.
  config_json="$rendered/kubernetes/apps/network/cloudflare-tunnel/config.json"
  [ -f "$config_json" ] || fail "the tunnel Application config is missing: $config_json"
  source="$(jq -r '.manifestPath' "$config_json")"
  case "$source" in
    clusters/tunnel/*) source="${source#clusters/tunnel/}" ;;
    *) fail "tunnel Secret source is outside the configured Git prefix: $source" ;;
  esac
  kustomize build --enable-alpha-plugins --enable-exec "$rendered/$source" \
    | yq -e 'select(.kind == "Secret" and .stringData.TUNNEL_TOKEN != null) | .stringData.TUNNEL_TOKEN == "synthetic-ci-tunnel-token"' >/dev/null \
    || fail "the published prefix does not decrypt to the synthetic tunnel token"
  if grep -rqF "$SYNTHETIC_TOKEN" "$rendered/kubernetes"; then
    fail "synthetic tunnel token remains in plaintext under $rendered/kubernetes"
  fi
fi

# Rendered output must already match the format-yaml hook, otherwise every
# `just configure platform` shows up as formatting churn. Only tracked-file
# types are checked: rendered plaintext is never inspected.
log "assert rendered output is formatted"
oxfmt --check "$rendered/.sops.yaml" "$rendered/kubernetes" >/dev/null \
  || fail "rendered output is not formatted; run oxfmt on the rendered tree"

# One confidentiality contract through the public output check. filestatus still
# says encrypted after a plaintext Helm value is added, so the mutation proves
# `just check platform` looks at every scalar. The original file is restored on
# any exit, including a failure inside the check.
if [ "$config" = base ]; then
  log "assert partially encrypted output is rejected"
  values="$(find "$rendered/kubernetes/apps" -name 'values.sops.yaml' -print -quit)"
  [ -n "$values" ] || fail "no encrypted values file was rendered"
  backup="$(mktemp)"
  trap 'mv -f "$backup" "$values"' EXIT
  cp "$values" "$backup"
  yq -i '.publicationCheck = "synthetic-unencrypted"' "$values"
  sops filestatus "$values" | jq -e '.encrypted == true' >/dev/null \
    || fail "the mutation did not leave a file that SOPS still reports as encrypted"
  if ANYLINGS_OUTPUT_DIR="$rendered" just check platform >/dev/null 2>&1; then
    fail "accepted partially encrypted publication output"
  fi
  mv -f "$backup" "$values"
  trap - EXIT
  ANYLINGS_OUTPUT_DIR="$rendered" just check platform >/dev/null
fi

log "render assertions passed for $config"
