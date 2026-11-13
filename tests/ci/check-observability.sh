#!/usr/bin/env bash
# Focused contract checks for the optional self-hosted observability slice.
#
# Renders the real configuration input, then renders the actually pinned Helm
# charts from that output with `helm template`. Every assertion below is about
# what those charts produce, not about the values file's intent, so a chart
# upgrade that changes a name, a Service type or a security default fails here.
#
# No cluster, cloud API, credential or private key is read: the age identity is
# generated locally for this run and the only Secret input is a synthetic name.
set -euo pipefail

# Runtime is opt-in and uses explicitly supplied disposable release binaries.
# It never downloads tools, invokes Kubernetes, or starts detached services.
case "${1:-}" in
  ""|--runtime) ;;
  *) echo "usage: $0 [--runtime]" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

log() { printf '[observability] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

for tool in helm jq yq just kubeconform uv; do
  command -v "$tool" >/dev/null || die "$tool is required"
done

scratch="$(mktemp -d "${TMPDIR:-/tmp}/anylings-observability.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
chmod 700 "$scratch"

export SOPS_AGE_KEY_FILE="$scratch/age.key"

# shellcheck source=tests/ci/fixture-input.sh
. tests/ci/fixture-input.sh

# Every check below compares against a captured string with a here-string rather
# than a `command | grep -q` pipeline. `grep -q` closes its input as soon as it
# matches, which sends SIGPIPE to the producing command, and `pipefail` would
# then report the whole pipeline as failed even though the match succeeded.
first_line() {
  local value="$1"
  printf '%s\n' "${value%%$'\n'*}"
}

# --------------------------------------------------------------- render cases --

# Render one supported configuration input through the normal platform entry
# points, into its own output directory.
render_case() {
  local name="$1" input="$2" out="$scratch/rendered-$1" config
  mkdir -p "$out" || die "could not create the $name output directory"
  # The helper also writes the synthetic baseline tunnel token beside the copy.
  config="$(prepare_platform_input "$input" "$scratch/$name")" \
    || die "could not prepare the $name input"
  (
    export ANYLINGS_CONFIG="$config"
    export ANYLINGS_OUTPUT_DIR="$out"
    just init platform >/dev/null
    just configure platform >/dev/null
  )
  printf '%s\n' "$out"
}

log "render the default configuration (observability must stay absent)"
default_out="$(render_case default tests/config/base.toml)"

log "render the configuration that enables observability"
enabled_out="$(render_case observability tests/config/observability.toml)"

# ------------------------------------------------------------- default off --

check_default_off() {
  local apps="$default_out/kubernetes/apps" project="$default_out/kubernetes/projects/kubernetes.yaml"
  local text
  [ ! -e "$apps/observability" ] \
    || die "observability applications were rendered while the input left it disabled"
  # An empty directory would still be discovered as a workload path, so make
  # sure no config.json exists anywhere beneath it.
  local published
  published="$(find "$apps" -path '*/observability/*' -name 'config.json' -print -quit)"
  [ -z "$published" ] \
    || die "observability config.json was published without being enabled: $published"
  text="$(cat "$project")"
  if grep -q 'namespace: observability' <<<"$text"; then
    die "the AppProject grants the observability namespace while the slice is disabled"
  fi
  if grep -qE 'victoriametrics|open-telemetry|prometheus-community' <<<"$text"; then
    die "the AppProject allows a monitoring chart registry while the slice is disabled"
  fi
  log "default-off holds: no applications, namespace or extra chart repo"
}

# ----------------------------------------------------------- enabled slice --

check_enabled_apps() {
  local apps="$enabled_out/kubernetes/apps/observability" project="$enabled_out/kubernetes/projects/kubernetes.yaml"
  local app text
  for app in victoria-metrics gatus otel-kubelet kube-state-metrics victoria-logs victoria-traces otel-events; do
    [ -f "$apps/$app/config.json" ] || die "$app was not published as an application"
    [ -f "$apps/$app/values.yaml" ] || die "$app has no rendered values file"
  done
  # Pinned coordinates and a private namespace, expressed in the same shape the
  # ApplicationSet already consumes for every other workload.
  jq -e '.namespace == "observability"' "$apps/gatus/config.json" >/dev/null \
    || die "gatus is not destined for the observability namespace"
  jq -e '.chart == "victoria-metrics-single" and .chartVersion == "0.50.0"' \
    "$apps/victoria-metrics/config.json" >/dev/null \
    || die "the metric store chart is not pinned to the reviewed version"
  jq -e '.chart == "app-template" and .chartVersion == "5.3.0"' "$apps/gatus/config.json" >/dev/null \
    || die "gatus does not reuse the platform's app-template version"
  text="$(cat "$project")"
  grep -q 'ghcr.io/victoriametrics/helm-charts' <<<"$text" \
    || die "the AppProject does not allow the pinned VictoriaMetrics chart registry"
  grep -q 'namespace: observability' <<<"$text" \
    || die "the AppProject does not allow the observability destination"
  jq -e '.chart == "victoria-logs-single" and .chartVersion == "0.13.12"' \
    "$apps/victoria-logs/config.json" >/dev/null \
    || die "the log store chart is not pinned to the reviewed version"
  jq -e '.chart == "opentelemetry-collector" and .chartVersion == "0.140.0"' \
    "$apps/otel-events/config.json" >/dev/null \
    || die "the event collector reuses the pinned collector chart"
  jq -e '.chart == "victoria-traces-single" and .chartVersion == "0.1.13"' \
    "$apps/victoria-traces/config.json" >/dev/null \
    || die "the trace store chart is not pinned to the reviewed version"
  log "enabled slice publishes seven pinned applications with a scoped AppProject"
}

# -------------------------------------------------------- chart rendering --

# Render a chart from the published values, exactly as Argo would: the pinned
# chart coordinates come from config.json, never from this script.
chart_render() {
  local dir="$1" out="$2" repo chart version
  repo="$(jq -r '.chartRepo' "$dir/config.json")"
  chart="$(jq -r '.chart' "$dir/config.json")"
  version="$(jq -r '.chartVersion' "$dir/config.json")"
  log "render $chart@$version from $repo"
  helm template "$(jq -r '.appName' "$dir/config.json")" "oci://$repo/$chart" \
    --version "$version" \
    --namespace "$(jq -r '.namespace' "$dir/config.json")" \
    --values "$dir/values.yaml" >"$out" \
    || die "helm template failed for $chart@$version from $repo"
  [ -s "$out" ] || die "$chart@$version rendered nothing"
}

# A workload may only be reachable in-cluster: no route, no public Service type.
assert_no_public_exposure() {
  local out="$1" label="$2" kinds types
  kinds="$(yq -r 'select(. != null) | .kind' "$out" | sort -u)"
  [ -n "$kinds" ] || die "$label rendered no resources"
  for forbidden in Ingress HTTPRoute Gateway LoadBalancer NodePort; do
    if grep -qx "$forbidden" <<<"$kinds"; then
      die "$label rendered a $forbidden resource"
    fi
  done
  # Services are the only place a type can widen; check every one of them.
  types="$(yq -r 'select(.kind == "Service") | .spec.type // "ClusterIP"' "$out")"
  if [ -n "$types" ] && grep -qvx 'ClusterIP' <<<"$types"; then
    die "$label rendered a Service that is not ClusterIP"
  fi
  log "$label renders no Ingress, route, LoadBalancer or NodePort"
}

check_metric_store() {
  local out="$scratch/victoria-metrics.yaml" args uid drops
  chart_render "$enabled_out/kubernetes/apps/observability/victoria-metrics" "$out"

  yq -e 'select(.kind == "Deployment" or .kind == "StatefulSet")' "$out" >/dev/null \
    || die "the metric store rendered no workload"
  args="$(yq -r 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[].args[]' "$out")"
  # An explicit retention window, on the real container arguments.
  grep -qx -- '--retentionPeriod=7d' <<<"$args" \
    || die "the metric store does not run with an explicit 7d retention"
  # Resource caps, including a memory limit that the VM flags cannot replace.
  yq -e 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[] | .resources.limits.memory' "$out" >/dev/null \
    || die "the metric store container has no memory limit"
  yq -e 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[] | .resources.requests.memory' "$out" >/dev/null \
    || die "the metric store container has no memory request"
  # Cardinality and free-disk budgets are what keeps a small footprint small.
  for flag in --maxLabelsPerTimeseries --storage.maxHourlySeries \
    --storage.maxDailySeries --storage.minFreeDiskSpaceBytes --memory.allowedBytes; do
    grep -q -- "^$flag=" <<<"$args" \
      || die "the metric store does not bound $flag"
  done
  grep -qE '^--memory.allowedBytes=[0-9]+$' <<<"$args" \
    || die "the metric store memory byte budget is not a valid integer"
  # Non-root, read-only root filesystem, no added capabilities.
  yq -e 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.securityContext.runAsNonRoot == true' "$out" >/dev/null \
    || die "the metric store pod does not require a non-root user"
  uid="$(yq -r 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.securityContext.runAsUser' "$out")"
  [ -n "$uid" ] || die "the metric store pod declares no uid"
  if grep -qx '0' <<<"$uid"; then
    die "the metric store pod runs as uid 0"
  fi
  yq -e 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[].securityContext.readOnlyRootFilesystem == true' "$out" >/dev/null \
    || die "the metric store container has a writable root filesystem"
  drops="$(yq -r 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[].securityContext.capabilities.drop[]' "$out")"
  grep -qx 'ALL' <<<"$drops" \
    || die "the metric store container does not drop all capabilities"
  # Storage ownership: no claim is invented, and the scratch space is bounded.
  if yq -e 'select(.kind == "PersistentVolumeClaim")' "$out" >/dev/null 2>&1; then
    die "the metric store provisions a PersistentVolumeClaim on its own"
  fi
  yq -e 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.volumes[] | select(.emptyDir) | .emptyDir.sizeLimit' "$out" >/dev/null \
    || die "the metric store scratch volume has no sizeLimit"
  # Least privilege: this deployment scrapes static targets and must not hold
  # cluster-wide read access to Nodes, Pods or Ingresses.
  local kind
  for kind in ClusterRole ClusterRoleBinding; do
    if yq -e "select(.kind == \"$kind\")" "$out" >/dev/null 2>&1; then
      die "the metric store renders a $kind, which its static targets do not need"
    fi
  done
  assert_no_public_exposure "$out" "the metric store"
  log "metric store: 7d retention, capped, non-root, ClusterIP, no claim, no cluster RBAC"
}

check_status_dashboards() {
  local out="$scratch/gatus.yaml" data config
  local app="$enabled_out/kubernetes/apps/observability/gatus"
  chart_render "$app" "$out"

  yq -e 'select(.kind == "Deployment")' "$out" >/dev/null || die "gatus rendered no Deployment"
  # Credentials arrive only by reference to an externally managed Secret.
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].envFrom[] | select(.secretRef)' "$out" >/dev/null \
    || die "gatus has no Secret reference for its notification credentials"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].envFrom[] | select(.secretRef) | .secretRef.optional == true' \
    "$out" >/dev/null \
    || die "the notification Secret reference is required, so a cluster without it cannot start"
  data="$(yq -r 'select(.kind == "ConfigMap") | .data[]' "$out")"
  # No literal credential may appear in the rendered workload: the values file
  # carries ${VAR} placeholders, which Gatus expands from the environment.
  if grep -qE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' <<<"$data"; then
    die "a Telegram bot token literal was rendered into the Gatus ConfigMap"
  fi
  config="$(yq -r 'select(.kind == "ConfigMap") | .data["config.yaml"] // ""' "$out")"
  grep -q 'token: "${TELEGRAM_BOT_TOKEN}"' <<<"$config" \
    || die "gatus does not read its bot token from the environment"
  # Bounded memory history, and the file states that it is not durable.
  grep -qE '^\s+type: memory$' <<<"$config" \
    || die "gatus does not use its bounded in-memory store"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].resources.limits.memory' "$out" >/dev/null \
    || die "gatus has no memory limit, so its in-memory history is unbounded in effect"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.securityContext.runAsNonRoot == true' "$out" >/dev/null \
    || die "gatus does not require a non-root user"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].securityContext.readOnlyRootFilesystem == true' "$out" >/dev/null \
    || die "gatus has a writable root filesystem"
  if yq -e 'select(.kind == "PersistentVolumeClaim")' "$out" >/dev/null 2>&1; then
    die "gatus provisions a PersistentVolumeClaim on its own"
  fi
  assert_no_public_exposure "$out" "gatus"
  log "gatus: optional Secret reference, bounded memory store, capped, ClusterIP"
}

