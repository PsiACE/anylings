#!/usr/bin/env bash
# Run the repository's offline checks the way the pull-request gate does, on
# the current checkout.
#
# This is a convenience aggregate, not a second implementation: every step is
# an existing recipe or script, and it reuses the tools the caller already has
# (the mise/uv environment, the OpenTofu plugin cache). It deliberately starts
# no cluster, no k0s, no sudo and no cloud API. It is an offline check, not a
# runtime rehearsal: it does not reproduce the native suite, does not need the
# opt-in CI label, and is not evidence about DOKS CSI, Cloudflare or multi-host
# behavior.
set -euo pipefail
cd "$(dirname "$0")/../.."

work="$(mktemp -d)"
umask 077
trap 'rm -rf "$work"' EXIT

log() { printf '[local-gate] %s\n' "$*" >&2; }

log "infrastructure format"
tofu fmt -check -recursive -diff

for root in platform k0s doks cloudflare; do
  log "OpenTofu $root validate"
  # Keep plugins and backend data out of the work tree.
  export TF_DATA_DIR="$work/tofu-$root"
  tofu -chdir="infra/tofu/$root" init -backend=false -input=false -no-color >"$work/init-$root.log" 2>&1 \
    || { tail -20 "$work/init-$root.log" >&2; exit 1; }
  tofu -chdir="infra/tofu/$root" validate -no-color
done

log "DOKS offline pool contracts"
# The mocked provider still needs the already-initialized DOKS plugin cache, so
# the test reuses that data directory instead of the last loop iteration's.
TF_DATA_DIR="$work/tofu-doks" tofu -chdir=infra/tofu/doks test -no-color
unset TF_DATA_DIR

# Both supported render inputs, each in its own private directory, then the
# repository's own confidentiality and Kubernetes schema checks. Nothing is
# written to the checkout: no age identity, no rendered plaintext.
for config in base cloudflare-tunnel; do
  log "platform render ($config)"
  inputs="$work/inputs-$config"
  mkdir -p "$inputs"
  export ANYLINGS_CONFIG="$inputs/cluster.toml"
  export SOPS_AGE_KEY_FILE="$inputs/age.key"
  export ANYLINGS_OUTPUT_DIR="$work/rendered-$config"
  export ANYLINGS_BOOTSTRAP_MANIFESTS_DIR="$work/bootstrap-manifests-$config"
  cp "tests/config/$config.toml" "$ANYLINGS_CONFIG"
  if [ "$config" = cloudflare-tunnel ]; then
    # Same synthetic token the workflow writes; check-render.sh greps for it.
    mkdir -p "$inputs/.local"
    printf 'synthetic-ci-tunnel-token\n' >"$inputs/.local/tunnel.token"
  fi
  just init platform
  just configure platform
  bash tests/ci/check-render.sh "$config"
  bash "$ANYLINGS_OUTPUT_DIR/bootstrap/bootstrap.sh" render
  unset ANYLINGS_CONFIG SOPS_AGE_KEY_FILE ANYLINGS_OUTPUT_DIR ANYLINGS_BOOTSTRAP_MANIFESTS_DIR
done

log "workflow and safety contracts"
bash tests/ci/check-workflow-gate.sh
bash tests/ci/check-safety.sh
bash tests/ci/check-k0s-reset.sh

log "workflow lint"
zizmor --offline .github/workflows

log "offline checks passed; runtime acceptance is still unevidenced"
