#!/usr/bin/env bash
# Persistence contract for the Magpie /config volume, exercised through the
# public config inputs and the pinned chart rather than by matching template
# text.
#
# The default input keeps the ephemeral emptyDir. An input that names a claim
# renders a pod volume that references that claim and renders no
# PersistentVolumeClaim object, so the claim stays owned outside this Git tree.
# Invalid names are rejected by the existing config validator. No cluster,
# claim, provider or credential is used; only the pinned public chart's
# templates run.
set -euo pipefail
cd "$(dirname "$0")/../.."

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
umask 077
log() { printf '[magpie-claim] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

# Render one config input into its own output tree.
render() {
  local config="$1" out="$2" log="$scratch/render.log"
  if ! ANYLINGS_CONFIG="$config" ANYLINGS_OUTPUT_DIR="$out" \
    SOPS_AGE_KEY_FILE="$scratch/age.key" just template render >"$log" 2>&1; then
    cat "$log" >&2
    die "rendering failed for $config"
  fi
  [ -f "$out/kubernetes/apps/default/magpie/values.sops.yaml" ] \
    || die "magpie values were not rendered for $config"
}

# Render that tree with the chart coordinates the platform itself publishes, so
# this check follows the pinned chart instead of restating it.
template() {
  local rendered="$1" config chart_repo chart chart_version chart_file
  config="$rendered/kubernetes/apps/default/magpie/config.json"
  chart_repo="$(jq -r '.chartRepo' "$config")"
  chart="$(jq -r '.chart' "$config")"
  chart_version="$(jq -r '.chartVersion' "$config")"
  chart_file="$scratch/${chart}-${chart_version}.tgz"
  [ -f "$chart_file" ] \
    || helm pull "oci://${chart_repo}/${chart}" --version "$chart_version" -d "$scratch" >/dev/null
  helm template magpie "$chart_file" --namespace default \
    --values "$rendered/kubernetes/apps/default/magpie/values.sops.yaml"
}

age-keygen --output "$scratch/age.key" >/dev/null 2>&1

# Default: /config stays ephemeral, so no claim is referenced or created.
default="$scratch/default"
render "tests/config/base.toml" "$default"
default_manifest="$(template "$default")" || die "the pinned chart rejected the default values"
if grep -qx 'kind: PersistentVolumeClaim' <<<"$default_manifest"; then
  die "the default input creates a PersistentVolumeClaim object"
fi
if [ "$(yq 'select(.kind == "Deployment") | .spec.template.spec.volumes[]
  | select(.name == "config") | has("emptyDir")' <<<"$default_manifest")" != true ]; then
  die "the default /config volume is not an emptyDir"
fi

# A named claim: mounted by reference, and still no PVC created.
claim="$scratch/claim"
render "tests/config/magpie-claim.toml" "$claim"
claim_manifest="$(template "$claim")" || die "the pinned chart rejected the claim values"
if grep -qx 'kind: PersistentVolumeClaim' <<<"$claim_manifest"; then
  die "the claim input creates an app-owned PersistentVolumeClaim"
fi
deployment="$(yq 'select(.kind == "Deployment") | .spec.template.spec' <<<"$claim_manifest")"
if [ "$(yq '.volumes[] | select(.name == "config")
  | .persistentVolumeClaim.claimName' <<<"$deployment")" != magpie-config ]; then
  die "the config volume does not reference the configured claim"
fi
# The claim is mounted at /config while /tmp keeps its own scratch volume.
if [ "$(yq '.containers[0].volumeMounts[] | select(.name == "config")
  | .mountPath' <<<"$deployment")" != /config ]; then
  die "the claim is not mounted at /config"
fi
if [ "$(yq '.volumes[] | select(.name == "tmp") | has("emptyDir")' <<<"$deployment")" != true ]; then
  die "the /tmp scratch volume is no longer an emptyDir"
fi
if [ "$(yq '.containers[0].volumeMounts[] | select(.name == "tmp")
  | .mountPath' <<<"$deployment")" != /tmp ]; then
  die "the scratch volume is no longer mounted at /tmp"
fi

# Invalid claim names are rejected by the existing validator, not silently
# turned into a broken volume reference.
long_name="$(printf 'a%.0s' {1..254})"
for invalid in "Upper-Case" "bad_name" "-leading-dash" "trailing-" "leading.dot-" "dot..dot" "$long_name"; do
  config="$scratch/invalid.toml"
  printf '[repository]\nurl = "git://git.example.test/platform.git"\n[magpie]\nconfig_claim = "%s"\n' \
    "$invalid" >"$config"
  if ANYLINGS_CONFIG="$config" just check config >/dev/null 2>&1; then
    die "the config validator accepted the invalid claim name '${invalid:0:40}'"
  fi
done

# The claim input renders through the same publication path as the default, so
# its output must also already match the repository's YAML formatting.
oxfmt --check "$claim/.sops.yaml" "$claim/kubernetes" >/dev/null \
  || die "the claim input renders output that the format check rejects"

log "default stays ephemeral; a named claim mounts without an app-owned PVC"
