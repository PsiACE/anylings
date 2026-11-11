#!/usr/bin/env bash
# Native integration fixtures for a disposable GitHub Actions runner only.
# No workstation lifecycle or local preparation interface is provided.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

umask 077
log() { printf '[integration] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }
[ "${GITHUB_ACTIONS:-}" = true ] && [ "${ANYLINGS_CI_DISPOSABLE:-}" = 1 ] \
  && [ -n "${RUNNER_TEMP:-}" ] || die "requires an explicitly disposable GitHub Actions runner"
[ "$#" -eq 0 ] || die "usage: bash tests/ci/run.sh (no arguments)"
STATE="$(mktemp -d "$RUNNER_TEMP/anylings-ci.XXXXXX")"
export KUBECONFIG="$STATE/kubeconfig" ANYLINGS_CONTEXT=""
export ANYLINGS_CONFIG="$STATE/cluster.toml" ANYLINGS_OUTPUT_DIR="$STATE/rendered"
export SOPS_AGE_KEY_FILE="$STATE/age.key"
export ANYLINGS_TOFU_K0S_DIR="$STATE/tofu/k0s"
export ANYLINGS_TOFU_PLATFORM_DIR="$STATE/tofu/platform"
export TF_CLI_ARGS_apply="-input=false -no-color -auto-approve"
export TF_CLI_ARGS_destroy="-input=false -no-color -auto-approve"
unset TF_DATA_DIR
K0S_WORK="$ANYLINGS_TOFU_K0S_DIR"
RENDERED="$ANYLINGS_OUTPUT_DIR"
GITWORK="$STATE/git"
GIT_PUSH_URL="$STATE/published.git"
GIT_SYNC_URL=""
DIAG="$STATE/diag"
STORAGE_PATH="$STATE/storage"
E2E_ARGOCD_NS="argo-system"
E2E_REVISION=""
SOPS_VALUE="anylings-ci-decrypted"
ECHO_PORT=18080
PIDS=()
mkdir "$DIAG"

kc() { kubectl --context "$ANYLINGS_CONTEXT" "$@"; }

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@" || die "root or passwordless sudo is required to prepare local storage"
  fi
}

# ---------------------------------------------------------------- diagnostics --

collect_diagnostics() {
  log "collecting sanitized diagnostics into $DIAG"
  mkdir -p "$DIAG"
  # Object metadata and pod logs only: no Secret content, no kubeconfig, and no
  # Application spec dumps that could echo cleartext values.
  {
    kc get nodes -o wide || true
    kc get pods --all-namespaces || true
    kc get events --all-namespaces --sort-by=.lastTimestamp | tail -40 || true
  } >"$DIAG/kubernetes.txt" 2>&1 || true
  # Application status only: no spec, no manifest, so no repository credential
  # or decrypted value can be captured.
  kc --namespace "$E2E_ARGOCD_NS" get applications \
    -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision' \
    >"$DIAG/applications.txt" 2>&1 || true
  kc --namespace "$E2E_ARGOCD_NS" logs statefulset/argocd-application-controller --tail=60 \
    >"$DIAG/application-controller.log" 2>&1 || true
  # Never collect repo-server logs: render diagnostics can contain plaintext.
  kc --namespace local-path-storage logs deployment/local-path-provisioner --tail=100 \
    >"$DIAG/local-path-provisioner.log" 2>&1 || true
}

teardown() {
  local rc=$?
  trap - EXIT
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  if [ "$rc" -ne 0 ]; then
    collect_diagnostics || true
  fi
  log "purging owned integration fixtures (disposable runner)"
  if ! cleanup; then
    log "owned cluster cleanup failed"
    rc=1
  fi
  exit "$rc"
}

# ------------------------------------------------------------------ cluster --

wait_http() {
  local url="$1" deadline=$((SECONDS + ${2:-180}))
  until curl --silent --fail --max-time 3 --output /dev/null "$url" 2>/dev/null; do
    (( SECONDS < deadline )) || die "timed out waiting for $url"
    sleep 2
  done
}