# ------------------------------------------------------------ live wiring --

# The dashboards are only useful if vmui serves them, and the probes are only
# useful if they name Services that the same render actually creates.
check_wiring() {
  local vm="$scratch/victoria-metrics.yaml" gatus="$scratch/gatus.yaml"
  local args flag mount configmap manifest services service

  # The flag, the mount and the ConfigMap must agree; a dashboard that nothing
  # reads is an empty placeholder however good its queries are.
  args="$(yq -r 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[].args[]' "$vm")"
  flag="$(first_line "$(sed -n 's/^--vmui\.customDashboardsPath=//p' <<<"$args")")"
  [ -n "$flag" ] || die "the metric store does not enable vmui dashboards"
  mount="$(yq -r 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.containers[].volumeMounts[] | select(.mountPath == "'"$flag"'") | .name' "$vm")"
  [ -n "$mount" ] || die "no volume is mounted at the dashboard path $flag"
  configmap="$(yq -r 'select(.kind == "Deployment" or .kind == "StatefulSet")
    | .spec.template.spec.volumes[] | select(.name == "'"$mount"'") | .configMap.name' "$vm")"
  [ -n "$configmap" ] || die "the dashboard mount is not backed by a ConfigMap"
  [ "$configmap" = "victoria-metrics-dashboards" ] \
    || die "the dashboard volume names $configmap, not the published ConfigMap"

  # That ConfigMap is a plain manifest installed by the same Application, so it
  # must exist in the rendered sources with real dashboard documents.
  manifest="$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml"
  [ -f "$manifest" ] || die "the dashboards ConfigMap was not published"
  yq -e 'select(.kind == "ConfigMap" and .metadata.name == "'"$configmap"'")' "$manifest" >/dev/null \
    || die "the published manifest does not define $configmap"

  # Each dashboard must parse, contain panels, and every single panel must carry
  # at least one non-empty query. Counting panels and queries separately would
  # accept one panel with an empty expr list, which renders as an empty chart --
  # exactly the failure this check exists to catch.
  local file key empty_panels total_panels queries
  for key in $(yq -r 'select(.kind == "ConfigMap") | .data | keys[]' "$manifest"); do
    file="$scratch/$key"
    yq -r 'select(.kind == "ConfigMap") | .data["'"$key"'"]' "$manifest" >"$file"
    jq -e . "$file" >/dev/null || die "$key is not valid dashboard JSON"
    jq -e '.title | length > 0' "$file" >/dev/null || die "$key has no title"
    total_panels="$(jq '[.rows[].panels[]] | length' "$file")"
    [ "$total_panels" -gt 0 ] || die "$key has no panels"
    empty_panels="$(jq '[.rows[].panels[] | select(([.expr[]? | select(length > 0)] | length) == 0)] | length' "$file")"
    [ "$empty_panels" -eq 0 ] \
      || die "$key has $empty_panels panel(s) without a query"
    # A query that is obviously a placeholder is not a dashboard.
    queries="$(jq -r '.rows[].panels[].expr[]' "$file")"
    if grep -qE '^\s*$|TODO|TBD|PLACEHOLDER|example_metric' <<<"$queries"; then
      die "$key contains a placeholder query"
    fi

    # Every `job="..."` matcher must name a scrape job this render actually
    # declares, or the panel is permanently empty while still looking valid.
    local names matcher
    names="$(yq -r 'select(.kind == "ConfigMap") | .data["scrape.yml"] // ""' "$vm" \
      | sed -n 's/^ *- *job_name: *\(.*\)$/\1/p')"
    [ -n "$names" ] || die "the metric store renders no scrape job_name, so panels cannot select one"
    for matcher in $(grep -oE 'job="[^"]+"' <<<"$queries" | sed 's/^job="//;s/"$//' | sort -u); do
      grep -qx -- "$matcher" <<<"$names" \
        || die "$key queries job=\"$matcher\", which no rendered scrape job declares ($(tr '\n' ' ' <<<"$names"))"
    done
  done

  # Cross-application wiring: the scrape target and the probes must name
  # Services that this render creates, not names invented in the values file.
  services="$(yq -r 'select(.kind == "Service") | .metadata.name' "$gatus")"
  service="$(first_line "$services")"
  [ -n "$service" ] || die "gatus rendered no Service"
  local targets host target found=""
  targets="$(yq -r 'select(.kind == "ConfigMap") | .data["scrape.yml"] // ""' "$vm" \
    | sed -n 's/^ *- \([A-Za-z0-9][A-Za-z0-9.:_-]*:[0-9]\+\)$/\1/p')"
  # A scrape target may be the bare Service name or its fully qualified form;
  # both resolve to the same Service, so compare the leading label only.
  for target in $targets; do
    host="${target%:*}"
    [ "${host%%.*}" = "$service" ] && found=1
  done
  [ -n "$found" ] || die "the metric store does not scrape the gatus Service $service"

  local probes url port
  probes="$(yq -r 'select(.kind == "ConfigMap") | .data["config.yaml"] // ""' "$gatus")"
  probes="$(grep -oE 'http://[a-z0-9.-]+\.observability\.svc\.cluster\.local:[0-9]+' <<<"$probes" | sort -u)"
  [ -n "$probes" ] || die "gatus declares no in-cluster probe"
  local vm_services
  vm_services="$(yq -r 'select(.kind == "Service") | .metadata.name' "$vm")"
  for url in $probes; do
    host="${url#http://}"
    port="${host##*:}"
    host="${host%%:*}"
    target="${host%%.*}"
    grep -qx "$target" <<<"$vm_services" \
      || die "gatus probes $target, which the metric store render does not create"
    if [ "$target" = "victoria-metrics" ]; then
      yq -e 'select(.kind == "Service" and .metadata.name == "victoria-metrics")
        | .spec.ports[] | select(.port == '"$port"')' "$vm" >/dev/null \
        || die "the probe port $port is not exposed by the victoria-metrics Service"
    fi
  done
  log "dashboards, mount, vmui flag, scrape target and probe Services all agree"
}


