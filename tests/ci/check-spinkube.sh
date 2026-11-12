#!/usr/bin/env bash
# Focused contract checks for the optional SpinKube slice.
#
# Renders the real configuration inputs through the normal platform entry points,
# then renders the pinned Helm charts from the published values with
# `helm template`. Every assertion is about what those charts and manifests
# actually produce, so a chart upgrade or a template edit that changes a
# namespace, a Service type or a dependency fails here rather than on a cluster.
#
# No cluster, cloud API, credential or private key is used: the age identity is
# generated locally for this run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

log() { printf '[spinkube] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

for tool in helm jq yq just; do
  command -v "$tool" >/dev/null || die "$tool is required"
done

scratch="$(mktemp -d "${TMPDIR:-/tmp}/anylings-spinkube.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
chmod 700 "$scratch"

export SOPS_AGE_KEY_FILE="$scratch/age.key"

# A here-string comparison, not `command | grep -q`: grep closing its input
# early would send SIGPIPE and `pipefail` would report a passing match as a
# failed pipeline.
first_line() {
  local value="$1"
  printf '%s\n' "${value%%$'\n'*}"
}

# --------------------------------------------------------------- render cases --

render_case() {
  local name="$1" input="$2" out="$scratch/rendered-$1" config="$scratch/$1/cluster.toml"
  # `set -e` does not reach into a subshell whose failure value is then ignored,
  # and this function's output is captured with `$(...)`: a failed `just
  # configure platform` inside a subshell used to leave the caller holding a
  # successful-looking directory path. Every step is checked instead, and the
  # environment is passed per command so the caller's shell keeps no state from
  # a case that failed.
  mkdir -p "$out" "$scratch/$name" \
    || die "could not create the render directories for the $name case"
  cp "$input" "$config" \
    || die "could not copy the $name input to $config"
  ANYLINGS_CONFIG="$config" ANYLINGS_OUTPUT_DIR="$out" just init platform >/dev/null \
    || die "initializing the $name input failed"
  ANYLINGS_CONFIG="$config" ANYLINGS_OUTPUT_DIR="$out" just configure platform >/dev/null \
    || die "configuring the $name input failed"
  printf '%s\n' "$out"
}

log "render the default configuration (the slice must stay absent)"
default_out="$(render_case default tests/config/base.toml)"

log "render the configuration that enables the slice"
enabled_out="$(render_case spinkube tests/config/spinkube.toml)"

apps="$enabled_out/kubernetes/apps/spinkube"
project="$enabled_out/kubernetes/projects/kubernetes.yaml"

# ------------------------------------------------------------- default off --

check_default_off() {
  local rendered="$default_out/kubernetes/apps" project_default="$default_out/kubernetes/projects/kubernetes.yaml"
  local text published
  [ ! -e "$rendered/spinkube" ] \
    || die "the slice published an application directory while the input left it disabled"
  published="$(find "$rendered" -path '*/spinkube/*' -name 'config.json' -print -quit)"
  [ -z "$published" ] \
    || die "a spinkube config.json was published without being enabled: $published"
  # The vendored CRD is not a `.j2` template and cannot gate itself, so a
  # disabled slice must not publish it either. This is the exact regression the
  # path filter exists to prevent.
  published="$(find "$default_out/kubernetes" -name 'crds.yaml' -path '*spinkube*' -print -quit)"
  [ -z "$published" ] \
    || die "the vendored Spin CRDs were published while the slice is disabled: $published"
  text="$(cat "$project_default")"
  if grep -q 'namespace: spinkube' <<<"$text"; then
    die "the AppProject grants the spinkube namespace while the slice is disabled"
  fi
  for repo in ghcr.io/spinframework/charts quay.io/jetstack/charts; do
    if grep -qF "$repo" <<<"$text"; then
      die "the AppProject allows $repo while the slice is disabled"
    fi
  done
  if grep -q 'admissionregistration.k8s.io' <<<"$text"; then
    die "the AppProject grants admission webhook kinds while the slice is disabled"
  fi
  log "default-off holds: no applications, no vendored CRDs, no namespace or extra grant"
}

# ----------------------------------------------------------- enabled slice --

check_enabled_apps() {
  local app text
  for app in cert-manager spin-operator; do
    [ -f "$apps/$app/config.json" ] || die "$app was not published as an application"
    [ -f "$apps/$app/values.yaml" ] || die "$app has no rendered values file"
  done
  jq -e '.namespace == "spinkube"' "$apps/cert-manager/config.json" >/dev/null \
    || die "cert-manager is not destined for the spinkube namespace"
  jq -e '.namespace == "spinkube"' "$apps/spin-operator/config.json" >/dev/null \
    || die "the operator is not destined for the spinkube namespace"
  # Pinned coordinates, not floating tags: a silently moving chart is a silent
  # upgrade of a cluster-scoped controller.
  jq -e '.chart == "cert-manager" and .chartVersion == "v1.20.0"' \
    "$apps/cert-manager/config.json" >/dev/null \
    || die "the cert-manager prerequisite is not pinned to the reviewed version"
  jq -e '.chart == "spin-operator" and .chartVersion == "0.6.1"' \
    "$apps/spin-operator/config.json" >/dev/null \
    || die "the Spin operator is not pinned to the reviewed version"
  # The operator Application owns its own manifests; cert-manager's CRDs come
  # from its chart, so it must not carry a manifest path.
  jq -e '.manifestPath | length > 0' "$apps/spin-operator/config.json" >/dev/null \
    || die "the operator Application has no manifest path for its CRDs and executor"
  text="$(cat "$project")"
  for repo in ghcr.io/spinframework/charts quay.io/jetstack/charts; do
    grep -qF "$repo" <<<"$text" \
      || die "the AppProject does not allow the pinned chart registry $repo"
  done
  grep -q 'namespace: spinkube' <<<"$text" \
    || die "the AppProject does not allow the spinkube destination"
  for kind in MutatingWebhookConfiguration ValidatingWebhookConfiguration; do
    grep -q "kind: $kind" <<<"$text" \
      || die "the AppProject does not whitelist the cluster-scoped $kind"
  done
  log "enabled slice publishes two pinned applications and a scoped AppProject"
}

# --------------------------------------------------------------- manifest facts --

check_operator_manifests() {
  local dir="$apps/spin-operator" crds="$apps/spin-operator/crds.yaml"
  local executor="$dir/executor.yaml" sample="$dir/sample-spinapp.yaml"
  local kinds count name scope version wave

  [ -f "$crds" ] || die "the vendored CRDs were not published"
  # The release asset's own identity: two CRDs, both namespaced v1alpha1. A
  # vendored copy is only honest while it matches what upstream shipped.
  count="$(yq -N 'select(.kind != null) | select(.kind == "CustomResourceDefinition") | .metadata.name' "$crds" | wc -l)"
  [ "$count" -eq 2 ] || die "the vendored CRD file defines $count CRDs, not 2"
  for name in spinapps.core.spinkube.dev spinappexecutors.core.spinkube.dev; do
    yq -e "select(.kind == \"CustomResourceDefinition\" and .metadata.name == \"$name\")" "$crds" >/dev/null \
      || die "the vendored CRDs are missing $name"
    scope="$(yq -r "select(.kind == \"CustomResourceDefinition\" and .metadata.name == \"$name\") | .spec.scope" "$crds")"
    [ "$scope" = Namespaced ] || die "$name is $scope, so the per-namespace executor cannot work"
    version="$(yq -r "select(.kind == \"CustomResourceDefinition\" and .metadata.name == \"$name\") | .spec.versions[0].name" "$crds")"
    [ "$version" = v1alpha1 ] || die "$name no longer serves v1alpha1"
    wave="$(yq -r "select(.kind == \"CustomResourceDefinition\" and .metadata.name == \"$name\") | .metadata.annotations.\"argocd.argoproj.io/sync-wave\"" "$crds")"
    [ "$wave" = "-1" ] || die "$name is not applied before the executor and sample app (wave=$wave)"
  done

  # The executor must live where the app lives: the operator resolves it by the
  # SpinApp's namespace, so a control-plane namespace would silently leave the
  # sample app unscheduled.
  yq -e 'select(.kind == "SpinAppExecutor") | .metadata.namespace == "spinkube"' "$executor" >/dev/null \
    || die "the executor is not in the app namespace, so the app cannot find it"
  yq -e 'select(.kind == "SpinAppExecutor") | .spec.createDeployment == true' "$executor" >/dev/null \
    || die "the executor does not create a Deployment, so nothing runs the app"
  yq -e 'select(.kind == "SpinAppExecutor") | .spec.deploymentConfig.installDefaultCACerts == true' \
    "$executor" >/dev/null \
    || die "the executor does not install the default CA bundle the runtime needs"
  yq -e 'select(.kind == "SpinAppExecutor")
    | .spec.deploymentConfig.spinImage | test("^ghcr\\.io/fermyon/spin:v[0-9]+\\.[0-9]+\\.[0-9]+$")' \
    "$executor" >/dev/null \
    || die "the executor does not pin an immutable-style spin binary image tag"
  # The two fields are mutually exclusive in the CRD; a RuntimeClass here would
  # be a different execution model than the one this slice renders.
  if yq -e 'select(.kind == "SpinAppExecutor") | .spec.deploymentConfig.runtimeClassName' \
    "$executor" >/dev/null 2>&1; then
    die "the executor sets a runtimeClassName alongside spinImage"
  fi

  # The sample app is only useful if it names the executor that exists.
  local executor_name
  executor_name="$(yq -r 'select(.kind == "SpinAppExecutor") | .metadata.name' "$executor")"
  yq -e "select(.kind == \"SpinApp\") | .spec.executor == \"$executor_name\"" "$sample" >/dev/null \
    || die "the sample app does not reference the executor $executor_name"
  yq -e 'select(.kind == "SpinApp") | .metadata.namespace == "spinkube"' "$sample" >/dev/null \
    || die "the sample app is not in the executor's namespace"
  # A digest, not a tag: the app artifact must be the exact one the pinned
  # runtime was proven to decode and serve, and this check cannot repeat that
  # runtime proof offline, so it must at least refuse a reference that can move
  # under it. See the comment in the sample for the recorded run.
  yq -e 'select(.kind == "SpinApp")
    | .spec.image | test("@sha256:[0-9a-f]{64}$")' "$sample" >/dev/null \
    || die "the sample app image is not pinned to a digest"
  yq -e 'select(.kind == "SpinApp") | .spec.resources.limits.memory' "$sample" >/dev/null \
    || die "the sample app has no memory limit"

  # Nothing here may add a node-level runtime. This is checked structurally, not
  # by scanning text: the manifests explain in prose why no RuntimeClass is
  # involved, and a substring match would reject its own explanation.
  for file in "$executor" "$sample"; do
    if yq -e 'select(.kind == "RuntimeClass")' "$file" >/dev/null 2>&1; then
      die "$(basename "$file") declares a RuntimeClass"
    fi
  done
  if yq -e '... comments="" | select(.. | (.hostPath? // .hostNetwork? // .hostPID? // .privileged?) != null)' \
    "$executor" "$sample" >/dev/null 2>&1; then
    die "the operator manifests request a host-level capability"
  fi
  log "CRDs (2, namespaced, v1alpha1, wave -1), Spintainer executor and sample app agree"
}

# --------------------------------------------------------- chart rendering --

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

# A slice that installs a prerequisite controller must still not publish a
# route or a node-facing Service.
assert_no_public_exposure() {
  local out="$1" label="$2" kinds types
  kinds="$(yq -r 'select(.kind != null and .kind != "---") | .kind' "$out" | sort -u)"
  [ -n "$kinds" ] || die "$label rendered no resources"
  for forbidden in Ingress HTTPRoute Gateway LoadBalancer NodePort; do
    if grep -qx "$forbidden" <<<"$kinds"; then
      die "$label rendered a $forbidden resource"
    fi
  done
  # `-N` omits the `---` separators a multi-document render otherwise prints
  # between values, which would look like a non-ClusterIP entry.
  types="$(yq -N -r 'select(.kind == "Service") | .spec.type // "ClusterIP"' "$out")"
  if grep -qvx 'ClusterIP' <<<"$types"; then
    die "$label rendered a Service that is not ClusterIP"
  fi
  log "$label renders no Ingress, route, LoadBalancer or NodePort"
}

check_cert_manager() {
  local out="$scratch/cert-manager.yaml" count
  chart_render "$apps/cert-manager" "$out"

  # The CRDs it installs are the ones the operator's Issuer/Certificate use.
  for name in certificates.cert-manager.io issuers.cert-manager.io; do
    yq -e "select(.kind == \"CustomResourceDefinition\" and .metadata.name == \"$name\")" "$out" >/dev/null \
      || die "the cert-manager render does not install $name, so the operator CRs cannot apply"
  done
  count="$(yq -N 'select(.kind != null) | select(.kind == "Deployment") | .metadata.name' "$out" | wc -l)"
  [ "$count" -ge 3 ] || die "cert-manager rendered $count Deployments, not controller+webhook+cainjector"
  # No controller may be unbounded: this is a prerequisite, not the workload.
  local missing
  missing="$(yq -r 'select(.kind == "Deployment")
    | select(.spec.template.spec.containers[] | .resources.limits.memory == null)
    | .metadata.name' "$out")"
  [ -z "$missing" ] || die "cert-manager Deployments without a memory limit: $missing"
  # Every controller pod runs non-root; the chart sets it, and this asserts the
  # published values did not override it away.
  missing="$(yq -r 'select(.kind == "Deployment")
    | select(.spec.template.spec.securityContext.runAsNonRoot != true)
    | .metadata.name' "$out")"
  [ -z "$missing" ] || die "cert-manager Deployments that do not require a non-root user: $missing"
  # No app-owned claim: a claim here would silently provision a volume on a
  # cloud backend for a webhook prerequisite.
  if yq -e 'select(.kind == "PersistentVolumeClaim")' "$out" >/dev/null 2>&1; then
    die "cert-manager provisions a PersistentVolumeClaim"
  fi
  assert_no_public_exposure "$out" "cert-manager"
  log "cert-manager: CRDs, controller+webhook+cainjector, bounded, non-root, ClusterIP"
}

check_operator_chart() {
  local out="$scratch/spin-operator.yaml" wave cert_name issuer_name secret
  chart_render "$apps/spin-operator" "$out"

  # The chart leaves metadata.namespace to the Helm release namespace, so a
  # rendered object may omit it but must never contradict it: Argo installs this
  # Application into spinkube, and a hardcoded foreign namespace would land the
  # controller (or its webhook Service) somewhere the AppProject forbids.
  local stray
  stray="$(yq -N -r 'select(.kind != null and .kind != "ClusterRole" and .kind != "ClusterRoleBinding"
      and .kind != "CustomResourceDefinition" and .kind != "MutatingWebhookConfiguration"
      and .kind != "ValidatingWebhookConfiguration")
    | select(.metadata.namespace != null and .metadata.namespace != "spinkube")
    | .kind + "/" + .metadata.name' "$out")"
  [ -z "$stray" ] || die "the operator renders namespaced resources outside spinkube: $stray"
  yq -e 'select(.kind == "Deployment")' "$out" >/dev/null \
    || die "the operator rendered no Deployment"
  # Its webhook certificate is cert-manager's job; the reference is the whole
  # reason cert-manager is a prerequisite, so assert it is actually there.
  cert_name="$(yq -r 'select(.kind == "Certificate") | .metadata.name' "$out")"
  [ -n "$cert_name" ] || die "the operator render declares no webhook Certificate"
  issuer_name="$(yq -r 'select(.kind == "Issuer") | .metadata.name' "$out")"
  [ -n "$issuer_name" ] || die "the operator render declares no Issuer for its webhook certificate"
  yq -e "select(.kind == \"Certificate\") | .spec.issuerRef.name == \"$issuer_name\"" "$out" >/dev/null \
    || die "the webhook Certificate does not reference the rendered Issuer"
  secret="$(yq -r 'select(.kind == "Certificate") | .spec.secretName' "$out")"
  yq -e "select(.kind == \"Deployment\")
    | .spec.template.spec.volumes[] | select(.secret.secretName == \"$secret\")" "$out" >/dev/null \
    || die "the operator pod does not mount the certificate secret $secret"
  for kind in MutatingWebhookConfiguration ValidatingWebhookConfiguration; do
    yq -e "select(.kind == \"$kind\")" "$out" >/dev/null \
      || die "the operator rendered no $kind, so its CR validation is not wired"
    yq -e "select(.kind == \"$kind\")
      | .metadata.annotations.\"cert-manager.io/inject-ca-from\" | length > 0" "$out" >/dev/null \
      || die "$kind does not ask cert-manager to inject its CA bundle"
    # A webhook with a namespaceSelector that skipped spinkube would let invalid
    # SpinApps through; the chart's default selector excludes nothing by name.
    yq -e "select(.kind == \"$kind\") | .webhooks[].clientConfig.service.namespace == \"spinkube\"" \
      "$out" >/dev/null \
      || die "$kind points at a webhook Service outside spinkube"
  done
  # Bounded and non-root, like every other workload.
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].resources.limits.memory' "$out" >/dev/null \
    || die "the operator container has no memory limit"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.securityContext.runAsNonRoot == true' "$out" >/dev/null \
    || die "the operator pod does not require a non-root user"
  yq -e 'select(.kind == "Deployment")
    | .spec.template.spec.containers[].securityContext.capabilities.drop[]
    | select(. == "ALL")' "$out" >/dev/null \
    || die "the operator container does not drop all capabilities"
  if yq -e 'select(.kind == "PersistentVolumeClaim")' "$out" >/dev/null 2>&1; then
    die "the operator provisions a PersistentVolumeClaim"
  fi
  assert_no_public_exposure "$out" "the Spin operator"
  log "operator: cert-manager-issued webhook cert, both webhook configurations, bounded, ClusterIP"
}