phase_cluster() {
  # Only the disposable environment and input profile differ. The same public
  # cluster lifecycle and readiness commands are used by operators.
  tofu_prepare k0s
  cp "$REPO_ROOT/infra/tofu/k0s/cluster.sample.yaml" "$STATE/k0sctl.yaml"
  jq -n --arg path "$STATE/k0sctl.yaml" '{config_path: $path}' >"$K0S_WORK/ci.auto.tfvars.json"
  # Explicit disposable-host deletion policy, not another cluster implementation.
  # Production roots protect against replacement/destruction by default.
  cat >"$K0S_WORK/disposable_override.tf" <<'TF'
resource "terraform_data" "cluster" {
  lifecycle { prevent_destroy = false }
}
TF
  just apply k0s
  just export k0s >"$KUBECONFIG"
  chmod 600 "$KUBECONFIG"
  ANYLINGS_CONTEXT="$(kubectl --kubeconfig "$KUBECONFIG" config current-context)"
  export ANYLINGS_CONTEXT
  just wait cluster
}

phase_storage() {
  export ANYLINGS_STORAGE_PATH="$STORAGE_PATH"
  just apply k0s-storage
}

phase_git() {
  # Git's upstream headless server exports only this bare repository, read-only.
  # Publishing uses the local Git transport; no SSH admin API or hosted branch.
  git init --bare --quiet --initial-branch main "$GIT_PUSH_URL"
  touch "$GIT_PUSH_URL/git-daemon-export-ok"
  local address deadline=$((SECONDS + 120))
  address="$(kc get nodes -o json | jq -r '[.items[0].status.addresses[] | select(.type == "InternalIP") | .address][0] // empty')"
  [ -n "$address" ] || die "native runner has no node-internal address"
  git daemon --reuseaddr --listen="$address" --port=9418 \
    --base-path="$STATE" --strict-paths --disable=receive-pack --forbid-override=receive-pack \
    "$GIT_PUSH_URL" >"$DIAG/git-daemon.log" 2>&1 &
  PIDS+=("$!")
  [[ "$address" != *:* ]] || address="[$address]"
  GIT_SYNC_URL="git://$address:9418/published.git"
  until git ls-remote "$GIT_SYNC_URL" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || die "Git daemon did not become ready"
    sleep 2
  done
}

phase_configure() {
  sed "s|^url = .*|url = \"$GIT_SYNC_URL\"|" \
    "$REPO_ROOT/tests/config/base.toml" >"$ANYLINGS_CONFIG"
  # The connector is baseline, so the rendered platform always needs a token
  # file. This is a synthetic value, never a credential.
  mkdir -p "$STATE/.local"
  printf 'synthetic-ci-tunnel-token\n' >"$STATE/.local/tunnel.token"
  just init platform
  just configure platform
  E2E_ARGOCD_NS="$(yq -r '.metadata.namespace' "$RENDERED/kubernetes/bootstrap.yaml")"
  local recipient
  recipient="$(age-keygen -y "$SOPS_AGE_KEY_FILE")"
  just init platform
  [ "$(age-keygen -y "$SOPS_AGE_KEY_FILE")" = "$recipient" ] \
    || die "repeated initialization rotated the age identity"
}

phase_publish() {
  log "publish the rendered repository to the test Git"
  mkdir -p "$GITWORK"
  # The repository root mirrors the rendered output root, because the roots
  # reference kubernetes/... paths. Only that subtree is published: bootstrap
  # material and private inspection manifests must never reach Git.
  cp -R "$RENDERED/kubernetes" "$GITWORK/kubernetes"

  # Exercise the real KSOPS generator path: one extra encrypted Secret goes
  # through the same ksops file list as the repository credential, so the
  # decryption proof is Argo CD's own plugin invocation.
  local target="$GITWORK/kubernetes/bootstrap/repositories"
  [ -d "$target" ] || die "rendered repository credential directory is missing: $target"
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: e2e-sops\n  namespace: %s\nstringData:\n  value: %s\n' \
    "$E2E_ARGOCD_NS" "$SOPS_VALUE" >"$STATE/e2e-sops.plain.yaml"
  chmod 600 "$STATE/e2e-sops.plain.yaml"
  sops --config "$RENDERED/.sops.yaml" \
    --input-type yaml --output-type yaml \
    encrypt --filename-override kubernetes/bootstrap/repositories/e2e-sops.sops.yaml \
    "$STATE/e2e-sops.plain.yaml" >"$target/e2e-sops.sops.yaml"
  rm -f "$STATE/e2e-sops.plain.yaml"
  yq --inplace '.files += ["./e2e-sops.sops.yaml"]' "$target/secret-generator.yaml"

  # Fail before publishing even though this fixture is synthetic.
  if grep -rqF "$SOPS_VALUE" "$GITWORK"; then
    die "the synthetic Secret value would be published in plaintext"
  fi
  git -C "$GITWORK" init --quiet --initial-branch main
  git -C "$GITWORK" add --all
  git -C "$GITWORK" -c user.name=e2e -c user.email=e2e@cluster.local \
    commit --quiet --message "rendered workspace"
  git -C "$GITWORK" push --quiet "$GIT_PUSH_URL" main
  if git -C "$GITWORK" push "$GIT_SYNC_URL" main >"$DIAG/git-write-check.log" 2>&1; then
    die "the Git fixture unexpectedly permits network pushes"
  fi
  E2E_REVISION="$(git -C "$GITWORK" rev-parse HEAD)"
  log "pushed revision $E2E_REVISION"
}