# Check the actual chart output, including defaults that values-only tests miss.
check_workload_metrics() {
  local app
  for app in otel-kubelet kube-state-metrics; do
    chart_render "$enabled_out/kubernetes/apps/observability/$app" "$scratch/$app.yaml"
    assert_no_public_exposure "$scratch/$app.yaml" "$app"
    kubeconform -strict -summary "$scratch/$app.yaml"
  done
  # Decode YAML with the existing yq tool; the assertions need only stdlib.
  for app in otel-kubelet kube-state-metrics victoria-metrics; do
    yq -o=json -I=0 '.' "$scratch/$app.yaml" | jq -s '.' >"$scratch/$app.json"
  done
  kubeconform -strict -summary "$enabled_out/kubernetes/apps/observability/otel-kubelet/service.yaml" \
    "$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml"
  yq -o=json '.' "$enabled_out/kubernetes/apps/observability/otel-kubelet/service.yaml" >"$scratch/headless.json"
  yq -r 'select(.kind == "ConfigMap") | .data.relay // ""' "$scratch/otel-kubelet.yaml" >"$scratch/collector.yaml"
  yq -o=json '.' "$scratch/collector.yaml" >"$scratch/collector.json"
  yq -r 'select(.kind == "ConfigMap") | .data["scrape.yml"] // ""' "$scratch/victoria-metrics.yaml" >"$scratch/scrape.yaml"
  yq -o=json '.' "$scratch/scrape.yaml" >"$scratch/scrape.json"
  uv run --quiet --locked --no-dev python - "$scratch" "$enabled_out" <<'PYTEST'
import json
import sys
from pathlib import Path

scratch, enabled = map(Path, sys.argv[1:])

def read(name):
    return json.loads((scratch / name).read_text())

def only(documents, kind):
    matches = [doc for doc in documents if doc and doc['kind'] == kind]
    assert len(matches) == 1, (kind, len(matches))
    return matches[0]

collector_docs = read('otel-kubelet.json')
state_docs = read('kube-state-metrics.json')
collector = read('collector.json')
state_config = json.loads((enabled / 'kubernetes/apps/observability/kube-state-metrics/config.json').read_text())
assert state_config['chartVersion'] == '9.0.1'
agent_config = json.loads((enabled / 'kubernetes/apps/observability/otel-kubelet/config.json').read_text())
assert agent_config['chartVersion'] == '0.140.0'

# No write verbs, proxy, Secrets, ConfigMaps, or unrelated cluster inventory.
expected_state = {
    ('', 'nodes'), ('', 'pods'),
    *{('apps', name) for name in ('deployments', 'replicasets', 'daemonsets', 'statefulsets')},
    *{('batch', name) for name in ('jobs', 'cronjobs')},
}
for documents, expected, verbs in (
    (collector_docs, {('', 'nodes/stats')}, {'get'}),
    (state_docs, expected_state, {'list', 'watch'}),
):
    role = only(documents, 'ClusterRole')
    grants = set()
    for rule in role['rules']:
        assert set(rule['verbs']) == verbs, rule
        assert not rule.get('nonResourceURLs'), rule
        grants.update((group, resource) for group in rule['apiGroups'] for resource in rule['resources'])
    assert grants == expected, grants
    account = only(documents, 'ServiceAccount')['metadata']['name']
    binding = only(documents, 'ClusterRoleBinding')
    assert binding['roleRef']['name'] == role['metadata']['name']
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': account, 'namespace': 'observability'}]
    workload = only(documents, 'DaemonSet' if documents is collector_docs else 'Deployment')
    pod = workload['spec']['template']['spec']
    assert pod['serviceAccountName'] == account
    assert pod['automountServiceAccountToken'] is True
    assert not any(pod.get(key) for key in ('hostNetwork', 'hostPID', 'hostIPC'))
    assert not any('hostPath' in volume for volume in pod.get('volumes', []))
    assert pod['securityContext']['runAsNonRoot']
    assert pod['securityContext']['runAsUser'] != 0
    assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    assert len(pod['containers']) == 1
    container = pod['containers'][0]
    for resource in ('cpu', 'memory'):
        assert container['resources']['requests'][resource]
        assert container['resources']['limits'][resource]
    security = container['securityContext']
    assert security['readOnlyRootFilesystem']
    assert not security['allowPrivilegeEscalation']
    assert not security.get('privileged')
    assert security['capabilities'] == {'drop': ['ALL']}
    assert not any(port.get('hostPort') for port in container.get('ports', []))
    assert not any(doc['kind'] in ('PersistentVolumeClaim', 'StatefulSet') for doc in documents if doc)

pod_template = only(collector_docs, 'DaemonSet')['spec']['template']
agent = pod_template['spec']['containers'][0]
assert agent['image'] == 'otel/opentelemetry-collector-contrib:0.140.1'
assert any(env['name'] == 'K8S_NODE_NAME' and env['valueFrom']['fieldRef']['fieldPath'] == 'spec.nodeName' for env in agent['env'])
assert {port['containerPort'] for port in agent['ports']} == {8889}
assert set(collector['receivers']) == {'kubeletstats'}
assert set(collector['exporters']) == {'prometheus'}
assert set(collector['service']['pipelines']) == {'metrics'}
assert collector['service']['pipelines']['metrics'] == {
    'receivers': ['kubeletstats'],
    'processors': ['memory_limiter', 'transform/identity'],
    'exporters': ['prometheus'],
}
receiver = collector['receivers']['kubeletstats']
assert receiver['auth_type'] == 'serviceAccount'
assert receiver['endpoint'] == 'https://${env:K8S_NODE_NAME}:10250'
assert receiver['ca_file'] == '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
assert receiver['insecure_skip_verify'] is False
assert set(receiver['metric_groups']) == {'node', 'pod', 'container', 'volume'}
assert receiver['extra_metadata_labels'] == []
assert 'k8s_api_config' not in receiver
assert collector['exporters']['prometheus']['metric_expiration'] == '2m'
statements = collector['processors']['transform/identity']['metric_statements']
assert statements == [{'context': 'resource', 'statements': [
    'keep_keys(attributes, ["k8s.node.name", "k8s.namespace.name", "k8s.pod.name", "k8s.container.name", "k8s.volume.name"])'
]}]

# Selector agreement plus DNS A discovery ensures every ready DaemonSet Pod is
# scraped individually. VM itself still needs no Kubernetes API permissions.
headless = read('headless.json')
assert headless['spec']['clusterIP'] == 'None'
assert headless['spec']['type'] == 'ClusterIP'
assert all(pod_template['metadata']['labels'].get(key) == value for key, value in headless['spec']['selector'].items())
assert headless['spec']['ports'] == [{'name': 'metrics', 'port': 8889, 'targetPort': 'metrics', 'protocol': 'TCP'}]
jobs = {job['job_name']: job for job in read('scrape.json')['scrape_configs']}
assert jobs['kubelet']['dns_sd_configs'] == [{
    'names': ['otel-kubelet.observability.svc.cluster.local'],
    'type': 'A', 'port': 8889,
}]
assert not jobs['kubelet'].get('static_configs')
assert jobs['kubelet']['sample_limit'] == 5000
assert jobs['kube-state-metrics']['sample_limit'] == 5000
assert jobs['kube-state-metrics']['metric_relabel_configs'] == [{'action': 'labeldrop', 'regex': 'uid'}]
state_service = only(state_docs, 'Service')
assert state_service['metadata']['name'] == 'kube-state-metrics'
assert any(port['port'] == 8080 for port in state_service['spec']['ports'])
assert jobs['kube-state-metrics']['static_configs'] == [{'targets': ['kube-state-metrics.observability.svc.cluster.local:8080']}]
state = only(state_docs, 'Deployment')
assert state['spec']['replicas'] == 1
container = state['spec']['template']['spec']['containers'][0]
assert container['image'] == 'registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0'
args = container['args']
resources = next(arg for arg in args if arg.startswith('--resources='))
assert set(resources.split('=', 1)[1].split(',')) == {resource for _, resource in expected_state}
allowlist = next(arg for arg in args if arg.startswith('--metric-allowlist='))
for metric in ('kube_pod_container_status_restarts_total', 'kube_pod_container_status_last_terminated_reason', 'kube_pod_status_scheduled', 'kube_pod_container_resource_requests', 'kube_pod_owner', 'kube_deployment_status_replicas_available'):
    assert metric in allowlist.split('=', 1)[1].split(',')
assert not any(arg.startswith(('--metric-labels-allowlist=', '--metric-annotations-allowlist=')) for arg in args)
print('[observability] pinned collectors: minimal RBAC, TLS/SA, resources, privacy and all-Pod DNS discovery passed')
PYTEST
}

# ------------------------------------------------------- operational logs --

