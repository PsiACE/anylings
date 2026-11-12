#!/usr/bin/env bash
# Contract for the shared-gateway wiring: does the render itself produce the
# two Secrets and the env that the published DOKS tree contains?
#
# Before this check existed the two KSOPS Secrets and the runtime's gateway env
# were hand edits, so the next `just configure platform` silently dropped them.
# The check therefore asserts the property that matters — a re-render reproduces
# them — rather than matching template text.
#
# Offline: synthetic files in a scratch directory, the pinned public chart, no
# cluster, no provider credential, no real key.
set -euo pipefail
cd "$(dirname "$0")/../.."

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
umask 077
log() { printf '[gateway] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

age-keygen --output "$scratch/age.key" >/dev/null 2>&1
# Fixture inputs live beside the copied config so its relative paths resolve.
mkdir -p "$scratch/input/secret-inputs"
cp tests/config/gateway.toml "$scratch/input/cluster.toml"
printf '{"providers": [], "groups": [], "order": [], "groupOrder": [], "noAutoGroups": false}\n' \
  >"$scratch/input/secret-inputs/providers.json"
printf '[{"id": "synthetic", "name": "fixture", "secret": "sk-magpie-key-synthetic-fixture"}]\n' \
  >"$scratch/input/secret-inputs/caller-keys.json"
printf 'sk-magpie-key-synthetic-fixture\n' >"$scratch/input/secret-inputs/magpie-gateway-key"

gateway="$scratch/gateway"
if ! ANYLINGS_CONFIG="$scratch/input/cluster.toml" ANYLINGS_OUTPUT_DIR="$gateway" \
  SOPS_AGE_KEY_FILE="$scratch/age.key" just template configure \
  >"$scratch/render.log" 2>&1; then
  cat "$scratch/render.log" >&2
  die "rendering failed for the gateway input"
fi

root="$gateway/kubernetes/apps"
for f in default/magpie/seed.sops.yaml default/magpie/secret-generator.yaml \
  default/magpie/kustomization.yaml agents/t3-code/secret.sops.yaml \
  agents/t3-code/secret-generator.yaml agents/t3-code/kustomization.yaml; do
  [ -f "$root/$f" ] || die "the gateway input did not render $f"
done

# Both Applications must render their kustomize directory, or the Secrets above
# are published but never applied.
for app in default/magpie agents/t3-code; do
  [ "$(jq -r '.manifestPath' "$root/$app/config.json")" = "kubernetes/apps/$app" ] \
    || die "$app does not set manifestPath, so its Secrets would never be rendered"
done

# The runtime authenticates with a secretKeyRef; a literal value here would be a
# committed credential.
values="$root/agents/t3-code/values.yaml"
[ "$(yq '.controllers.["t3-code"].containers.app.env["BUB_MAGPIE_API_KEY"]
  .valueFrom.secretKeyRef.key' "$values")" = gateway-key ] \
  || die "the runtime does not read its gateway key from a secretKeyRef"
[ "$(yq '.controllers.["t3-code"].containers.app.env["BUB_MODEL"]' "$values")" = "magpie:group/deepseek-flash" ] \
  || die "the runtime no longer selects the accepted model group"
if grep -q 'sk-magpie-key-synthetic-fixture' "$values"; then
  die "the runtime values contain the gateway key in plaintext"
fi

# The seed is copied into /config only when absent, so a restart cannot
# overwrite stores the running gateway has since changed.
if ! SOPS_AGE_KEY_FILE="$scratch/age.key" \
  sops --config "$gateway/.sops.yaml" --decrypt \
    "$root/default/magpie/values.sops.yaml" \
  | grep -q 'if \[ ! -e /config/magpie/\$f \]'; then
  die "the seed init container would overwrite an existing /config/magpie store"
fi

# Nothing in the rendered tree may carry the fixture secret in plaintext.
if grep -rq 'sk-magpie-key-synthetic-fixture' "$gateway/kubernetes"; then
  die "the rendered tree carries a plaintext gateway key"
fi

# Defaults must stay inert: the base input publishes neither Secret.
base="$scratch/base"
if ! ANYLINGS_CONFIG="tests/config/base.toml" ANYLINGS_OUTPUT_DIR="$base" \
  SOPS_AGE_KEY_FILE="$scratch/age.key" just template configure \
  >"$scratch/base.log" 2>&1; then
  cat "$scratch/base.log" >&2
  die "rendering failed for the default input"
fi
for f in default/magpie/seed.sops.yaml agents/t3-code/secret.sops.yaml; do
  [ ! -e "$base/kubernetes/apps/$f" ] \
    || die "the default input renders $f without a configured gateway"
done
[ "$(jq -r '.manifestPath' "$base/kubernetes/apps/default/magpie/config.json")" = "" ] \
  || die "the default input sets manifestPath without a rendered manifest"

# A gateway key that the gateway itself never learned only yields 401s, so the
# validator must refuse that half-configured input.
printf '[repository]\nurl = "git://git.example.test/platform.git"\n[agents]\nimage_repository = "registry.example.test/anylings/t3-code"\ngateway_secret = "k"\n' \
  >"$scratch/half.toml"
if ANYLINGS_CONFIG="$scratch/half.toml" just check config >/dev/null 2>&1; then
  die "the validator accepted agents.gateway_secret without magpie.seed_dir"
fi

log "a re-render reproduces both gateway Secrets; defaults stay inert"
