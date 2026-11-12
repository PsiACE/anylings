#!/usr/bin/env bash
# Focused checks for the Bub runtime image and its GitOps template.
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
                echo "usage: bash tests/runtime/check-bub.sh [--image <ref>]" >&2
                exit 2
            }
            IMAGE="$2"
            shift 2
            ;;
        *)
            echo "usage: bash tests/runtime/check-bub.sh [--image <ref>]" >&2
            exit 2
            ;;
    esac
done

log() { printf '[bub-check] %s\n' "$*" >&2; }
die() {
    log "error: $*"
    exit 1
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# ------------------------------------------------------- image definition --

log "check the image pins and the entrypoint contract"

containerfile="images/bub/Containerfile"
entrypoint="images/bub/entrypoint.sh"
requirements="images/bub/requirements.txt"
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
grep -qE '^bub\[app\]==[0-9]+\.[0-9]+\.[0-9]+$' "$REPO_ROOT/images/bub/requirements.in" \
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
    bash images/bub/entrypoint.sh' 2>"$scratch/err"; then
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
[ ! -e "$scratch/rendered-without/kubernetes/apps/agents/bub/config.json" ] \
    || die "an unconfigured agent runtime was rendered anyway"
[ -f "$scratch/rendered-without/kubernetes/apps/gateways/magpie/config.json" ] \
    || die "omitting the agent runtime also dropped an existing workload"
# The AppProject is the other place the runtime appears. Leaving its namespace
# permitted when nothing deploys there would widen the project for no reason.
if yq -e '.spec.destinations[] | select(.namespace == "agents")' \
    "$scratch/rendered-without/kubernetes/projects/kubernetes.yaml" >/dev/null; then
    die "the AppProject still permits the agents namespace with no agent runtime"
fi
yq -e '.spec.destinations[] | select(.namespace == "gateways")' \
    "$scratch/rendered-without/kubernetes/projects/kubernetes.yaml" >/dev/null \
    || die "the AppProject lost the gateway destination"

log "render the template and validate it as app-template input"

# The claim-enabled case is the repository's own agents fixture, not a private
# copy: a check against a different input could pass while the published
# fixture renders something else.
export ANYLINGS_CONFIG="$REPO_ROOT/tests/config/agents.toml"
export ANYLINGS_OUTPUT_DIR="$scratch/rendered-claim"
export SOPS_AGE_KEY_FILE="$scratch/age.key"
just init platform >/dev/null
just configure platform >/dev/null

values="$ANYLINGS_OUTPUT_DIR/kubernetes/apps/agents/bub/values.yaml"
config="$ANYLINGS_OUTPUT_DIR/kubernetes/apps/agents/bub/config.json"
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
image_repository = "registry.example.test/anylings/bub"
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
    printf '[repository]\nurl = "git://git.example.test/platform.git"\n[agents]\nimage_repository = "registry.example.test/anylings/bub"\nstorage_existing_claim = "%s"\n' \
        "$invalid" >"$scratch/invalid.toml"
    if ANYLINGS_CONFIG="$scratch/invalid.toml" just check config >/dev/null 2>&1; then
        die "the config validator accepted the invalid claim name '${invalid:0:40}'"
    fi
done

# The published config must name the runtime and its own namespace, and must
# not carry a manifest path: this workload ships values only.
jq -e '.appName == "bub" and .namespace == "agents"' "$config" >/dev/null \
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
yq -e '.controllers."bub".containers.app.securityContext.readOnlyRootFilesystem == true' "$values" >/dev/null \
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
    helm template bub "$chart_file" --namespace agents \
        --values "$rendered/kubernetes/apps/agents/bub/values.yaml" >"$out"
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
    "startup working directory outside the home volume": container.get("workingDir") == "/tmp",
    "resource limits": "limits" in container.get("resources", {}),
    # The volume is mounted at /home/t3 itself, which is the layout the image
    # and any existing claim already use: state at /home/t3/.t3, checkouts at
    # /home/t3/workspace, and a writable $HOME so the agent can create its own
    # ~/.bub. Leaf subPath mounts would move that layout and strand the state
    # already stored on an existing claim, so the exact mapping is asserted
    # rather than "some mount under /home/t3 exists".
    "volume mounted at /home/t3": any(
        m.get("mountPath") == "/home/t3" for m in container.get("volumeMounts", [])
    ),
    "no leaf remount under /home/t3": not any(
        m.get("mountPath", "").startswith("/home/t3/")
        for m in container.get("volumeMounts", [])
    ),
    "/home/t3 mount has no subPath": all(
        not m.get("subPath")
        for m in container.get("volumeMounts", [])
        if m.get("mountPath") == "/home/t3"
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
    "$claim_rendered/kubernetes/apps/agents/bub/values.yaml")"
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
    home_tmpfs="rw,mode=2770"
    # Podman copies the image tree into tmpfs by default. A fresh claim does
    # not, and copying its pre-chowned workspace would hide the startup bug.
    if [ "$container_tool" = podman ]; then
        home_tmpfs+=",notmpcopyup"
    fi

    # Both executables, as the image's own user, with no network. Bub must be
    # the pinned release and must actually be the ACP-capable CLI: a console
    # script that only prints a version would not start an ACP session, so
    # `bub acp --help` is part of the contract.
    pinned_bub="$(grep -oE '^bub\[app\]==[0-9]+\.[0-9]+\.[0-9]+$' \
        "$REPO_ROOT/images/bub/requirements.in" | cut -d= -f3)"
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

    # The image carries two components, so the deployed tag must name both of
    # the versions the image actually reports. Comparing against one of them
    # alone is what let a T3 release tag stand in for the runtime's identity.
    image_t3="$("$container_tool" run --rm --network=none --entrypoint /bin/sh "$IMAGE" \
        -c 't3 --version' | awk '{print $2}' | tr -d 'v')"
    deployed_tag="$(yq -r '.controllers."bub".containers.app.image.tag' "$values")"
    [ "$deployed_tag" = "bub${image_bub}-t3web${image_t3}" ] \
        || die "the template deploys $deployed_tag but the image is Bub $image_bub with T3 $image_t3"

    # Reproduce the bad startup order without deleting the directory the
    # runtime creates as root. A green runtime check must reject this shape.
    "$container_tool" run --rm --network=none \
        --read-only --cap-drop=ALL --security-opt=no-new-privileges \
        --user 65532:65532 --group-add 0 \
        --tmpfs "/home/t3:$home_tmpfs" --tmpfs /tmp:rw,mode=1777 \
        --workdir /home/t3/workspace --entrypoint /bin/bash "$IMAGE" -ec '
        [ -d /home/t3/workspace ] && [ ! -w /home/t3/workspace ]
        if /usr/local/bin/bub-entrypoint >/tmp/out 2>/tmp/err; then
            echo "the root-owned workspace unexpectedly started" >&2
            exit 1
        fi
        grep -q "required directory is not writable: /home/t3/workspace" /tmp/err
    ' || die "the image check did not reproduce the root-owned WORKDIR failure"

    startup_workdir="$(yq -r 'select(.kind == "Deployment") | .spec.template.spec.containers[0].workingDir' \
        "$scratch/manifests-claim.yaml")"

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
# The volume is mounted at /home/t3 the way the rendered values do it: a
# root-owned, group-writable volume root that the pod's fsGroup can write, not a
# world-writable tmpfs. --group-add stands in for fsGroup, which is what grants
# the nonroot process access to that root. A 1777 tmpfs here would hide the real
# contract: the agent must be able to create its own $HOME state under the
# mounted root, and a root-only mount would fail that.
"$container_tool" run --rm -i --network=none \
    --read-only --cap-drop=ALL --security-opt=no-new-privileges \
    --user 65532:65532 --group-add 0 \
    --tmpfs "/home/t3:$home_tmpfs" --tmpfs /tmp:rw,mode=1777 \
    --workdir "$startup_workdir" --entrypoint /bin/bash "$IMAGE" -ec '
    set -euo pipefail
    # Use the rendered workingDir, not the image default. Do not delete any
    # directory: doing so would hide the runtime-created root-owned WORKDIR.
    [ ! -e /home/t3/.t3 ] && [ ! -e /home/t3/workspace ]
    /usr/local/bin/bub-entrypoint >/tmp/out 2>/tmp/err &
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

    # $HOME itself must be writable, not only the two subdirectories. The agent
    # T3 launches keeps its own state under ~/.bub, so a read-only $HOME breaks
    # the ACP session even while the server is up and answering.
    mkdir -p "$HOME/.bub/tapes" || { echo "$HOME is not writable" >&2; exit 1; }
    # The entrypoint has created the workspace as the nonroot run user; use
    # that same directory for the provider T3 launches.
    cd /home/t3/workspace

    # Offline ACP handshake: the real initialize request, no model call and no
    # network. A failure here means the runtime cannot start its agent at all.
    # A FIFO, not a redirection from a regular file: the ACP server requires a
    # real pipe or socket on stdin and exits if it is handed a plain file.
    printf "%s\n" "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":1,\"clientCapabilities\":{}}}" >/tmp/acp.in
    mkfifo /tmp/acp.fifo
    ( cat /tmp/acp.in >/tmp/acp.fifo ) &
    # stdout stays a real pipe through `cat`: the ACP server inspects its own
    # stdio transport, and a regular file on either end makes it exit.
    if ! timeout 60 bub acp </tmp/acp.fifo 2>/tmp/acp.err | cat >/tmp/acp.json; then
        echo "offline bub acp initialize failed" >&2
        cat /tmp/acp.err >&2
        exit 1
    fi
    [ -s /tmp/acp.json ] || { echo "offline bub acp returned nothing" >&2; cat /tmp/acp.err >&2; exit 1; }
    python3 - <<PY
import json
reply = json.loads(open("/tmp/acp.json").readline())
assert reply["id"] == 1, reply
assert "protocolVersion" in reply["result"], reply
PY
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
if PATH="$stub_dir:$PATH" T3_CHECK_NESTED=1 bash "$REPO_ROOT/tests/runtime/check-bub.sh" \
    >/dev/null 2>&1; then
    die "the check passed while the pinned chart could not be pulled"
fi
fi

# ---------------------------------------------------------- image publishing --

# Publishing is a separate concern from building, and the difference is who can
# trigger it. These assertions are about the workflow as data: no pull-request
# event, no fork, and no long-lived credential can reach a push.
log "check the publisher is limited to trusted main pushes"
workflow="$REPO_ROOT/.github/workflows/runtime-image.yaml"
[ -f "$workflow" ] || die "the runtime image workflow is missing"

# Only a push to main starts it. A `pull_request` or `pull_request_target`
# trigger would let a fork's code reach a package write.
triggers="$(yq -r '.on | keys | join(" ")' "$workflow")"
[ "$triggers" = push ] || die "the publisher has unexpected triggers: $triggers"
[ "$(yq -r '.on.push.branches | join(" ")' "$workflow")" = main ] \
    || die "the publisher does not run on main pushes only"

# The job condition repeats the trust boundary, so a trigger edit alone does not
# widen it.
job_if="$(yq -r '.jobs.publish.if' "$workflow")"
expected_if="github.repository=='PsiACE/anylings'&&github.event_name=='push'&&github.ref=='refs/heads/main'"
[ "$(printf '%s' "$job_if" | tr -d '[:space:]')" = "$expected_if" ] \
    || die "the publisher condition does not restrict all three trust boundaries"

# package write is the one extra permission, and it is never granted workflow-wide.
yq -o=json '.permissions' "$workflow" | jq -e 'type == "object" and length == 0' >/dev/null \
    || die "the publisher must explicitly deny workflow-level permissions"
[ "$(yq -r '.jobs.publish.permissions | keys | join(" ")' "$workflow")" = "contents packages" ] \
    || die "the publisher holds unexpected job permissions"
[ "$(yq -r '.jobs.publish.permissions.packages' "$workflow")" = write ] \
    || die "the publisher cannot write a package"
[ "$(yq -r '.jobs.publish.permissions.contents' "$workflow")" = read ] \
    || die "the publisher does not keep contents read-only"

# One job: no publishable step hides in a second, differently-conditioned job.
[ "$(yq -r '.jobs | keys | length' "$workflow")" = 1 ] \
    || die "the publisher holds more than one job"

# The credential is the run's own token. A PAT, deploy key or sealed cloud
# secret in this workflow would be a long-lived credential in CI.
if grep -qE 'secrets\.[A-Za-z_]+' "$workflow"; then
    unexpected="$(grep -oE 'secrets\.[A-Za-z_]+' "$workflow" | sort -u | grep -vxF 'secrets.GITHUB_TOKEN' || true)"
    [ -z "$unexpected" ] || die "the publisher references a non-ephemeral secret: $unexpected"
fi
# Order is a contract, not a comment: check this build before either push.
yq -o=json '.jobs.publish.steps' "$workflow" | jq -e '
    map(.id) as $ids |
    ($ids | index("build")) as $build | ($ids | index("check")) as $check |
    ($ids | index("push")) as $push |
    $build != null and $check != null and $push != null and
    $build < $check and $check < $push and
    .[$check].run == "bash tests/runtime/check-bub.sh --image \"${REPOSITORY}:${SOURCE_TAG}\""
' >/dev/null || die "the publisher must check the built image before pushing"

# Exercise the actual publish script without a network or registry. A synthetic
# podman checks stdin/authfile use, records the digest and observes cleanup on
# success, push failure and an invalid registry digest. Comments cannot satisfy
# these assertions, and no real credential or Podman auth store is involved.
publish_script="$(yq -r '.jobs.publish.steps[] | select(.id == "push") | .run' "$workflow")"
publish_bin="$scratch/publisher-bin"
mkdir "$publish_bin"
cat >"$publish_bin/podman" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
command="$1"; shift
authfile=""; digestfile=""; stdin_password=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --authfile) authfile="$2"; shift 2 ;;
        --digestfile) digestfile="$2"; shift 2 ;;
        --username) shift 2 ;;
        --password-stdin) stdin_password=1; shift ;;
        *) shift ;;
    esac