# The log store must bound itself by age, by disk and by scratch volume, and
# must not obtain a claim or a cluster role it has no use for.
check_log_store() {
  local out="$scratch/victoria-logs.yaml" args workload
  chart_render "$enabled_out/kubernetes/apps/observability/victoria-logs" "$out"
  assert_no_public_exposure "$out" "the log store"
  kubeconform -strict -summary "$out"

  workload='select(.kind == "Deployment" or .kind == "StatefulSet")'
  yq -e "$workload" "$out" >/dev/null || die "the log store rendered no workload"
  args="$(yq -r "$workload | .spec.template.spec.containers[].args[]" "$out")"
  grep -qx -- '--retentionPeriod=3d' <<<"$args" \
    || die "the log store does not run with an explicit 3d retention"
  grep -qx -- '--retention.maxDiskSpaceUsageBytes=1GiB' <<<"$args" \
    || die "the log store does not cap the disk its retention may use"
  for flag in --memory.allowedBytes --insert.maxLineSizeBytes \
    --insert.maxFieldsPerLine --maxConcurrentInserts --search.maxQueryDuration; do
    grep -q -- "^$flag=" <<<"$args" \
      || die "the log store does not bound $flag"
  done
  yq -e "$workload | .spec.template.spec.containers[] | .resources.limits.memory" "$out" >/dev/null \
    || die "the log store container has no memory limit"
  yq -e "$workload | .spec.template.spec.containers[] | .resources.requests.memory" "$out" >/dev/null \
    || die "the log store container has no memory request"
  yq -e "$workload | .spec.template.spec.securityContext.runAsNonRoot == true" "$out" >/dev/null \
    || die "the log store pod does not require a non-root user"
  yq -e "$workload | .spec.template.spec.containers[].securityContext.readOnlyRootFilesystem == true" "$out" >/dev/null \
    || die "the log store container has a writable root filesystem"
  if [ -n "$(yq -r "$workload | .spec.template.spec.securityContext.runAsUser" "$out")" ]; then
    [ "$(yq -r "$workload | .spec.template.spec.securityContext.runAsUser" "$out")" != 0 ] \
      || die "the log store pod runs as uid 0"
  fi
  # An emptyDir with a sizeLimit, never a claim the cluster would bill for.
  yq -e "$workload | .spec.template.spec.volumes[] | select(.emptyDir) | .emptyDir.sizeLimit" "$out" >/dev/null \
    || die "the log store scratch volume has no sizeLimit"
  if yq -e 'select(.kind == "PersistentVolumeClaim")' "$out" >/dev/null 2>&1; then
    die "the log store provisions a PersistentVolumeClaim on its own"
  fi
  # The chart's bundled Vector agent tails every container log on the node.
  # This slice ingests structured OTLP only, so it must not be rendered at all.
  if yq -e 'select(.kind == "DaemonSet")' "$out" >/dev/null 2>&1; then
    die "the log store chart rendered its host log-tailing DaemonSet"
  fi
  if yq -e 'select(.kind == "ServiceAccount") | .automountServiceAccountToken == true' "$out" >/dev/null 2>&1; then
    die "the log store mounts an API token it never uses"
  fi
  for kind in ClusterRole ClusterRoleBinding; do
    if yq -e "select(.kind == \"$kind\")" "$out" >/dev/null 2>&1; then
      die "the log store renders a $kind, which its HTTP ingest path does not need"
    fi
  done
  assert_no_public_exposure "$out" "the log store"
  log "log store: 3d + 1GiB caps, capped, non-root, ClusterIP, no claim, no cluster RBAC, no host agent"
}

