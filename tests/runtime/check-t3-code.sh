#!/usr/bin/env bash
# Focused checks for the T3 Code runtime image and its GitOps template.
#
# Everything here is static: no registry push, no cluster, no cloud call. Two
# things are proven for real: the entrypoint's startup contract (by running it
# against a synthetic HOME) and that the rendered values are valid
# app-template input (by rendering them through the pinned chart).
#
# `--image <ref>` additionally asserts what only a built image can show: both
# executables report the pinned versions, the process runs as the image's
# nonroot account, and the state/workspace roots are writable. That is the
# check to run in CI after `podman build`; without the flag this stays static.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

IMAGE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --image)
            [ "$#" -ge 2 ] || {
                echo "usage: bash tests/runtime/check-t3-code.sh [--image <ref>]" >&2
                exit 2
            }
            IMAGE="$2"
            shift 2
            ;;
        *)
            echo "usage: bash tests/runtime/check-t3-code.sh [--image <ref>]" >&2
            exit 2
            ;;
    esac
done

log() { printf '[t3-code-check] %s\n' "$*" >&2; }
die() {
    log "error: $*"
    exit 1
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# ------------------------------------------------------- image definition --

log "check the image pins and the entrypoint contract"

containerfile="images/t3-code/Containerfile"
entrypoint="images/t3-code/entrypoint.sh"
requirements="images/t3-code/requirements.txt"
for file in "$containerfile" "$entrypoint" "$requirements"; do
    [ -f "$file" ] || die "missing $file"
done

# The three versions this image is defined by. Each must be pinned, not floating.
grep -qE '^ARG T3_VERSION=[0-9]+\.[0-9]+\.[0-9]+$' "$containerfile" \
    || die "T3_VERSION is not pinned to a release"
# A base image by tag alone can be republished; only a digest is a pin.
grep -qE '^ARG PYTHON_IMAGE=.*@sha256:[0-9a-f]{64}$' "$containerfile" \
    || die "the Python base image is not digest-pinned"
grep -qE '^ARG UV_IMAGE=.*@sha256:[0-9a-f]{64}$' "$containerfile" \
    || die "the uv stage image is not digest-pinned"
# Both release digests must be present, so one architecture cannot silently
# reuse the other's checksum.
grep -qE '^ARG T3_SHA256_AMD64=[0-9a-f]{64}$' "$containerfile" \
    || die "the amd64 release digest is missing"
grep -qE '^ARG T3_SHA256_ARM64=[0-9a-f]{64}$' "$containerfile" \
    || die "the arm64 release digest is missing"
# The download is verified before extraction.
grep -q 'sha256sum --check' "$containerfile" || die "the release archive is not verified"

# Bub is installed from its PyPI distribution with the extra that provides the
# command line; requirements.in states that intent and requirements.txt is the
# resolved lock whose hashes the install verifies.
grep -qE '^bub\[app\]==[0-9]+\.[0-9]+\.[0-9]+$' "$REPO_ROOT/images/t3-code/requirements.in" \
    || die "bub[app] is not pinned in requirements.in"
grep -qE '^bub==[0-9]+\.[0-9]+\.[0-9]+ ' "$requirements" \
    || die "the resolved bub wheel pin with hashes is missing"
grep -q -- '--require-hashes' "$containerfile" || die "the Python install does not verify hashes"

# The image must not carry an entrypoint that starts as root, and must not
# disable T3's authentication.
grep -qE '^USER 65532:65532$' "$containerfile" || die "the image does not set a nonroot USER"
if grep -qE 'T3CODE_(LAN|PROTECT)|--lan' "$containerfile"; then
    die "the image must not widen reachability or policy"
fi
bash -n "$entrypoint" || die "the entrypoint is not valid shell"

# --------------------------------------------------- entrypoint behaviour --

log "exercise the entrypoint against a synthetic HOME"

# Stub executables: the entrypoint's job is argument and directory handling,
# not running T3 itself, and no real user configuration is involved.
stub_bin="$scratch/bin"
mkdir -p "$stub_bin"
cat >"$stub_bin/t3" <<'STUB'
#!/usr/bin/env bash
# Record the exact invocation, then exit as a server that failed to start would.
if [ "${1:-}" = "--version" ]; then
    echo "t3 v0.0.0-stub"
    exit 0
fi
printf '%s\n' "$*" >"$T3_STUB_LOG"
exit 0
STUB
cat >"$stub_bin/bub" <<'STUB'
#!/usr/bin/env bash
echo "bub 0.0.0-stub"
STUB
chmod +x "$stub_bin/"*

# Run the entrypoint with an isolated HOME and its own state/workspace, so no
# real T3 data directory is ever touched.
run_entrypoint() {
    local state="$scratch/home/.t3" workspace="$scratch/workspace"
    mkdir -p "$state" "$workspace"
    env -i \
        PATH="$stub_bin:/usr/bin:/bin" \
        HOME="$scratch/home" \
        T3CODE_HOME="$state" \
        T3CODE_PROJECT_ROOT="$workspace" \
        T3_STUB_LOG="$scratch/invocation" \
        "${@}" \
        bash "$entrypoint"
}

run_entrypoint T3CODE_HOST=127.0.0.1 T3CODE_PORT=3999
grep -qx 'serve --mode web --host 127.0.0.1 --port 3999 --base-dir '"$scratch"'/home/.t3 --no-browser '"$scratch"'/workspace' \
    "$scratch/invocation" \
    || die "the entrypoint did not start the web server with the expected arguments: $(cat "$scratch/invocation")"

# Registration of the mounted workspace is opt-in, so a restart of a workspace
# that already has projects does not add one each time.
run_entrypoint T3CODE_HOST=127.0.0.1 T3CODE_PORT=3999 T3CODE_AUTO_BOOTSTRAP_PROJECT=true
grep -q -- '--auto-bootstrap-project-from-cwd' "$scratch/invocation" \
    || die "opt-in project registration was not passed through"

# An unwritable state root must fail at startup, not at the first request. A
# read-only directory is used rather than a missing one, because a missing
# directory under a writable parent is legitimately created for a fresh volume.
readonly_dir="$scratch/readonly"
mkdir -p "$readonly_dir"
chmod 0500 "$readonly_dir"
if run_entrypoint T3CODE_HOST=127.0.0.1 T3CODE_PORT=3999 T3CODE_HOME="$readonly_dir/.t3" 2>"$scratch/err"; then
    chmod 0700 "$readonly_dir"
    die "the entrypoint accepted a state directory it could not write"
fi
chmod 0700 "$readonly_dir"
grep -q 'not writable\|missing' "$scratch/err" || die "the failure did not name the directory problem"

# A fresh mount is empty: the state root must be created rather than required.
run_entrypoint T3CODE_HOST=127.0.0.1 T3CODE_PORT=3999 T3CODE_HOME="$scratch/fresh/state"
[ -d "$scratch/fresh/state" ] || die "the entrypoint did not create a missing state root"

# A bad boolean must be rejected rather than silently treated as false.
if run_entrypoint T3CODE_HOST=127.0.0.1 T3CODE_PORT=3999 T3CODE_AUTO_BOOTSTRAP_PROJECT=yes 2>"$scratch/err"; then
    die "the entrypoint accepted a non-boolean project-registration flag"
fi

if env -i PATH="$stub_bin:/usr/bin:/bin" HOME="$scratch/home" bash -c '
    set -euo pipefail
    # Simulate the root refusal without needing root: the branch checks id -u.
    id() { echo 0; }
    export -f id
    bash images/t3-code/entrypoint.sh' 2>"$scratch/err"; then
    die "the entrypoint started as root"
fi
grep -q 'refusing to run as root' "$scratch/err" || die "the root refusal was not reported"

# ------------------------------------------------------- GitOps template --

# The runtime is optional: a cluster without a published image keeps rendering
# its existing workloads, which is what makes this slice deployable without
# blocking on a registry. The omitted case is the shared base input, so it stays
# the same fixture the render job uses instead of a second copy of it.
(
    export ANYLINGS_CONFIG="$REPO_ROOT/tests/config/base.toml"
    export ANYLINGS_OUTPUT_DIR="$scratch/rendered-without"
    export SOPS_AGE_KEY_FILE="$scratch/age.key"
    just init platform >/dev/null
    just configure platform >/dev/null
)
[ ! -e "$scratch/rendered-without/kubernetes/apps/agents/t3-code/config.json" ] \
    || die "an unconfigured agent runtime was rendered anyway"
[ -f "$scratch/rendered-without/kubernetes/apps/default/magpie/config.json" ] \
    || die "omitting the agent runtime also dropped an existing workload"
# The AppProject is the other place the runtime appears. Leaving its namespace
# permitted when nothing deploys there would widen the project for no reason.
if yq -e '.spec.destinations[] | select(.namespace == "agents")' \
    "$scratch/rendered-without/kubernetes/projects/kubernetes.yaml" >/dev/null; then
    die "the AppProject still permits the agents namespace with no agent runtime"
fi
yq -e '.spec.destinations[] | select(.namespace == "default")' \
    "$scratch/rendered-without/kubernetes/projects/kubernetes.yaml" >/dev/null \
    || die "the AppProject lost an existing destination"

log "render the template and validate it as app-template input"

# The claim-enabled case is the repository's own agents fixture, not a private
# copy: a check against a different input could pass while the published
# fixture renders something else.
export ANYLINGS_CONFIG="$REPO_ROOT/tests/config/agents.toml"
export ANYLINGS_OUTPUT_DIR="$scratch/rendered-claim"
export SOPS_AGE_KEY_FILE="$scratch/age.key"
just init platform >/dev/null
just configure platform >/dev/null

values="$ANYLINGS_OUTPUT_DIR/kubernetes/apps/agents/t3-code/values.yaml"
config="$ANYLINGS_OUTPUT_DIR/kubernetes/apps/agents/t3-code/config.json"
[ -f "$values" ] || die "the runtime values were not rendered"
[ -f "$config" ] || die "the runtime app config was not rendered"

# How many runtime renders this check produced, each paired with the volume it
# must mount. A branch that is never rendered is never asserted, so both are
# listed explicitly below.
claim_rendered="$ANYLINGS_OUTPUT_DIR"
empty_rendered=""

# An explicit empty claim means "use an emptyDir". This is the case that used to
# fail while an omitted key passed, so it is a regression, not a style check.
cat >"$scratch/empty-claim.toml" <<'TOML'
[repository]
url = "git://git.example.test/platform.git"

[agents]
image_repository = "registry.example.test/anylings/t3-code"
storage_existing_claim = ""
TOML
(
    export ANYLINGS_CONFIG="$scratch/empty-claim.toml"
    export ANYLINGS_OUTPUT_DIR="$scratch/rendered-empty"
    export SOPS_AGE_KEY_FILE="$scratch/age.key"
    just init platform >/dev/null
    just configure platform >/dev/null
)
empty_rendered="$scratch/rendered-empty"

# Invalid names must be rejected by the config validator it already has, so a
# broken reference cannot reach the cluster. DNS limits are part of validity.
invalid_claims=(
    "Upper-Case" "bad_name" "-leading" "trailing-" "dot..dot" "a/b"
    "$(printf 'a%.0s' {1..254})" # total length beyond 253
    "$(printf 'a%.0s' {1..64})"  # single label beyond 63
)
for invalid in "${invalid_claims[@]}"; do
    printf '[repository]\nurl = "git://git.example.test/platform.git"\n[agents]\nimage_repository = "registry.example.test/anylings/t3-code"\nstorage_existing_claim = "%s"\n' \
        "$invalid" >"$scratch/invalid.toml"
    if ANYLINGS_CONFIG="$scratch/invalid.toml" just check config >/dev/null 2>&1; then
        die "the config validator accepted the invalid claim name '${invalid:0:40}'"
    fi
done

# The published config must name the runtime and its own namespace, and must
# not carry a manifest path: this workload ships values only.
jq -e '.appName == "t3-code" and .namespace == "agents"' "$config" >/dev/null \
    || die "the rendered app config does not describe the runtime"
jq -e '.manifestPath == "" and .valuesFile != ""' "$config" >/dev/null \
    || die "the rendered app config carries an unexpected manifest source"

# No credential may be expressible in these values, and no vendor or model may
# be preselected: provider sign-in is an instance decision.
if yq -o=json '.' "$values" | jq -e '
    [.. | objects | keys[] | ascii_downcase]
    | map(select(test("password|token|secret|api[_-]?key|credential")))
    | length > 0' >/dev/null; then
    die "the runtime values contain a credential-shaped key"
fi
yq -e '.service.app.type == "ClusterIP"' "$values" >/dev/null \
    || die "the runtime Service is not cluster-internal"
yq -e '.defaultPodOptions.securityContext.runAsNonRoot == true' "$values" >/dev/null \
    || die "the runtime pod does not require a nonroot account"
yq -e '.controllers."t3-code".containers.app.securityContext.readOnlyRootFilesystem == true' "$values" >/dev/null \
    || die "the runtime container does not use a read-only root filesystem"
yq -e '.persistence.home.type == "persistentVolumeClaim"' "$values" >/dev/null \
    || die "the configured existing claim was not applied"
yq -e '.spec.destinations[] | select(.namespace == "agents")' \
    "$ANYLINGS_OUTPUT_DIR/kubernetes/projects/kubernetes.yaml" >/dev/null \
    || die "the AppProject does not permit the configured agents namespace"

# Render every rendered branch through the pinned chart: a values file the chart
# rejects, or probes it cannot build, fails here rather than at the first sync.
#
# This is a required check. An earlier version logged "SKIPPED" and still exited
# 0 when the chart could not be pulled, so a green result could claim chart
# validity without ever rendering the chart. It now fails closed.
CHART_REPO="$(jq -r '.chartRepo' "$config")"
CHART="$(jq -r '.chart' "$config")"
CHART_VERSION="$(jq -r '.chartVersion' "$config")"
mkdir -p "$scratch/chart"

pull_chart() {
    local version="$1"
    [ -s "$scratch/chart/${CHART}-${version}.tgz" ] && return 0
    helm pull "oci://${CHART_REPO}/${CHART}" --version "$version" -d "$scratch/chart" >/dev/null 2>&1
}

if ! pull_chart "$CHART_VERSION"; then
    die "could not pull ${CHART}:${CHART_VERSION}; this check requires the pinned chart and does not pass without it"
fi
chart_file="$scratch/chart/${CHART}-${CHART_VERSION}.tgz"
[ -s "$chart_file" ] || die "the pulled chart archive is empty: $chart_file"

# render_values <rendered dir> <output file>: what the chart produces for that
# rendered tree.
render_values() {
    local rendered="$1" out="$2"
    helm template t3-code "$chart_file" --namespace agents \
        --values "$rendered/kubernetes/apps/agents/t3-code/values.yaml" >"$out"
}

# The same assertions run for both branches, so a claim reference and an
# emptyDir cannot drift apart in what they are allowed to lose.
check_workload() {
    local manifests="$1" expected_volume="$2" expected_claim="${3-}"
    python3 - "$manifests" "$expected_volume" "$expected_claim" <<'PY'
import sys

import yaml

path, expected_volume, expected_claim = sys.argv[1], sys.argv[2], sys.argv[3]
docs = [d for d in yaml.safe_load_all(open(path)) if d]
by_kind = {d["kind"]: d for d in docs}
for kind in ("Deployment", "Service"):
    if kind not in by_kind:
        raise SystemExit(f"the chart rendered no {kind}")

deployment = by_kind["Deployment"]["spec"]
pod = deployment["template"]["spec"]
container = pod["containers"][0]
security = container["securityContext"]
checks = {
    "one replica": deployment.get("replicas") == 1,
    "recreate strategy": deployment.get("strategy", {}).get("type") == "Recreate",
    "http port": any(p.get("containerPort") == 3773 for p in container.get("ports", [])),
    "readiness probe": "readinessProbe" in container,
    "liveness probe": "livenessProbe" in container,
    "no privilege escalation": security.get("allowPrivilegeEscalation") is False,
    "read-only root filesystem": security.get("readOnlyRootFilesystem") is True,
    "all capabilities dropped": security["capabilities"]["drop"] == ["ALL"],
    "nonroot pod": pod["securityContext"].get("runAsUser") == 65532,
    "nonroot enforced": pod["securityContext"].get("runAsNonRoot") is True,
    "fsGroup for the mounted claim": pod["securityContext"].get("fsGroup") == 65532,
    "resource limits": "limits" in container.get("resources", {}),
    "writable /home/t3 mount": any(
        m.get("mountPath") == "/home/t3" for m in container.get("volumeMounts", [])
    ),
}
volumes = {v["name"]: v for v in pod.get("volumes", [])}
home = volumes.get("home", {})
if expected_volume == "emptyDir":
    checks["ephemeral home volume"] = "emptyDir" in home
elif expected_volume == "claim":
    checks["home references the claim"] = (
        home.get("persistentVolumeClaim", {}).get("claimName") == expected_claim
    )
else:
    raise SystemExit(f"unknown expected volume kind: {expected_volume}")
checks["no app-owned claim"] = not any(d["kind"] == "PersistentVolumeClaim" for d in docs)

service = by_kind["Service"]["spec"]
checks.update({
    "cluster-internal service": service.get("type") == "ClusterIP",
    "service publishes the container port": any(
        p.get("port") == 3773 for p in service.get("ports", [])
    ),
})
failed = [name for name, ok in checks.items() if not ok]
if failed:
    raise SystemExit("rendered workload lost: " + ", ".join(failed))
PY
}

# Claim branch: the fixture's own claim name must be the one mounted.
render_values "$claim_rendered" "$scratch/manifests-claim.yaml" \
    || die "the pinned chart rejected the claim values"
claim_name="$(yq -r '.persistence.home.existingClaim // ""' \
    "$claim_rendered/kubernetes/apps/agents/t3-code/values.yaml")"
[ -n "$claim_name" ] || die "the claim fixture rendered no existingClaim"
check_workload "$scratch/manifests-claim.yaml" claim "$claim_name" \
    || die "the claim branch lost a required property"

# Ephemeral branch: an explicit empty claim must render an emptyDir, and the pod
# must still be nonroot with a read-only root filesystem.
[ -n "$empty_rendered" ] || die "the explicit-empty claim branch was not rendered"
render_values "$empty_rendered" "$scratch/manifests-empty.yaml" \
    || die "the pinned chart rejected the empty-claim values"
check_workload "$scratch/manifests-empty.yaml" emptyDir \
    || die "the emptyDir branch lost a required property"

log "app-template rendered both branches with the expected properties"

# ------------------------------------------------------------ built image --

if [ -n "$IMAGE" ]; then
    log "check the built image $IMAGE"
    container_tool=""
    for tool in podman docker; do
        if command -v "$tool" >/dev/null 2>&1; then
            container_tool="$tool"
            break
        fi
    done
    [ -n "$container_tool" ] || die "--image needs podman or docker"

    # Both executables, as the image's own user, with no network. Bub must be
    # the pinned release and must actually be the ACP-capable CLI: a console
    # script that only prints a version would not start an ACP session, so
    # `bub acp --help` is part of the contract.
    pinned_bub="$(grep -oE '^bub\[app\]==[0-9]+\.[0-9]+\.[0-9]+$' \
        "$REPO_ROOT/images/t3-code/requirements.in" | cut -d= -f3)"
    [ -n "$pinned_bub" ] || die "requirements.in does not pin bub[app]"
    toolchain="$("$container_tool" run --rm --network=none --entrypoint /bin/sh "$IMAGE" -c '
        set -e
        id -u
        t3 --version
        bub --version
        /opt/bub/bin/python --version
        bub acp --help
    ')" || die "the image does not provide the expected nonroot toolchain"
    grep -qx 65532 <<<"$toolchain" || die "the image does not run as uid 65532"
    image_bub="$(awk '/^bub /{print $2; exit}' <<<"$toolchain")"
    [ "$image_bub" = "$pinned_bub" ] \
        || die "image carries Bub $image_bub but the template pins $pinned_bub"
    # `bub acp` is what T3 launches; without the subcommand this is not a
    # runtime image regardless of the version string.
    grep -q 'Run Bub as an ACP agent' <<<"$toolchain" \
        || die "the image's bub has no ACP subcommand"
    grep -qE '^Python 3\.' <<<"$toolchain" || die "the image has no usable Python"

    # The version the image actually carries must be the one the template
    # deploys, so the rendered tag cannot drift from the built binary.
    image_t3="$("$container_tool" run --rm --network=none --entrypoint /bin/sh "$IMAGE" \
        -c 't3 --version' | awk '{print $2}' | tr -d 'v')"
    deployed_t3="$(yq -r '.controllers."t3-code".containers.app.image.tag' "$values")"
    [ "$image_t3" = "$deployed_t3" ] \
        || die "image carries T3 $image_t3 but the template deploys $deployed_t3"

# The startup contract, exercised the way the deployment runs it: read-only root
# filesystem, all capabilities dropped, no-new-privileges, and only /home/t3 and
# /tmp writable. --network=none means nothing leaves the machine.
#
# The previous version started the entrypoint, discarded its exit status with
# `|| true`, and grepped a log line. That passed even when the process died
# before serving anything, so the assertions here are the real ones: the process
# is alive, it still is after the probe, and the server answers the readiness
# endpoint the deployment's probe uses with a 200 whose body is the unauthenticated
# auth state. A log line proves nothing about that.
"$container_tool" run --rm --network=none \
    --read-only --cap-drop=ALL --security-opt=no-new-privileges \
    --tmpfs /home/t3:rw,mode=1777 --tmpfs /tmp:rw,mode=1777 \
    --entrypoint /bin/bash "$IMAGE" -ec '
    set -euo pipefail
    # Fresh, empty state: the entrypoint must create state and workspace under
    # the mounted root rather than requiring them to be pre-populated.
    rm -rf /home/t3/.t3 /home/t3/workspace
    /usr/local/bin/t3-entrypoint >/tmp/out 2>/tmp/err &
    pid=$!
    cleanup() { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }
    trap cleanup EXIT

    # Bounded readiness: retry until the server answers, and fail if it never
    # does. --retry-connrefused distinguishes "not listening yet" from a refusal
    # that will not resolve.
    if ! curl --fail --silent --show-error \
        --max-time 5 --retry 30 --retry-delay 1 --retry-connrefused \
        http://127.0.0.1:3773/api/auth/session >/tmp/session.json; then
        echo "the server never answered its readiness endpoint" >&2
        cat /tmp/out /tmp/err >&2 || true
        exit 1
    fi

    # Alive *after* serving, so a server that answered and then crashed fails.
    kill -0 "$pid"

    # The body is the real auth state: unauthenticated, with the auth methods a
    # client must use. A page-served 200 would not have these keys, which is why
    # the HTML fallback the SPA returns for unknown paths cannot satisfy this.
    python3 - <<PY
import json
state = json.load(open("/tmp/session.json"))
assert state.get("authenticated") is False, state
assert "auth" in state, state
PY

    # The writable roots exist and belong to the run user.
    [ -d /home/t3/.t3 ] && [ -d /home/t3/workspace ]
    [ "$(id -u)" -eq 65532 ]
    echo "readiness ok"
' || die "the image did not serve its readiness endpoint under the deployment shape"

    log "the built image provides both toolchains and starts as nonroot"
fi

# The chart check must fail closed: an unreachable chart cannot produce a green
# result. A stub `helm` that always fails forces exactly that path, so the
# regression needs no network and no registry.
if [ -z "${T3_CHECK_NESTED:-}" ]; then
log "assert the chart check fails when the chart cannot be pulled"
stub_dir="$scratch/stub-bin"
mkdir -p "$stub_dir"
cat >"$stub_dir/helm" <<'STUB'
#!/usr/bin/env bash
echo "stub helm: pull unavailable" >&2
exit 1
STUB
chmod +x "$stub_dir/helm"
if PATH="$stub_dir:$PATH" T3_CHECK_NESTED=1 bash "$REPO_ROOT/tests/runtime/check-t3-code.sh" \
    >/dev/null 2>&1; then
    die "the check passed while the pinned chart could not be pulled"
fi
fi

log "all checks passed"