done
printf '%s\n' "$command" >>"$STUB_LOG"
case "$authfile" in
    "$RUNNER_TEMP"/*) ;;
    *) exit 97 ;;
esac
case "$command" in
    login)
        [ "$stdin_password" = 1 ] && [ "$(cat)" = "$GHCR_TOKEN" ] || exit 96
        printf '%s' "$authfile" >"$STUB_AUTH_PATH"
        printf '{"auths":{}}\n' >"$authfile"
        ;;
    push)
        [ -f "$authfile" ]
        [ "$STUB_PUSH_FAIL" = 0 ] || exit 125
        if [ -n "$digestfile" ]; then printf '%s\n' "$STUB_DIGEST" >"$digestfile"; fi
        ;;
    logout) [ -f "$authfile" ] ;;
    *) exit 98 ;;
esac
STUB
chmod +x "$publish_bin/podman"
for scenario in success push-failure invalid-digest; do
    probe="$scratch/publisher-$scenario"
    mkdir -p "$probe/home" "$probe/temp"
    fail_push=0; fixture_digest="sha256:$(printf 'a%.0s' {1..64})"
    [ "$scenario" != push-failure ] || fail_push=1
    [ "$scenario" != invalid-digest ] || fixture_digest=sha256:not-a-digest
    fixture_token=synthetic-publisher-stdin-token
    if (
        cd "$probe"
        env -i PATH="$publish_bin:$PATH" HOME="$probe/home" RUNNER_TEMP="$probe/temp" \
            REPOSITORY=ghcr.io/example/test/bub SOURCE_TAG=sha-synthetic \
            VERSION_TAG=bub0.6.1-t3web0.1.8 \
            GHCR_TOKEN="$fixture_token" ACTOR=synthetic GITHUB_OUTPUT="$probe/output" \
            STUB_LOG="$probe/calls" STUB_AUTH_PATH="$probe/auth-path" \
            STUB_PUSH_FAIL="$fail_push" STUB_DIGEST="$fixture_digest" \
            bash -euo pipefail -c "$publish_script" >"$probe/log" 2>&1
    ); then result=0; else result=$?; fi
    if [ "$scenario" = success ]; then
        [ "$result" = 0 ] || die "the synthetic publisher failed"
        grep -qxF "digest=$fixture_digest" "$probe/output" || die "the registry digest was not recorded"
    elif [ "$scenario" = push-failure ]; then
        [ "$result" = 125 ] || die "the publisher did not preserve the push failure"
    else
        [ "$result" != 0 ] || die "the publisher accepted an invalid registry digest"
    fi
    grep -qxF logout "$probe/calls" || die "the publisher did not log out after $scenario"
    [ -s "$probe/auth-path" ] || die "the publisher did not create a private authfile"
    [ ! -e "$(cat "$probe/auth-path")" ] || die "the publisher kept its authfile after $scenario"
    if grep -rqF -- "$fixture_token" "$probe"; then
        die "the publisher persisted the synthetic plaintext token after $scenario"
    fi
done

# The image path is derived, not configured: no registry or owner is hardcoded
# in the workflow, so a fork or rename cannot publish to somebody's namespace.
grep -q 'ghcr.io/${REPOSITORY,,}/bub' "$workflow" \
    || die "the published image path is not derived from the repository"
if grep -qE 'ghcr\.io/[A-Za-z0-9._-]+/' "$workflow"; then
    die "the publisher hardcodes an image namespace"
fi
# The moving tag names both components. A tag equal to one component's release
# is the mislabelling this asserts against: it would describe the image by its
# web shell, or by the CLI, while claiming to be the whole runtime.
version_tag_script="$(yq -r '.jobs.publish.steps[] | select(.id == "image") | .run' "$workflow")"
grep -q 'version_tag="bub${bub_version}-t3web${t3_version}"' <<<"$version_tag_script" \
    || die "the publisher does not tag the image with both component versions"
grep -q 'images/bub/requirements.in' <<<"$version_tag_script" \
    || die "the publisher does not read the Bub version from images/bub"
grep -q 'images/bub/Containerfile' <<<"$version_tag_script" \
    || die "the publisher does not read the T3 Code version from images/bub"
if grep -qE 'version_tag="\$\{t3_version\}"|version_tag="\$\{bub_version\}"' <<<"$version_tag_script"; then
    die "the publisher tags the combined image with a single component version"
fi
# Derive the two versions the same way the workflow does and render one tag, so
# the fixture asserts the arithmetic rather than only the workflow text.
derived_bub="$(sed -nE 's/^bub\[app\]==([0-9]+\.[0-9]+\.[0-9]+)$/\1/p' "$REPO_ROOT/images/bub/requirements.in")"
derived_t3="$(sed -nE 's/^ARG T3_VERSION=([0-9]+\.[0-9]+\.[0-9]+)$/\1/p' "$REPO_ROOT/images/bub/Containerfile")"
[ -n "$derived_bub" ] && [ -n "$derived_t3" ] \
    || die "the image pins do not yield a Bub and a T3 Code version"
# The fixture renders the default tag from the template; it must be the tag the
# publisher would produce for these pins, or a deployment would name a tag that
# is never published.
[ "$(yq -r '.controllers."bub".containers.app.image.tag' \
    "$scratch/rendered-claim/kubernetes/apps/agents/bub/values.yaml")" \
    = "bub${derived_bub}-t3web${derived_t3}" ] \
    || die "the rendered default tag does not match the pins in images/bub"

# The gate never publishes: its jobs stay offline and keep contents: read.
if grep -q 'packages: write' "$REPO_ROOT/.github/workflows/ci.yaml"; then
    die "the pull-request gate can write packages"
fi
if yq -e '.jobs[] | select(.uses != null)' "$REPO_ROOT/.github/workflows/ci.yaml" >/dev/null 2>&1; then
    die "the gate delegates to another workflow"
fi

# ------------------------------------------------------------ digest pinning --

# A deployment may pin an immutable digest instead of trusting a moving tag.
log "check the digest pin renders and the tag-only path does not"
digest="sha256:$(printf 'a%.0s' {1..64})"
cat >"$scratch/pinned.toml" <<TOML
[repository]
url = "git://git.example.test/platform.git"

[agents]
image_repository = "ghcr.io/psiace/anylings/bub"
image_tag = "sha-0123456789abcdef"
image_digest = "$digest"
TOML
(
    export ANYLINGS_CONFIG="$scratch/pinned.toml"
    export ANYLINGS_OUTPUT_DIR="$scratch/rendered-pinned"
    export SOPS_AGE_KEY_FILE="$scratch/age.key"
    just init platform >/dev/null
    just configure platform >/dev/null
)
pinned_values="$scratch/rendered-pinned/kubernetes/apps/agents/bub/values.yaml"
[ "$(yq -r '.controllers."bub".containers.app.image.digest' "$pinned_values")" = "$digest" ] \
    || die "a configured digest was not rendered"
# The chart must combine tag and digest, or the pin would silently do nothing.
helm template bub "$chart_file" --namespace agents --values "$pinned_values" \
    >"$scratch/pinned-manifests.yaml"
pinned_image="$(yq -r 'select(.kind == "Deployment")
  | .spec.template.spec.containers[0].image' "$scratch/pinned-manifests.yaml")"
case "$pinned_image" in
    *:sha-0123456789abcdef@"$digest") ;;
    *) die "the pinned image is not tag@digest: $pinned_image" ;;
esac

# The tag-only path must not render a stale or empty digest key at all.
yq -e '.controllers."bub".containers.app.image | has("digest") | not' "$values" >/dev/null \
    || die "the tag-only values carry a digest key"
yq -r 'select(.kind == "Deployment")
  | .spec.template.spec.containers[0].image' "$scratch/manifests-claim.yaml" \
    | grep -qv '@' || die "the tag-only image is pinned to a digest anyway"

# Invalid digests are rejected by the config validator, including a truncated
# value and a non-sha256 algorithm.
bad_digests=(
    "sha256:abc"
    "sha256:$(printf 'a%.0s' {1..63})"
    "sha256:$(printf 'A%.0s' {1..64})"
    "md5:$(printf 'a%.0s' {1..64})"
    "$(printf 'a%.0s' {1..64})"
)
for bad in "${bad_digests[@]}"; do
    printf '[repository]\nurl = "git://git.example.test/platform.git"\n[agents]\nimage_repository = "ghcr.io/psiace/anylings/bub"\nimage_digest = "%s"\n' \
        "$bad" >"$scratch/bad-digest.toml"
    if ANYLINGS_CONFIG="$scratch/bad-digest.toml" just check config >/dev/null 2>&1; then
        die "the config validator accepted the invalid digest '${bad:0:24}...'"
    fi
done

log "all checks passed"