# The event collector is the only component allowed to read the Kubernetes API
# for logs. Its grant is one read-only resource, and its configuration must
# prove that nothing outside the allowlist can reach the store.
check_event_collector() {
  local out="$scratch/otel-events.yaml" role config docs="$scratch/otel-events.json"
  local app="$enabled_out/kubernetes/apps/observability/otel-events"
  chart_render "$app" "$out"
  assert_no_public_exposure "$out" "the event collector"
  kubeconform -strict -summary "$out"

  yq -e 'select(.kind == "Deployment")' "$out" >/dev/null \
    || die "the event collector rendered no Deployment"
  # One replica: two watchers would duplicate every record.
  [ "$(yq -r 'select(.kind == "Deployment") | .spec.replicas' "$out")" = 1 ] \
    || die "the event collector is not a single-replica watcher"
  role="$(yq -o=json -I=0 'select(.kind == "ClusterRole")' "$out")"
  [ -n "$role" ] || die "the event collector renders no ClusterRole"
  jq -e '.rules == [{"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list", "watch"]}]' \
    <<<"$role" >/dev/null \
    || die "the event collector's RBAC is wider than read-only Events"
  # A pod mount of a host path or an API proxy would be a host-level log reader.
  if yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.volumes[] | select(.hostPath)' "$out" >/dev/null 2>&1; then
    die "the event collector mounts a host path"
  fi
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.securityContext.runAsNonRoot == true' "$out" >/dev/null \
    || die "the event collector does not require a non-root user"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].securityContext.readOnlyRootFilesystem == true' "$out" >/dev/null \
    || die "the event collector has a writable root filesystem"

  config="$(yq -r 'select(.kind == "ConfigMap") | .data.relay // ""' "$out")"
  [ -n "$config" ] || die "the event collector rendered no configuration"
  printf '%s\n' "$config" >"$scratch/rendered-events-collector.yaml"
  yq -o=json '.' "$scratch/rendered-events-collector.yaml" >"$docs"
  yq -o=json '.' "$scratch/rendered-events-collector.yaml" >"$scratch/rendered-events-collector.json"
  # Decode the real rendered configuration with the existing tools; every
  # assertion below is about what the collector will actually run.
  uv run --quiet --locked --no-dev python - "$docs" <<'PYLOG'
import json
import sys

config = json.load(open(sys.argv[1]))

# Exactly two inputs, and neither of them reads a file, a host path or a proxy.
assert set(config['receivers']) == {'k8sobjects', 'otlp'}, config['receivers']
events = config['receivers']['k8sobjects']
assert events['auth_type'] == 'serviceAccount', events
assert events['include_initial_state'] is False, events
assert events['objects'] == [{'name': 'events', 'mode': 'watch'}], events
assert set(config['receivers']['otlp']['protocols']) == {'http'}

# Two exporters: VictoriaLogs' own OTLP path for logs, and a separate trace
# exporter. Neither signal can land in the other store.
assert set(config['exporters']) == {'otlphttp/victoria-logs', 'otlphttp/victoria-traces'}
endpoint = config['exporters']['otlphttp/victoria-logs']['logs_endpoint']
assert endpoint == 'http://victoria-logs.observability.svc.cluster.local:9428/insert/opentelemetry/v1/logs', endpoint

pipelines = config['service']['pipelines']
assert set(pipelines) == {'logs/events', 'logs/structured', 'traces'}, pipelines
for name, pipeline in pipelines.items():
    if name.startswith('logs/'):
        assert pipeline['exporters'] == ['otlphttp/victoria-logs'], (name, pipeline)
    else:
        assert pipeline['exporters'] == ['otlphttp/victoria-traces'], (name, pipeline)
# Types are checked before any regex or range, that allowlist runs before the
# shape stage, and the shape stage runs before the exporter. A record rejected
# by the type guard can therefore never reach the statement that composes a
# body from it -- the ordering is the privacy guarantee, not a detail.
assert pipelines['logs/events']['processors'] == [
    'memory_limiter', 'transform/purge_envelope', 'transform/events_extract',
    'filter/events_types', 'filter/events_allowlist', 'transform/events_shape'], pipelines
assert pipelines['logs/structured']['processors'] == [
    'memory_limiter', 'transform/purge_envelope', 'transform/otlp_extract',
    'filter/otlp_types', 'filter/otlp_allowlist', 'transform/otlp_shape'], pipelines

# A log record carries an envelope the attributes allowlist never sees, and the
# store persists every part of it as its own field. The purge stage must run
# first in both pipelines and must clear each caller-controlled part; replacing
# them with a repository constant is fine, forwarding them is not.
purge = config['processors']['transform/purge_envelope']
assert purge['error_mode'] == 'ignore', purge
statements = {
    (group['context'], statement.split(',')[0].split('(')[1].strip())
    for group in purge['log_statements'] for statement in group['statements']
}
expected_purge = {
    ('resource', 'resource.schema_url'),
    ('scope', 'scope.name'),
    ('scope', 'scope.version'),
    ('scope', 'scope.attributes'),
    ('scope', 'scope.schema_url'),
    ('log', 'log.event_name'),
    ('log', 'log.trace_id'),
    ('log', 'log.span_id'),
    ('log', 'log.flags'),
    ('log', 'log.severity_text'),
}
assert statements == expected_purge, statements ^ expected_purge
# The scope name/version are the fixed values this repository owns, never a
# pass-through, and no scope attribute survives.
fixed_scope = {
    statement.split(',', 1)[0]: statement.rsplit(',', 1)[1].strip()
    for group in purge['log_statements'] if group['context'] == 'scope'
    for statement in group['statements'] if statement.startswith('set(scope.name') or statement.startswith('set(scope.version')
}
assert fixed_scope == {
    'set(scope.name': '"otel-events")',
    'set(scope.version': '"0")',
}, fixed_scope
assert any(statement == 'keep_keys(scope.attributes, [])'
           for group in purge['log_statements'] if group['context'] == 'scope'
           for statement in group['statements']), purge

# A repeated attribute key is validated on its first occurrence and stored from
# every occurrence, so the checked value and the stored value could differ. Both
# pipelines must normalise to exactly one occurrence before anything reads it,
# and the Events pipeline must discard inherited record attributes entirely.
extract = config['processors']['transform/otlp_extract']['log_statements']
log_statements = next(group['statements'] for group in extract if group['context'] == 'log')
assert any(statement.startswith('set(attributes["__otl_dup_reason"]') for statement in log_statements), log_statements
assert any(statement.startswith('keep_keys(attributes, ["__otl_dup_') for statement in log_statements), log_statements
for key in ('reason', 'level', 'component', 'event_count'):
    assert any(statement.startswith(f'set(attributes["{key}"], attributes["__otl_dup_') for statement in log_statements), key
    assert any(statement.startswith(f'delete_key(attributes, "__otl_dup_') for statement in log_statements), key
# The rebuild must be the only path by which an approved key survives: nothing
# may re-add the caller's original map wholesale.
assert not any('set(attributes,' in statement for statement in log_statements), log_statements
events_extract = config['processors']['transform/events_extract']['log_statements']
events_log = next(group['statements'] for group in events_extract if group['context'] == 'log')
assert events_log[0] == 'keep_keys(attributes, [])', events_log[:2]
assert not any('keep_keys(attributes, ["event_' in statement for statement in events_log), events_log
def assert_resource_normalised(processor, approved):
    """The resource map is normalised exactly like the record map.

    The resource attribute list repeats keys the same way the record list does,
    and `keep_keys` filters by name without collapsing repeats. So this asserts
    the copy-out / rebuild / delete *effect*, not a literal statement list: a
    different but equivalent normalisation is acceptable, while the pre-fix
    `keep_keys`-only stage is not. Asserting the text would forbid the only
    correct implementation, which is what this replaced.
    """
    groups = config['processors'][processor]['log_statements']
    resource = next(group['statements'] for group in groups if group['context'] == 'resource')
    scratch = [f"__otl_dup_{name}" for _, name in approved]
    # Each approved value is copied out under its own scratch key, under a
    # presence condition, from the approved source key.
    for source, name in approved:
        copy = f'set(attributes["__otl_dup_{name}"], attributes["{source}"])'
        assert any(statement.startswith(copy) for statement in resource), (processor, source, resource)
    # The map is rebuilt from the scratch keys alone, so no caller key survives
    # by accident and no unapproved key can be re-added.
    keep = next((statement for statement in resource if statement.startswith('keep_keys(attributes, [')), None)
    assert keep is not None, (processor, resource)
    kept = keep.split('[', 1)[1].rsplit(']', 1)[0]
    assert sorted(kept.replace('"', '').split(', ')) == sorted(scratch), (processor, kept, scratch)
    # Each scratch value is written back under the approved name.
    for source, name in approved:
        restore = f'set(attributes["{source}"], attributes["__otl_dup_{name}"])'
        assert any(statement.startswith(restore) for statement in resource), (processor, source, resource)
    # The stage must END with a whitelist over the approved names.
    #
    # Scratch keys are caller-writable: a sender may repeat `__otl_dup_ns` in
    # its own resource list. `delete_key` is `pdata.Map.Remove`, which removes
    # only the FIRST matching entry, so a cleanup built from `delete_key` leaves
    # an occurrence behind that is then stored as a new field and promoted into
    # `_stream`. `keep_keys` uses `RemoveIf` and drops every non-matching entry,
    # so the approved names are the only keys that can survive.
    expected_keep = 'keep_keys(attributes, [' + ', '.join(
        '"%s"' % source for source, _ in approved) + '])'
    assert resource[-1] == expected_keep, (processor, resource[-1], expected_keep)
    # No cleanup may depend on removing a name the caller can also supply.
    assert not any(statement.startswith('delete_key(attributes, "__otl_dup_')
                   for statement in resource), (processor, resource)
    # Nothing writes the caller's map wholesale, and no unapproved key is read.
    assert not any(statement.startswith('set(attributes,') for statement in resource), resource
    approved_sources = {source for source, _ in approved}
    for statement in resource:
        for _, name in approved:
            if statement.startswith(f'set(attributes["{name}"]'):
                assert f'__otl_dup_{name}' in statement, (processor, statement)
    return approved_sources


# Events carries namespace only; structured carries namespace and service.
assert assert_resource_normalised(
    'transform/events_extract', [('k8s.namespace.name', 'ns')]
) == {'k8s.namespace.name'}
assert assert_resource_normalised(
    'transform/otlp_extract',
    [('k8s.namespace.name', 'ns'), ('service.name', 'svc')],
) == {'k8s.namespace.name', 'service.name'}

# Integer contract: `event_count` is optional, but a value that is present must
# already be an integer. A range check alone is not a type check, and under
# `error_mode: ignore` a type error keeps the record, so this guard is what
# actually stops a string or a nested map from being stored.
for pipeline in ('events', 'otlp'):
    guards = config['processors'][f'filter/{pipeline}_types']['logs']['log_record']
    joined = '\n'.join(guards)
    count_guard = next(guard for guard in guards if 'event_count' in guard)
    assert 'not IsInt(attributes["event_count"])' in count_guard, count_guard
    # Absent is allowed; present-and-wrong is not. Anything else is not a guard.
    assert '!= nil' in count_guard and 'IsMatch' not in count_guard, count_guard
    # Every other accepted attribute is proven to be a string first, because a
    # regex on a map, array, bool or double raises and would keep the record.
    string_guards = [guard for guard in guards if 'IsString' in guard]
    expected_strings = 7 if pipeline == 'events' else 5
    assert len(string_guards) == expected_strings, (pipeline, guards)
    for guard in string_guards:
        assert 'not IsString' in guard, guard

# Drop-not-sanitize: every allowlist entry rejects a missing or malformed field
# outright, and no entry rewrites a value into an accepted one.
filters = config['processors']['filter/events_allowlist']['logs']['log_record']
assert len(filters) == 7, filters
textual = [condition for condition in filters if 'event_count' not in condition]
numeric = [condition for condition in filters if 'event_count' in condition]
assert len(textual) == 6 and len(numeric) == 1, filters
for condition in textual:
    assert condition.startswith('attributes['), condition
    assert 'IsMatch' in condition, condition
    # A missing or malformed field is rejected, never defaulted.
    assert '== nil or not IsMatch' in condition, condition
assert 'attributes["event_count"] < 0' in numeric[0], numeric
assert config['processors']['filter/events_allowlist']['error_mode'] == 'ignore'

# The composed body is built from allowlisted attributes only: a marker that
# only ever appeared in the original message cannot be concatenated here.
shape = config['processors']['transform/events_shape']['log_statements'][0]['statements']
compose = next(statement for statement in shape if 'set(log.body' in statement)
for field in ('event_type', 'object_kind', 'event_namespace', 'object_name', 'event_reason'):
    assert f'attributes["{field}"]' in compose, (field, compose)
for forbidden in ('message', 'note', 'annotations', 'metadata', 'body'):
    assert f'"{forbidden}"' not in compose, compose
keep = next(statement for statement in shape if statement.startswith('keep_keys'))
assert 'body' not in keep and 'message' not in keep, keep

# Optional parts must not be concatenated unconditionally: a missing component
# or namespace would render as `<nil>` in the stored message.
structured_shape = config['processors']['transform/otlp_shape']['log_statements'][0]['statements']
compose = next(statement for statement in structured_shape if 'set(log.body' in statement)
# The base body is built from the required field only.
assert 'attributes["level"]' in compose, compose
assert 'component' not in compose and 'k8s.namespace.name' not in compose, compose
appends = {
    statement for statement in structured_shape
    if 'set(log.body' in statement and 'Concat([log.body' in statement
}
# `reason` is required, so it is appended unconditionally. The two optional
# parts carry their own presence condition, which is what keeps `<nil>` and a
# doubled separator out of the stored message.
optional = sorted(
    next(field for field in ('component', 'k8s.namespace.name') if field in statement)
    for statement in appends if statement.endswith('!= nil')
)
assert optional == ['component', 'k8s.namespace.name'], appends
required = [statement for statement in appends if not statement.endswith('!= nil')]
assert len(required) == 1 and 'attributes["reason"]' in required[0], required
assert not any('<nil>' in statement for statement in structured_shape)

# The structured input requires an explicit level and reason, so an arbitrary
# application body cannot enter the store by arriving without them.
structured = config['processors']['filter/otlp_allowlist']['logs']['log_record']
joined = '\n'.join(structured)
assert 'attributes["reason"]' in joined and 'attributes["level"]' in joined
assert 'resource.attributes["service.name"]' in joined
print('[observability] rendered event collector: read-only Events RBAC, type guards before regex/range, nil-free body')
PYLOG

  yq -o=json '.' "$out" | jq -s '.' >"$docs"
  uv run --quiet --locked --no-dev python - "$docs" <<'PYRBAC'
import json
import sys

documents = [doc for doc in json.load(open(sys.argv[1])) if doc]
role = next(doc for doc in documents if doc['kind'] == 'ClusterRole')
binding = next(doc for doc in documents if doc['kind'] == 'ClusterRoleBinding')
account = next(doc for doc in documents if doc['kind'] == 'ServiceAccount')
assert binding['roleRef']['name'] == role['metadata']['name']
assert binding['subjects'] == [{
    'kind': 'ServiceAccount', 'name': account['metadata']['name'], 'namespace': 'observability'}]
deployment = next(doc for doc in documents if doc['kind'] == 'Deployment')
pod = deployment['spec']['template']['spec']
assert pod['serviceAccountName'] == account['metadata']['name']
assert not any(pod.get(key) for key in ('hostNetwork', 'hostPID', 'hostIPC'))
container = pod['containers'][0]
assert container['image'] == 'otel/opentelemetry-collector-contrib:0.140.1'
assert not any(port.get('hostPort') for port in container.get('ports', []))
assert {port['containerPort'] for port in container['ports']} == {4318, 8888}
assert not any(doc['kind'] in ('PersistentVolumeClaim', 'StatefulSet', 'DaemonSet') for doc in documents)
print('[observability] rendered event collector: SA binding, no host namespace or port, pinned image, ClusterIP only')
PYRBAC

  # The dashboard reads this collector's drop counter, so the metric must be
  # one the pinned collector really exports.
  grep -q 'otelcol_processor_filter_logs_filtered_total' \
    "$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml" \
    || die "the operational log dashboard does not read the collector's drop counter"
  log "event collector: read-only Events, single watcher, allowlist-only body, drop counter wired"
}