# Backend selection is test input. Both normal roots get private state and
# their own default plugin/backend data directory; TF_DATA_DIR is not shared.
tofu_prepare() {
  local root="$1" source="$REPO_ROOT/infra/tofu/$1" work="$STATE/tofu/$1"
  mkdir -p "$work"
  cp "$source"/*.tf "$source/.terraform.lock.hcl" "$work/"
  cat >"$work/backend_override.tf" <<EOF
terraform {
  backend "local" {
    path = "$STATE/tofu/$root.tfstate"
  }
}
EOF
}

# -------------------------------------------------------------- assertions --

assert_apps() {
  log "wait for Applications to be Synced+Healthy at the pushed revision"
  [ -n "$E2E_REVISION" ] || die "no pushed revision recorded"
  local deadline=$((SECONDS + 900))
  # One API snapshot: no per-object requests or duplicate existence passes.
  # Include the expected apps so an empty or partially created list cannot pass.
  # The Cloudflare Tunnel connector is baseline, but this runner has no
  # Cloudflare account, so cloudflared never reaches the edge: it must be
  # Synced from Git here, and only the credential-free apps must be Healthy.
  until kc --namespace "$E2E_ARGOCD_NS" get applications -o json | jq -e --arg rev "$E2E_REVISION" '
    (["bootstrap", "root", "argo", "echo", "reloader", "cloudflare-tunnel"]
      - [.items[].metadata.name] | length == 0) and
    all(.items[];
      .status.sync.status == "Synced" and
      ([.status.sync.revision, .status.sync.revisions[]?] | index($rev) != null)) and
    all(.items[];
      if .metadata.name == "cloudflare-tunnel" then true
      else .status.health.status == "Healthy" end)
  ' >/dev/null; do
    (( SECONDS < deadline )) || die "Applications did not converge on $E2E_REVISION"
    sleep 10
  done
}

# Only the presence and equality of the expected value are checked; the value
# is never printed, so a failure cannot leak decrypted content into CI logs.
assert_sops_secret() {
  log "assert the KSOPS-decrypted Secret reached the cluster"
  local deadline=$((SECONDS + 300)) value
  while :; do
    value="$(kc --namespace "$E2E_ARGOCD_NS" get secret e2e-sops \
      -o jsonpath='{.data.value}' 2>/dev/null | base64 --decode 2>/dev/null || true)"
    [ "$value" = "$SOPS_VALUE" ] && break
    (( SECONDS < deadline )) || die "secret e2e-sops was not decrypted by Argo CD (value differs or is missing)"
    sleep 10
  done
  log "SOPS Secret decrypted by Argo CD and matches the synthetic value"
}

# Exercise ApplicationSet's default sync/deletion policy through Git, not an
# imperative kubectl delete or a private-controller-state assertion.
assert_gitops_removal() {
  log "remove and restore echo through its Git application config"
  local relative="kubernetes/apps/default/echo/config.json"
  local saved="$STATE/echo-config.json" resources=()
  mapfile -t resources < <(kc --namespace default get deployments,services \
    -l app.kubernetes.io/name=echo -o name)
  [ "${#resources[@]}" -gt 0 ] || die "echo has no deployed resources"
  cp "$GITWORK/$relative" "$saved"
  git -C "$GITWORK" rm --quiet "$relative"
  git -C "$GITWORK" -c user.name=e2e -c user.email=e2e@cluster.local \
    commit --quiet --message "remove echo application config"
  git -C "$GITWORK" push --quiet "$GIT_PUSH_URL" main
  kc --namespace "$E2E_ARGOCD_NS" wait --for=delete application/echo --timeout=900s
  kc --namespace default wait --for=delete "${resources[@]}" --timeout=300s

  mv "$saved" "$GITWORK/$relative"
  git -C "$GITWORK" add "$relative"
  git -C "$GITWORK" -c user.name=e2e -c user.email=e2e@cluster.local \
    commit --quiet --message "restore echo application config"
  git -C "$GITWORK" push --quiet "$GIT_PUSH_URL" main
  E2E_REVISION="$(git -C "$GITWORK" rev-parse HEAD)"
  assert_apps
}

assert_echo_http() {
  log "assert the echo application answers over HTTP"
  local svc
  svc="$(kc --namespace default get service \
    -l app.kubernetes.io/name=echo -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$svc" ] || die "expected echo Service is missing"
  kc --namespace default port-forward --address=127.0.0.1 "service/$svc" "$ECHO_PORT:8080" \
    >"$DIAG/echo-port-forward.log" 2>&1 &
  PIDS+=("$!")
  wait_http "http://127.0.0.1:$ECHO_PORT/healthz" 180
  log "echo responded on http://127.0.0.1:$ECHO_PORT"
}

# Everything below lives in the rehearsal's own labeled objects in its own
# manifest, and is deleted by those exact names, so no unrelated workload is
# ever removed.
assert_storage_restore() {
  log "assert PVC restart and backup/restore to a fresh claim (local-path)"
  # This synthetic workload explicitly selects the provisioned native storage;
  # it is not evidence that a global platform setting reaches application PVCs.
  local manifest="$STATE/pvc-check.yaml"
  cat >"$manifest" <<EOF
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: e2e-data
  namespace: default
  labels:
    anylings.dev/owner: e2e
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources:
    requests:
      storage: 64Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: e2e-data
  namespace: default
  labels:
    anylings.dev/owner: e2e
spec:
  containers:
    - name: holder
      image: docker.io/library/busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
      command: ["sleep", "3600"]
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: e2e-data
EOF
  # Only this manifest, and only after confirming the objects are ours.
  clean_storage_check() {
    kc delete --filename "$manifest" --ignore-not-found --wait=true --timeout=180s >/dev/null
  }
  # A pre-existing pod or claim with this name but without our label is
  # somebody else's object and must not be deleted.
  local resource existing
  for resource in pod pvc; do
    existing="$(kc --namespace default get "$resource" e2e-data --ignore-not-found -o json)"
    if [ -n "$existing" ]; then
      [ "$(jq -r '.metadata.labels["anylings.dev/owner"]' <<<"$existing")" = e2e ] \
        || die "$resource/e2e-data exists and is not owned by the rehearsal"
    fi
  done
  clean_storage_check
  kc apply --filename "$manifest" >/dev/null

  local deadline=$((SECONDS + 300))
  until [ "$(kc --namespace default get pvc e2e-data -o jsonpath='{.status.phase}' 2>/dev/null)" = Bound ]; do
    (( SECONDS < deadline )) || die "PVC e2e-data did not bind with storage class local-path"
    sleep 5
  done
  kc --namespace default wait pod/e2e-data --for=condition=Ready --timeout=240s >/dev/null
  kc --namespace default exec e2e-data -- sh -ec 'echo persisted > /data/marker'

  # Same claim, new pod: only the persistent volume can carry the data over.
  kc --namespace default delete pod e2e-data --wait=true >/dev/null
  kc apply --filename "$manifest" >/dev/null
  kc --namespace default wait pod/e2e-data --for=condition=Ready --timeout=240s >/dev/null
  [ "$(kc --namespace default exec e2e-data -- cat /data/marker)" = persisted ] \
    || die "data did not survive the pod restart"
  # Back up bytes outside the cluster, then restore into a newly provisioned
  # claim. A pod restart alone is persistence, not a restore rehearsal.
  local old_uid backup="$STATE/pvc-backup.tar"
  old_uid="$(kc --namespace default get pvc e2e-data -o jsonpath='{.metadata.uid}')"
  kc --namespace default exec e2e-data -- tar -C /data -cf - marker >"$backup"
  clean_storage_check
  kc apply --filename "$manifest" >/dev/null
  kc --namespace default wait pod/e2e-data --for=condition=Ready --timeout=300s >/dev/null
  [ "$(kc --namespace default get pvc e2e-data -o jsonpath='{.metadata.uid}')" != "$old_uid" ] \
    || die "restore did not create a fresh claim"
  kc --namespace default exec e2e-data -- test ! -e /data/marker
  kc --namespace default exec -i e2e-data -- tar -C /data -xf - <"$backup"
  [ "$(kc --namespace default exec e2e-data -- cat /data/marker)" = persisted ] \
    || die "restored data differs"
  clean_storage_check
  rm -f "$backup"
  log "PVC restart and backup/restore to a fresh claim passed"
}

# Prove the bootstrap -> Argo handoff with a Git-only change to an existing
# bootstrap-installed field, then restore the original configuration. Neither
# bootstrap install nor OpenTofu apply participates in either transition.
assert_gitops_handoff() {
  log "assert Argo changes and restores its own configuration from Git alone"
  local relative="kubernetes/apps/argo-system/argo-cd/values.yaml"
  local values="$GITWORK/$relative" saved="$STATE/argo-values.before"
  local original expected transition changed=121s
  original="$(kc --namespace "$E2E_ARGOCD_NS" get configmap argocd-cm \
    -o jsonpath='{.data.timeout\.reconciliation}')"
  [ "$original" != "$changed" ] || changed=122s
  cp "$values" "$saved"
  for transition in change restore; do
    if [ "$transition" = change ]; then
      RECONCILIATION_INTERVAL="$changed" \
        yq -i '.configs.cm."timeout.reconciliation" = strenv(RECONCILIATION_INTERVAL)' "$values"
      expected="$changed"
    else
      cp "$saved" "$values"
      expected="$original"
    fi
    git -C "$GITWORK" add "$relative"
    git -C "$GITWORK" -c user.name=e2e -c user.email=e2e@cluster.local \
      commit --quiet --message "$transition Argo reconciliation interval"
    git -C "$GITWORK" push --quiet "$GIT_PUSH_URL" main
    E2E_REVISION="$(git -C "$GITWORK" rev-parse HEAD)"
    assert_apps
    [ "$(kc --namespace "$E2E_ARGOCD_NS" get configmap argocd-cm \
      -o jsonpath='{.data.timeout\.reconciliation}')" = "$expected" ] \
      || die "Argo did not $transition its bootstrap-installed configuration"
  done
  rm -f "$saved"
  log "Git-only Argo configuration change and restoration passed"
}

assert_repeatable() {
  log "assert reconfiguration, existing Argo readiness and root apply repeat safely"
  # Re-rendering regenerates SOPS data keys, so ciphertext differs; what must
  # hold is that configure succeeds again and nothing is left in plaintext.
  just configure platform
  # Argo already owns this installation; do not rerun first-install bootstrap.
  just wait platform

  # The CLI's documented detailed exit code reports pending changes without
  # depending on human-readable summaries or private state-file structure.
  for domain in k0s platform; do
    TF_CLI_ARGS_plan="-input=false -no-color -detailed-exitcode" just plan "$domain" >"$STATE/tofu-repeat.log" 2>&1 \
      || { cat "$STATE/tofu-repeat.log" >&2; die "$domain repeat operation left changes or could not plan"; }
    just apply "$domain" >>"$STATE/tofu-repeat.log" 2>&1 \
      || { cat "$STATE/tofu-repeat.log" >&2; die "$domain second apply failed"; }
  done
  rm -f "$STATE/tofu-repeat.log"

  # The revision is an invariant: same commit, still Synced+Healthy.
  assert_apps
}

# ------------------------------------------------------------ integration --

verify() {
  phase_cluster
  phase_storage
  phase_git
  phase_configure
  phase_publish
  tofu_prepare platform
  just bootstrap platform
  just wait platform
  just apply platform
  assert_apps
  assert_sops_secret
  assert_gitops_removal
  assert_echo_http
  assert_storage_restore
  assert_gitops_handoff
  assert_repeatable
  kc get nodes -o wide || true
  kc --namespace "$E2E_ARGOCD_NS" get applications || true
  log "rehearsal succeeded"
}

cleanup() {
  # The OpenTofu lifecycle owner resets its configured hosts. A failed reset MUST
  # prevent external volume-data deletion,
  # including when this function runs in the EXIT trap's conditional context.
  [ -f "$K0S_WORK/backend_override.tf" ] || return 0
  just --yes destroy k0s || return $?
  [ ! -L "$STORAGE_PATH" ] || die "storage path became a symlink"
  as_root rm -rf -- "$STORAGE_PATH"
}

trap teardown EXIT
verify
