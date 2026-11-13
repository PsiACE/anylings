#!/usr/bin/env bash
# Public fixture entrypoint, with the cluster/privilege boundary stubbed, plus
# the offline unit tests for the retained-PV inspection core and its
# read-only operator preflight. Nothing here reaches a cluster: the CLI tests
# replace the kubectl process seam, so no kubeconfig or credential is read.
set -euo pipefail
cd "$(dirname "$0")/../.."
just_bin="$(command -v just)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/repo/tests/ci" "$scratch/repo/infra/tofu/k0s" "$scratch/bin" "$scratch/runner"
cp tests/ci/run.sh "$scratch/repo/tests/ci/"
cp infra/tofu/k0s/*.tf infra/tofu/k0s/.terraform.lock.hcl infra/tofu/k0s/cluster.sample.yaml "$scratch/repo/infra/tofu/k0s/"
entry="$scratch/repo/tests/ci/run.sh"
cat >"$scratch/bin/just" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  'apply k0s')
    root="${ANYLINGS_OUTPUT_DIR%/rendered}"
    printf '%s\n' "$root" >"$SAFETY_LAST_RUN"
    mkdir "$root/storage"
    printf 'persisted\n' >"$root/storage/data"
    exit 42 ;;
  '--yes destroy k0s') exit "$RESET_STATUS" ;;
  *) exit 97 ;;
esac
STUB
printf '#!/usr/bin/env bash\nexit 1\n' >"$scratch/bin/kubectl"
cat >"$scratch/bin/sudo" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" != -n ] || shift
[ "$*" = "rm -rf -- $(cat "$SAFETY_LAST_RUN")/storage" ] || exit 98
exec "$@"
STUB
chmod +x "$scratch/bin/"*
export PATH="$scratch/bin:$PATH" RUNNER_TEMP="$scratch/runner" SAFETY_LAST_RUN="$scratch/last-run"
if GITHUB_ACTIONS=false ANYLINGS_CI_DISPOSABLE=1 bash "$entry" >"$scratch/run.log" 2>&1; then exit 1; fi
if GITHUB_ACTIONS=true ANYLINGS_CI_DISPOSABLE=0 bash "$entry" >"$scratch/run.log" 2>&1; then exit 1; fi
[ -z "$(find "$RUNNER_TEMP" -mindepth 1 -print -quit)" ]
export GITHUB_ACTIONS=true ANYLINGS_CI_DISPOSABLE=1
previous=''
for RESET_STATUS in 33 0; do
  export RESET_STATUS
  # Even successful cleanup must not hide failed cluster startup.
  if bash "$entry" >"$scratch/run.log" 2>&1; then cat "$scratch/run.log"; exit 1; fi
  run="$(cat "$SAFETY_LAST_RUN")"
  [ "$run" != "$previous" ]
  mode="$(stat -c '%a' "$run")"
  (( (8#$mode & 077) == 0 ))
  if [ "$RESET_STATUS" = 33 ]; then
    [ "$(cat "$run/storage/data")" = persisted ]
    previous="$run"
  else
    [ ! -e "$run/storage" ]
    [ "$(cat "$previous/storage/data")" = persisted ]
  fi
done
printf 'CI authorization, fresh private directories and failure-safe cleanup passed\n'

# The real public storage command must accept mktemp-style dotted directory
# names. Stub only Kubernetes access; no host storage or cluster is touched.
export KUBECONFIG="$scratch/kubeconfig" ANYLINGS_CONTEXT=synthetic
export ANYLINGS_STORAGE_PATH="$scratch/anylings-ci.fixture/storage"
export STORAGE_MANIFEST="$scratch/storage.yaml"
: > "$KUBECONFIG"
cat > "$scratch/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
if [[ " $* " == *" apply "* ]]; then cat > "$STORAGE_MANIFEST"; fi
STUB
"$just_bin" apply k0s-storage
[ -s "$STORAGE_MANIFEST" ]
if ANYLINGS_STORAGE_PATH=/var/lib/k0s/../k0s/volume \
  "$just_bin" apply k0s-storage >"$scratch/storage-rejected.log" 2>&1; then
  echo 'storage accepted a protected k0s path' >&2; exit 1
fi
printf 'Dotted storage paths accepted; protected-path aliases rejected\n'

# The retained-PV inspection core is pure and its preflight transport is mocked,
# so this stays offline: synthetic dicts only, no cluster, no kubeconfig.
uv run --quiet --locked --no-dev python -m unittest \
  tests.ci.test_retained_pv tests.ci.test_inspect_retained_pv
printf 'Retained-PV inspection core and read-only preflight tests passed\n'