check_default_off
check_enabled_apps
check_operator_manifests
check_cert_manager
check_operator_chart

# ------------------------------------------------------------ rejected input --

# The section is a closed switch, not a place to smuggle in a version override or
# a runtime class: an unknown key must fail validation rather than be ignored and
# leave the reader believing a pinned value changed.
check_rejected_input() {
  local config="$scratch/invalid.toml" case_name
  for case_name in 'enabled = "sometimes"' 'runtime_class = "wasmtime"' 'chart_version = "9.9.9"'; do
    printf '[repository]\nurl = "git://git.example.test/platform.git"\n[spinkube]\n%s\n' \
      "$case_name" >"$config"
    if ANYLINGS_CONFIG="$config" just check config >/dev/null 2>&1; then
      die "the config validator accepted [spinkube] $case_name"
    fi
  done
  # And the enabled flag itself must stay a boolean, so `[spinkube] enabled` with
  # no value (which TOML rejects) is not the only thing that fails.
  printf '[repository]\nurl = "git://git.example.test/platform.git"\n[spinkube]\nenabled = true\n' \
    >"$config"
  ANYLINGS_CONFIG="$config" just check config >/dev/null \
    || die "the config validator rejected a valid [spinkube] section"
  log "unknown or mistyped [spinkube] keys are rejected; the documented form is accepted"
}

check_rejected_input

# ------------------------------------------------- propagated render failure --

# The masked-failure regression: a render case whose preparation succeeds but
# whose `just configure platform` fails. That failure has to end the check, not
# print a directory and let the later assertions read a half-written tree. The
# input is a real validation error (`enabled` is not a boolean), so this
# exercises the same path an operator's typo would.
check_render_failure_propagates() {
  local bad="$scratch/failing-input.toml"
  printf '[repository]\nurl = "git://git.example.test/platform.git"\n[spinkube]\nenabled = "sometimes"\n' \
    >"$bad" || die "could not write the failing render input"
  if ( render_case failing "$bad" ) >/dev/null 2>&1; then
    die "a render case whose configure step failed still reported success"
  fi
  log "a failing render case fails the check instead of printing a directory"
}

check_render_failure_propagates

log "spinkube contracts passed"