# ------------------------------------------------- frontdoor probes/delivery --

# The frontdoor aggregate comes from the tunnel's own counters, the probes name
# fixed operator-owned targets, and the notifier body is a closed template.
check_frontdoor_probe_delivery() {
  local vm="$scratch/victoria-metrics.yaml" gatus="$scratch/gatus.yaml" args config
  chart_render "$enabled_out/kubernetes/apps/observability/victoria-metrics" "$vm"
  chart_render "$enabled_out/kubernetes/apps/observability/gatus" "$gatus"

  # --- the scrape job that produces the aggregate -------------------------
  yq -r 'select(.kind == "ConfigMap") | .data["scrape.yml"] // ""' "$vm" >"$scratch/scrape-fd.yaml"
  grep -q 'job_name: cloudflare-tunnel' "$scratch/scrape-fd.yaml" \
    || die "the metric store does not scrape the frontdoor tunnel"
  # Derive the intended host from the ACTUAL platform render, not from a string
  # this check also asserts: the frontdoor app's config.json names the Argo
  # release, the pinned chart renders a Service of that name, and only then is
  # the scrape target compared against it. `helm template cf` would have
  # produced a Service called `cf` and "verified" nothing.
  local frontdoor_app="$enabled_out/kubernetes/apps/network/cloudflare-tunnel"
  [ -f "$frontdoor_app/config.json" ] \
    || die "the frontdoor application was not published"
  local release namespace chart_version expected_host
  release="$(jq -r '.appName' "$frontdoor_app/config.json")"
  namespace="$(jq -r '.namespace' "$frontdoor_app/config.json")"
  chart_version="$(jq -r '.chartVersion' "$frontdoor_app/config.json")"
  # The published values are SOPS-encrypted, so they are not readable here.
  # Render the chart from the PLAINTEXT source template instead -- the same
  # values, before encryption -- with the release name the ApplicationSet uses.
  local source_values="$REPO_ROOT/template/config/kubernetes/apps/network/cloudflare-tunnel/values.sops.yaml.j2"
  [ -f "$source_values" ] || die "the frontdoor values source template is missing"
  mise exec -- helm template "$release" \
    "oci://$(jq -r '.chartRepo' "$frontdoor_app/config.json")/$(jq -r '.chart' "$frontdoor_app/config.json")" \
    --version "$chart_version" --namespace "$namespace" \
    --values "$source_values" >"$scratch/frontdoor-chart.yaml" 2>"$scratch/frontdoor-chart.log" \
    || die "could not render the frontdoor chart: $(tail -3 "$scratch/frontdoor-chart.log")"
  local service port
  service="$(yq -r 'select(.kind == "Service") | .metadata.name' "$scratch/frontdoor-chart.yaml")"
  [ -n "$service" ] || die "the frontdoor chart rendered no Service"
  port="$(yq -r 'select(.kind == "Service") | .spec.ports[0].port' "$scratch/frontdoor-chart.yaml")"
  [ -n "$port" ] || die "the frontdoor Service exposes no port"
  # The rendered Service must be the one the release name implies; if the chart
  # ever renames it, this fails rather than silently matching a stale host.
  [ "$service" = "$release" ] \
    || die "the frontdoor Service is $service but the release is $release"
  expected_host="$service.$namespace.svc.cluster.local:$port"

  yq -e ".scrape_configs[] | select(.job_name == \"cloudflare-tunnel\")
    | .static_configs[].targets[] | select(. == \"$expected_host\")" \
    "$scratch/scrape-fd.yaml" >/dev/null \
    || die "the frontdoor scrape target is not the rendered Service host $expected_host"
  printf '%s\n' "$expected_host" >"$scratch/frontdoor-host.txt"
  # The tunnel identity must be dropped. Asserting that *some* labeldrop exists
  # is vacuous: an unrelated regex satisfies it. The rendered regex is applied
  # below to a CURATED census of label names, so an ineffective drop fails.
  #
  # Provenance of that census, stated precisely: the set below is checked in and
  # was measured once from `cloudflared 2026.10.7` -- the repository pin in
  # .mise/config.toml and in the frontdoor values. This block does NOT read the
  # binary or a live exposition at run time, so it cannot detect a future tunnel
  # release renaming a label. What it does enforce is the mutation that actually
  # shipped wrong once: the rendered regex must match the identity names and must
  # not match the status dimension.
  uv run --quiet --locked --no-dev python - "$scratch/scrape-fd.yaml" <<'PYLABELS'
import json
import re
import sys
import yaml

job = next(
    job for job in yaml.safe_load(open(sys.argv[1]))["scrape_configs"]
    if job["job_name"] == "cloudflare-tunnel"
)
drops = [entry for entry in job.get("metric_relabel_configs", [])
         if entry.get("action") == "labeldrop"]
assert len(drops) == 1, drops
pattern = drops[0]["regex"]

# Labels observed in the pinned binary's own exposition. The first group is
# identity (must be removed); the second is the status dimension the dashboard
# needs (must survive) plus the label names a previous, ineffective pattern
# named -- asserted absent from the real exposition so the census cannot be
# quietly replaced by that dead list.
# Measured from the pinned binary's exposition; curated here, not re-measured.
EMITTED_IDENTITY = {"userHostname", "connection_id", "edge_location", "rpcName"}
MUST_SURVIVE = {"status_code"}
NOT_EMITTED = {"connIndex", "ip", "hostname", "tunnel_id"}

rx = re.compile(f"(?:{pattern})")
for label in EMITTED_IDENTITY:
    assert rx.fullmatch(label), (
        f"the rendered labeldrop does not match {label}, which the pinned "
        f"tunnel emits: {pattern}")
for label in MUST_SURVIVE:
    assert not rx.fullmatch(label), (
        f"the rendered labeldrop would remove {label}, which a dashboard "
        f"panel needs: {pattern}")
for label in NOT_EMITTED:
    assert not rx.fullmatch(label), label
# A drop that names nothing (or the dead list) proves nothing.
assert any(rx.fullmatch(label) for label in EMITTED_IDENTITY), pattern
print(json.dumps({
    "regex": pattern,
    "dropped": sorted(EMITTED_IDENTITY),
    "survives": sorted(MUST_SURVIVE),
}, sort_keys=True))
PYLABELS
  # --- the dashboard only names metrics that scrape can produce -----------
  local names metric
  names="$(sed -n 's/^ *- *job_name: *\(.*\)$/\1/p' "$scratch/scrape-fd.yaml")"
  grep -qx 'cloudflare-tunnel' <<<"$names" \
    || die "the frontdoor scrape job is not visible to the dashboard check"
  local queries
  queries="$(yq -r '.data["frontdoor-access.json"]' \
    "$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml" \
    | jq -r '.rows[].panels[].expr[]')"
  [ -n "$queries" ] || die "the frontdoor dashboard has no queries"
  for metric in $(grep -oE 'cloudflared_[a-z_]+' <<<"$queries" | sort -u); do
    case "$metric" in
      cloudflared_tunnel_total_requests|cloudflared_tunnel_response_by_code|\
      cloudflared_tunnel_request_errors|cloudflared_tunnel_ha_connections|\
      cloudflared_tunnel_concurrent_requests_per_tunnel) ;;
      *) die "the frontdoor dashboard queries $metric, which the tunnel does not export" ;;
    esac
  done
  # The drop collapses the two identity-only gauges to an arbitrary child, so
  # their residual value is not a valid aggregate. That is only safe while no
  # panel reads them: this makes the dependency explicit instead of leaving it as
  # a claim in a comment. If a panel is ever added over either gauge, this fails
  # and the author must choose between deleting the series and keeping the label.
  for identity_gauge in cloudflared_tunnel_server_locations \
                        cloudflared_tunnel_user_hostnames_counts; do
    if grep -q "$identity_gauge" <<<"$queries"; then
      die "the frontdoor dashboard reads $identity_gauge, whose children the rendered labeldrop collapses to an arbitrary child; that residue is not a valid aggregate"
    fi
  done

  # No path, query or client address may appear in a frontdoor query: the
  # tunnel exports none, and asserting it here stops one being invented later.
  if grep -qE '/request|user_agent|client_ip|request_uri|hostname=' <<<"$queries"; then
    die "the frontdoor dashboard queries a per-request dimension"
  fi

  # --- probes: fixed targets, real success and failure conditions ---------
  config="$(yq -r 'select(.kind == "ConfigMap") | .data["config.yaml"] // ""' "$gatus")"
  [ -n "$config" ] || die "gatus rendered no configuration"
  printf '%s\n' "$config" >"$scratch/gatus-config.yaml"
  uv run --quiet --locked --no-dev python - \
    "$scratch/gatus-config.yaml" "$(cat "$scratch/frontdoor-host.txt")" <<'PYPROBE'
