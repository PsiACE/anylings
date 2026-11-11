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
# blocking on a registry.
cat >"$scratch/without-agents.toml" <<'TOML'
[repository]
url = "git://git.example.test/platform.git"
TOML
(
    export ANYLINGS_CONFIG="$scratch/without-agents.toml"
    export ANYLINGS_OUTPUT_DIR="$scratch/rendered-without"
    export SOPS_AGE_KEY_FILE="$scratch/age.key"
    just init platform >/dev/null
    just configure platform >/dev/null
)
[ ! -e "$scratch/rendered-without/kubernetes/apps/agents/t3-code/config.json" ] \
    || die "an unconfigured agent runtime was rendered anyway"
[ -f "$scratch/rendered-without/kubernetes/apps/default/magpie/config.json" ] \
    || die "omitting the agent runtime also dropped an existing workload"

log "render the template and validate it as app-template input"

# A synthetic config: a registry and claim name that only exist in this check.
cat >"$scratch/cluster.toml" <<'TOML'
[repository]
url = "git://git.example.test/platform.git"

[agents]
image_repository = "registry.example.test/anylings/t3-code"
storage_existing_claim = "t3-code-state"
TOML

export ANYLINGS_CONFIG="$scratch/cluster.toml"
export ANYLINGS_OUTPUT_DIR="$scratch/rendered"
export SOPS_AGE_KEY_FILE="$scratch/age.key"
just init platform >/dev/null
just configure platform >/dev/null

values="$ANYLINGS_OUTPUT_DIR/kubernetes/apps/agents/t3-code/values.yaml"
config="$ANYLINGS_OUTPUT_DIR/kubernetes/apps/agents/t3-code/config.json"
[ -f "$values" ] || die "the runtime values were not rendered"
[ -f "$config" ] || die "the runtime app config was not rendered"

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

# Render through the pinned chart: a values file that app-template rejects, or
# probes it cannot build, fails here rather than at the first sync.
app_template="$scratch/chart/app-template"
if helm pull oci://ghcr.io/bjw-s-labs/helm/app-template \
    --version "$(jq -r '.chartVersion' "$config")" \
    --untar --untardir "$scratch/chart" >/dev/null 2>&1; then
    helm template t3-code "$app_template" \
        --namespace agents --values "$values" >"$scratch/manifests.yaml"

    # The rendered workload keeps the properties the values promise, so a chart
    # upgrade that drops one is caught here.
    python3 - "$scratch/manifests.yaml" <<'PY'
import sys
import yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
by_kind = {d["kind"]: d for d in docs}
for kind in ("Deployment", "Service"):
    if kind not in by_kind:
        raise SystemExit(f"the chart rendered no {kind}")

deployment = by_kind["Deployment"]["spec"]
container = deployment["template"]["spec"]["containers"][0]
checks = {
    "one replica": deployment.get("replicas") == 1,
    "recreate strategy": deployment.get("strategy", {}).get("type") == "Recreate",
    "http port": any(p.get("containerPort") == 3773 for p in container.get("ports", [])),
    "readiness probe": "readinessProbe" in container,
    "no privilege escalation": container["securityContext"].get("allowPrivilegeEscalation") is False,
    "all capabilities dropped": container["securityContext"]["capabilities"]["drop"] == ["ALL"],
    "nonroot pod": deployment["template"]["spec"]["securityContext"].get("runAsUser") == 65532,
    "resource limits": "limits" in container.get("resources", {}),
}
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
    log "app-template rendered the runtime with the expected properties"
else
    # Say so rather than passing silently: this is the only check that exercises
    # the real chart, and it needs registry access.
    log "SKIPPED chart rendering: the app-template chart could not be pulled"
    skipped_chart=1
fi

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

    # Both executables, as the image's own user, with no network.
    "$container_tool" run --rm --network=none --entrypoint /bin/sh "$IMAGE" -c '
        set -e
        id -u | grep -qx 65532
        t3 --version | grep -q "^t3 v"
        bub --version | grep -q "^bub "
        /opt/bub/bin/python --version
    ' || die "the image does not provide the expected nonroot toolchain"

    # The version the image actually carries must be the one the template
    # deploys, so the rendered tag cannot drift from the built binary.
    image_t3="$("$container_tool" run --rm --network=none --entrypoint /bin/sh "$IMAGE" \
        -c 't3 --version' | awk '{print $2}' | tr -d 'v')"
    deployed_t3="$(yq -r '.controllers."t3-code".containers.app.image.tag' "$values")"
    [ "$image_t3" = "$deployed_t3" ] \
        || die "image carries T3 $image_t3 but the template deploys $deployed_t3"

    # The startup contract: a fresh, empty state root is created and served.
    # --network=none means nothing leaves the machine.
    "$container_tool" run --rm --network=none --entrypoint /bin/sh "$IMAGE" -c '
        set -e
        rm -rf /home/t3/.t3
        test -w /home/t3
        timeout 60 /usr/local/bin/t3-entrypoint >/tmp/out 2>/tmp/err || true
        grep -q "server is ready\|Migrations ran" /tmp/out /tmp/err
    ' || die "the image did not start its server with a fresh state root"

    log "the built image provides both toolchains and starts as nonroot"
fi

if [ "${skipped_chart:-0}" = 1 ]; then
    log "completed with one gap: the pinned chart could not be pulled here"
else
    log "all checks passed"
fi
