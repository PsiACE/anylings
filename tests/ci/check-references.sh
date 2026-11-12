#!/usr/bin/env bash
# Regression contract for the published-reference check behind
# `just check platform`. Uses the real recipe and a task-owned render only: no
# cluster, no credential and no network. Each case gets its own copy of the
# rendered tree so one mutation cannot hide another.
set -euo pipefail
cd "$(dirname "$0")/../.."
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
umask 077

# shellcheck source=tests/ci/fixture-input.sh
. tests/ci/fixture-input.sh

export ANYLINGS_CONFIG="$scratch/cluster.toml"
export ANYLINGS_OUTPUT_DIR="$scratch/rendered"
export SOPS_AGE_KEY_FILE="$scratch/age.key"
prepare_platform_input tests/config/base.toml "$scratch" >/dev/null

just init platform >"$scratch/init.log" 2>&1
just configure platform >"$scratch/configure.log" 2>&1

# The accepted input must keep passing: both rendered apps resolve.
just check platform >"$scratch/check.log" 2>&1 \
  || { cat "$scratch/check.log" >&2; echo "accepted input failed the reference check" >&2; exit 1; }

base_app="kubernetes/apps/default/magpie/config.json"

# Reject a reference the promoted tree cannot resolve. `expect` names the
# diagnostic so a different failure cannot pass for the intended one.
case_dir=0
reject() {
  local label="$1" key="$2" ref="$3" expect="$4" dir
  case_dir=$((case_dir + 1))
  dir="$scratch/case-$case_dir"
  cp -R "$scratch/rendered" "$dir"
  jq --arg k "$key" --arg r "$ref" '.[$k] = $r' \
    "$scratch/rendered/$base_app" > "$dir/$base_app"
  if ANYLINGS_OUTPUT_DIR="$dir" just check platform >"$scratch/$label.log" 2>&1; then
    echo "accepted $label: $key=$ref" >&2
    exit 1
  fi
  grep -qF "$expect" "$scratch/$label.log" \
    || { cat "$scratch/$label.log" >&2; echo "$label failed for the wrong reason" >&2; exit 1; }
}

# The supervisor's probe: task-owned targets placed beside the render, reached
# with a parent segment. They exist, so existence alone would have accepted them.
printf 'synthetic\n' > "$scratch/outside.yaml"
mkdir "$scratch/outside-dir"
reject parent_escape valuesFile ../outside.yaml "outside the published tree"
reject parent_escape_dir manifestPath ../outside-dir "outside the published tree"
reject escaped_segment valuesFile "kubernetes/../../outside.yaml" "parent segment"
reject absolute_path valuesFile "/etc/hostname" "absolute path"
reject outside_subtree valuesFile "apps/default/magpie/values.sops.yaml" "outside the published tree"
reject current_segment valuesFile "kubernetes/./$base_app" "current segment"
reject empty_segment valuesFile "kubernetes//$base_app" "empty segment"
reject missing_file valuesFile "kubernetes/apps/default/magpie/absent.yaml" "missing valuesFile"
reject missing_dir manifestPath "kubernetes/apps/default/absent" "missing manifestPath"

# A symlink inside the tree that resolves to an outside file passes an
# existence test but would publish a broken reference.
printf 'synthetic\n' > "$scratch/outside-values.yaml"
case_dir=$((case_dir + 1))
dir="$scratch/case-$case_dir"
cp -R "$scratch/rendered" "$dir"
rm -f "$dir/kubernetes/apps/default/magpie/values.sops.yaml"
ln -s "$scratch/outside-values.yaml" "$dir/kubernetes/apps/default/magpie/values.sops.yaml"
if ANYLINGS_OUTPUT_DIR="$dir" just check platform >"$scratch/symlink.log" 2>&1; then
  echo "accepted a valuesFile symlink that resolves outside the tree" >&2
  exit 1
fi
grep -qF "resolves outside the published tree" "$scratch/symlink.log" \
  || { cat "$scratch/symlink.log" >&2; echo "symlink case failed for the wrong reason" >&2; exit 1; }

# The same link inside the tree stays acceptable: the check is about escaping
# the published subtree, not about forbidding symlinks.
case_dir=$((case_dir + 1))
dir="$scratch/case-$case_dir"
cp -R "$scratch/rendered" "$dir"
mv "$dir/kubernetes/apps/default/magpie/values.sops.yaml" \
  "$dir/kubernetes/apps/default/magpie/values-real.sops.yaml"
ln -s values-real.sops.yaml "$dir/kubernetes/apps/default/magpie/values.sops.yaml"
ANYLINGS_OUTPUT_DIR="$dir" just check platform >/dev/null 2>&1 \
  || { echo "rejected a symlink that stays inside the published tree" >&2; exit 1; }

# A configured prefix must be present on every reference, so a value written
# without it cannot silently point at another location.
prefixed="$scratch/prefixed"
prepare_platform_input tests/config/repository-prefix.toml "$prefixed" >/dev/null
if ! ANYLINGS_CONFIG="$prefixed/cluster.toml" ANYLINGS_OUTPUT_DIR="$prefixed/rendered" \
  SOPS_AGE_KEY_FILE="$scratch/age.key" just configure platform >/dev/null 2>&1; then
  echo "prefixed input did not render" >&2
  exit 1
fi
if ! ANYLINGS_CONFIG="$prefixed/cluster.toml" ANYLINGS_OUTPUT_DIR="$prefixed/rendered" \
  SOPS_AGE_KEY_FILE="$scratch/age.key" just check platform >/dev/null 2>&1; then
  echo "accepted prefixed input failed the reference check" >&2
  exit 1
fi
tunnel_config="kubernetes/apps/network/cloudflare-tunnel/config.json"
jq '.manifestPath = "kubernetes/apps/network/cloudflare-tunnel"' \
  "$prefixed/rendered/$tunnel_config" > "$scratch/stripped.json"
cp -R "$prefixed/rendered" "$scratch/stripped-tree"
cp "$scratch/stripped.json" "$scratch/stripped-tree/$tunnel_config"
if ANYLINGS_CONFIG="$prefixed/cluster.toml" ANYLINGS_OUTPUT_DIR="$scratch/stripped-tree" \
  SOPS_AGE_KEY_FILE="$scratch/age.key" just check platform >"$scratch/prefix.log" 2>&1; then
  echo "accepted a reference without the configured repository.path prefix" >&2
  exit 1
fi
grep -qF "missing the repository.path prefix" "$scratch/prefix.log" \
  || { cat "$scratch/prefix.log" >&2; echo "prefix case failed for the wrong reason" >&2; exit 1; }

printf 'Published-reference contract: accepted base and prefixed inputs pass; escapes, absolute paths, wrong prefixes, missing targets and outside symlinks are rejected\n'