import sys
import yaml

config = yaml.safe_load(open(sys.argv[1]))
frontdoor_host = sys.argv[2]

endpoints = {endpoint["name"]: endpoint for endpoint in config["endpoints"]}
# A frontdoor probe exists, targets the tunnel metrics Service, and carries a
# readiness condition rather than only a status check.
tunnel = endpoints["cloudflare-tunnel"]
# Compared against the host derived from the platform render and the pinned
# chart, never against a literal this same file chose.
assert tunnel["url"] == f"http://{frontdoor_host}/ready", (tunnel["url"], frontdoor_host)
conditions = " ".join(tunnel["conditions"])
assert "[STATUS] == 200" in conditions, tunnel
assert "readyConnections > 0" in conditions, tunnel
assert tunnel["interval"] == "60s", tunnel
# Every endpoint is a fixed, operator-owned target; no caller-supplied URL.
for name, endpoint in endpoints.items():
    assert endpoint["url"].startswith("http://"), (name, endpoint["url"])
    assert "${" not in endpoint["url"], (name, endpoint["url"])
    assert "/" in endpoint["url"].split("://", 1)[1], (name, endpoint["url"])
# Failing behaviour is real: every endpoint declares a failure path with a
# bounded threshold and a resolve, so a fault is reported and then cleared.
for name, endpoint in endpoints.items():
    assert endpoint.get("alerts"), name
    for alert in endpoint["alerts"]:
        assert alert["failure-threshold"] >= 1, (name, alert)
        assert alert["success-threshold"] >= 1, (name, alert)
        assert alert["send-on-resolved"] is True, (name, alert)

# Provider selection is what actually routes an alert. Configuring
# `alerting.custom` and never naming it on an endpoint delivers NOTHING, so
# every endpoint must select the custom provider explicitly, with the same
# thresholds and resolve as its telegram sibling.
for name, endpoint in endpoints.items():
    types = [alert["type"] for alert in endpoint["alerts"]]
    assert "custom" in types, (name, types)
    custom_alert = next(a for a in endpoint["alerts"] if a["type"] == "custom")
    telegram_alert = next((a for a in endpoint["alerts"] if a["type"] == "telegram"), None)
    if telegram_alert is not None:
        for field in ("failure-threshold", "success-threshold", "send-on-resolved",
                      "description"):
            assert custom_alert[field] == telegram_alert[field], (name, field)
# At least one endpoint must select it; the notifier is not decorative.
assert any(a["type"] == "custom" for e in endpoints.values() for a in e["alerts"])

# --- notification delivery is configured, not merely referenced ----------
alerting = config["alerting"]
assert "telegram" in alerting, alerting
custom = alerting["custom"]
assert custom["method"] == "POST", custom
assert custom["url"] == "${GATUS_WEBHOOK_URL}", custom
assert custom["headers"]["Content-Type"] == "application/json", custom
body = " ".join(str(custom["body"]).split())
# Placeholders that this pinned Gatus release actually substitutes. A template
# naming a placeholder the binary does not know renders it literally, so the
# trigger and resolve payloads would be byte-identical -- asserting only that
# the text is present would pass on that defect. The set is checked exactly, and
# the runtime fixture proves the substituted values differ.
GATUS_PLACEHOLDERS = {
    "[ENDPOINT_NAME]", "[ENDPOINT_GROUP]", "[ALERT_DESCRIPTION]",
    "[ALERT_TRIGGERED_OR_RESOLVED]",
}
import re as _re
used = set(_re.findall(r"\[[A-Z][A-Z_]+\]", body))
assert used == GATUS_PLACEHOLDERS, (sorted(used), sorted(GATUS_PLACEHOLDERS))
# Unsupported placeholders observed in this project: [ALERT_TYPE] is emitted
# literally, and the RESULT_* family can carry the probed URL or body.
for forbidden in ("[ALERT_TYPE]", "[RESULT_ERRORS]", "[RESULT_BODY]",
                  "[RESULT_HTTP]", "[RESPONSE_BODY]", "[RESULT_DNS_RCODE]",
                  "[RESULT_CONDITIONS]", "[ENDPOINT_URL]"):
    assert forbidden not in body, (forbidden, body)
# The rendered body must parse as JSON once placeholders are neutralised, so a
# stray quote or trailing comma cannot turn delivery into a parse failure.
import json as _json
_json.loads(body.translate(str.maketrans("", "", "[]")).replace(
    "ALERT_TRIGGERED_OR_RESOLVED", "x").replace("ENDPOINT_NAME", "x").replace(
    "ENDPOINT_GROUP", "x").replace("ALERT_DESCRIPTION", "x"))
# No credential literal is rendered into the config.
for secret in ("TELEGRAM_BOT_TOKEN", "GATUS_WEBHOOK_URL"):
    assert f'"{secret}"' not in body, secret
print("[observability] frontdoor aggregate, probe targets and notification body are closed contracts")
PYPROBE

  # The optional webhook Secret is referenced, never required, so a cluster
  # without it still starts and simply sends nothing.
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].envFrom[]
    | select(.secretRef.name == "gatus-webhook") | .secretRef.optional == true' \
    "$gatus" >/dev/null \
    || die "the webhook Secret reference is required or absent"
  assert_no_public_exposure "$gatus" "gatus"
  log "frontdoor aggregates, probe success/failure and webhook delivery configured"
}

# ---------------------------------------------------------------- traces --

# A trace is a timed operation with an identity and a parent, not a log line
# carrying a correlation id. The rendered trace path must be its own exporter,
# its own store, and an attribute allowlist that drops rather than sanitizes.
check_traces() {
  local out="$scratch/victoria-traces.yaml" collector args
  chart_render "$enabled_out/kubernetes/apps/observability/victoria-traces" "$out"
  assert_no_public_exposure "$out" "the trace store"
  kubeconform -strict -summary "$out"

  yq -e 'select(.kind == "Deployment")' "$out" >/dev/null \
    || die "the trace store rendered no Deployment"
  args="$(yq -r 'select(.kind == "Deployment") | .spec.template.spec.containers[].args[]' "$out")"
  grep -qx -- '--retentionPeriod=7d' <<<"$args" \
    || die "the trace store does not run with an explicit 7d retention"
  grep -q -- '^--retention.maxDiskSpaceUsageBytes=' <<<"$args" \
    || die "the trace store does not cap the disk its retention may use"
  grep -q -- '^--memory.allowedBytes=' <<<"$args" \
    || die "the trace store does not bound its memory budget"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[] | .resources.limits.memory' "$out" >/dev/null \
    || die "the trace store container has no memory limit"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.securityContext.runAsNonRoot == true' "$out" >/dev/null \
    || die "the trace store pod does not require a non-root user"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].securityContext.readOnlyRootFilesystem == true' \
    "$out" >/dev/null \
    || die "the trace store container has a writable root filesystem"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.volumes[] | select(.emptyDir) | .emptyDir.sizeLimit' \
    "$out" >/dev/null \
    || die "the trace store scratch volume has no sizeLimit"
  if yq -e 'select(.kind == "PersistentVolumeClaim")' "$out" >/dev/null 2>&1; then
    die "the trace store provisions a PersistentVolumeClaim on its own"
  fi
  for kind in ClusterRole ClusterRoleBinding; do
    if yq -e "select(.kind == \"$kind\")" "$out" >/dev/null 2>&1; then
      die "the trace store renders a $kind, which an HTTP store does not need"
    fi
  done
  # The Service must expose the native OTLP ingest port the collector targets.
  local port
  port="$(yq -r 'select(.kind == "Service") | .spec.ports[] | select(.name == "http") | .port' "$out")"
  [ "$port" = 10428 ] || die "the trace store Service does not expose 10428 (got $port)"

  # The collector half: a dedicated trace exporter, and a trace pipeline that
  # never routes through the log exporter.
  collector="$(yq -r 'select(.kind == "ConfigMap") | .data.relay // ""' "$scratch/otel-events.yaml")"
  printf '%s\n' "$collector" >"$scratch/rendered-traces-collector.yaml"
  uv run --quiet --locked --no-dev python - "$scratch/rendered-traces-collector.yaml" <<'PYTRACES'
import sys
import yaml

config = yaml.safe_load(open(sys.argv[1]))
assert "traces" in config["service"]["pipelines"], list(config["service"]["pipelines"])
pipeline = config["service"]["pipelines"]["traces"]
assert pipeline["exporters"] == ["otlphttp/victoria-traces"], pipeline
# A span must not be written into the log store, and the log pipelines must not
# carry the trace exporter.
assert "otlphttp/victoria-logs" not in pipeline["exporters"], pipeline
for name, other in config["service"]["pipelines"].items():
    if name.startswith("logs/"):
        assert "otlphttp/victoria-traces" not in other["exporters"], (name, other)
exporter = config["exporters"]["otlphttp/victoria-traces"]
assert exporter["traces_endpoint"].endswith("/insert/opentelemetry/v1/traces"), exporter
assert "victoria-traces.observability.svc.cluster.local:10428" in exporter["traces_endpoint"], exporter
# The processors must run in the order that makes the allowlist meaningful:
# drop, then rebuild, then export.
assert pipeline["processors"] == [
    "memory_limiter", "filter/traces_privacy", "transform/traces_privacy"], pipeline

# Every retained surface is rebuilt from an explicit key set, and the surfaces
# that cannot be allowlisted safely are cleared outright.
shape = config["processors"]["transform/traces_privacy"]["trace_statements"]
span_group = next(group for group in shape if group["context"] == "span")
keep = next(s for s in span_group["statements"] if s.startswith("keep_keys(attributes, ["))
kept = set(keep.split("[", 1)[1].rsplit("]", 1)[0].replace('"', "").split(", "))
assert kept == {
    "http.request.method", "http.response.status_code", "http.route",
    "rpc.method", "error.type",
}, kept
assert 'set(status.message, "")' in span_group["statements"], span_group
event_group = next(group for group in shape if group["context"] == "spanevent")
assert "keep_keys(attributes, [])" in event_group["statements"], event_group
assert any(s.startswith("set(name, ") for s in event_group["statements"]), event_group
scope_group = next(group for group in shape if group["context"] == "scope")
assert "keep_keys(scope.attributes, [])" in scope_group["statements"], scope_group
assert any(s.startswith("set(scope.name,") for s in scope_group["statements"]), scope_group
resource_group = next(group for group in shape if group["context"] == "resource")
resource_keep = next(
    s for s in resource_group["statements"] if s.startswith("keep_keys(attributes, ["))
resource_kept = set(
    resource_keep.split("[", 1)[1].rsplit("]", 1)[0].replace('"', "").split(", "))
assert resource_kept == {
    "service.name", "k8s.namespace.name", "deployment.environment.name"}, resource_kept
# Nothing copies a caller map wholesale.
assert not any(s.startswith("set(attributes,") for s in span_group["statements"]
               + resource_group["statements"])

# The name contract accepts the shapes real instrumentations emit, and the
# allowed keys are value-checked too -- key filtering alone would store a
# credential placed in an allowed key.
filters = config["processors"]["filter/traces_privacy"]["traces"]["span"]
# The name is type-checked and value-checked, in two statements.
assert any("not IsString(name)" in s for s in filters), filters
name_filter = next(s for s in filters if "IsMatch(name" in s)
# Route templates (leading slash) and letter-initial operation names are the
# accepted shapes; free text is not.
assert name_filter.startswith("not IsMatch(name,"), name_filter
assert "/" in name_filter.split("^(", 1)[1][:4], name_filter
method_filter = next(s for s in filters if 'attributes["http.request.method"]' in s)
assert "GET|HEAD|POST|PUT|PATCH|DELETE" in method_filter, method_filter
status_filter = next(s for s in filters if 'attributes["http.response.status_code"]' in s)
assert "IsInt(" in status_filter and "< 100" in status_filter, status_filter
print("[observability] trace path: dedicated exporter, ordered drop-then-rebuild, closed attribute set")
PYTRACES
  log "trace store: 7d + disk caps, non-root, ClusterIP, no claim, no cluster RBAC; collector has a dedicated trace pipeline"
}

check_default_off
check_enabled_apps
check_metric_store
check_log_store
check_event_collector
check_status_dashboards
check_workload_metrics
check_wiring
check_frontdoor_probe_delivery
check_traces
if [ "${1:-}" = --runtime ]; then
  : "${OTELCOL_BIN:?set OTELCOL_BIN to the pinned 0.140.1 release binary}"
  : "${VICTORIA_METRICS_BIN:?set VICTORIA_METRICS_BIN to the pinned v1.155.0 release binary}"
  : "${VICTORIA_LOGS_BIN:?set VICTORIA_LOGS_BIN to the pinned v1.53.0 release binary}"
  yq -r '.data["nodes-workloads.json"]' "$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml" \
    | jq '[.rows[].panels[].expr[]]' >"$scratch/workload-queries.json"
  yq -r '.data["operational-logs.json"]' "$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml" \
    | jq '[.rows[].panels[].expr[]]' >"$scratch/log-queries.json"
  # Decode the chart output the earlier checks already rendered, so the fixture
  # asserts against the store's actual arguments, not the values file.
  [ -s "$scratch/victoria-logs.yaml" ] || die "the log store chart output was not captured"
  yq -o=json '.' "$scratch/victoria-logs.yaml" | jq -s '.' >"$scratch/victoria-logs.json"
  [ -s "$scratch/rendered-events-collector.yaml" ] \
    || die "the rendered event collector configuration was not captured"
  [ -s "$scratch/log-queries.json" ] || die "the operational log dashboard has no queries"
  # The frontdoor-access, probe and delivery paths need the same rendered
  # artifacts plus the pinned Gatus and cloudflared binaries.
  : "${GATUS_BIN:?set GATUS_BIN to the pinned Gatus v5.38.0 release binary}"
  # The trace slice needs the rendered collector and store documents plus the
  # pinned VictoriaTraces binary.
  : "${VICTORIA_TRACES_BIN:?set VICTORIA_TRACES_BIN to the pinned v0.13.0 release binary}"
  mise exec -- helm template victoria-traces \
    oci://ghcr.io/victoriametrics/helm-charts/victoria-traces-single \
    --version 0.1.13 --namespace observability \
    --values "$enabled_out/kubernetes/apps/observability/victoria-traces/values.yaml" \
    >"$scratch/victoria-traces.yaml" 2>>"$scratch/chart-pull.log" \
    || die "could not render the trace store chart for the runtime fixture"
  [ -s "$scratch/victoria-traces.yaml" ] || die "the trace store chart rendered nothing"
  yq -r 'select(.kind == "ConfigMap") | .data["config.yaml"] // ""' "$scratch/gatus.yaml" \
    >"$scratch/gatus-config-rendered.yaml" \
    || die "could not extract the rendered Gatus configuration"
  [ -s "$scratch/gatus-config-rendered.yaml" ] \
    || die "the rendered Gatus configuration is empty"
  # The fixture requires the rendered image tag AND digest to equal the reviewed
  # pin, then extracts the binary from that pinned OCI image and requires its
  # sha256 to equal the binary actually executed. A moved tag, a deleted digest
  # or a substituted binary therefore fails there -- a caller-supplied checksum
  # would prove none of that.
  cp "$enabled_out/kubernetes/apps/observability/gatus/values.yaml" \
    "$scratch/gatus-values.yaml"
  # The fixture compares the rendered probe host against the Service the pinned
  # chart actually renders under the Argo release name, so the runtime check
  # cannot pass on a self-consistent but wrong host.
  [ -s "$scratch/frontdoor-host.txt" ] \
    || die "the derived frontdoor host was not captured"
  # A scrape config the pinned store can parse strictly, pointing the frontdoor
  # job at the loopback exposition the fixture runs. Every other job is dropped:
  # the point is the frontdoor aggregate, not the whole platform.
  yq '.scrape_configs |= map(select(.job_name == "cloudflare-tunnel"))' \
    "$scratch/scrape-fd.yaml" >"$scratch/frontdoor-scrape.yml" \
    || die "could not build the frontdoor scrape config"
  [ -s "$scratch/frontdoor-scrape.yml" ] \
    || die "the frontdoor scrape config is empty"
  yq -r '.data["frontdoor-access.json"]' "$enabled_out/kubernetes/apps/observability/victoria-metrics/dashboards.yaml" \
    | jq '[.rows[].panels[].expr[]]' >"$scratch/frontdoor-queries.json"
  uv run --quiet --locked --no-dev python tests/ci/frontdoor-probe-delivery-runtime.py "$scratch"
  # Trace slice: workload span -> collector filter/shape -> VictoriaTraces ->
  # Jaeger query, with the privacy allowlist proved by hostile attributes.
  uv run --quiet --locked --no-dev python tests/ci/traces-runtime.py "$scratch"
  uv run --quiet --locked --no-dev python tests/ci/observability-runtime.py "$scratch"
  # The same rendered collector configuration, with only loopback endpoints
  # substituted, feeds the actual log store and is then queried.
  uv run --quiet --locked --no-dev python tests/ci/operational-logs-runtime.py "$scratch"
fi
log "observability contracts passed"
