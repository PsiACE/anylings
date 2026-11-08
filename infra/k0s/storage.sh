#!/usr/bin/env bash
# Optional native-host storage capability, shared by operators and integration.
set -euo pipefail
: "${KUBECONFIG:?set one explicit kubeconfig file}" "${ANYLINGS_CONTEXT:?set an explicit context}"
[ -f "$KUBECONFIG" ] || { echo 'KUBECONFIG must name one existing file' >&2; exit 1; }
path="${ANYLINGS_STORAGE_PATH:?set the absolute node-local volume directory}"
[[ "$path" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'invalid storage path' >&2; exit 1; }
# Normalize dot segments without resolving symlinks on the operator's machine.
path="$(realpath --canonicalize-missing --no-symlinks -- "$path")"
case "$path" in
  /|/etc|/usr|/var|/home|/tmp|/var/lib/k0s|/var/lib/k0s/*|/var/lib/kubelet|/var/lib/kubelet/*)
    echo 'storage must use a dedicated directory outside system and kubelet roots' >&2; exit 1 ;;
esac
sed "s|\${ANYLINGS_STORAGE_PATH}|$path|g" "$(dirname "$0")/local-path.yaml.tmpl" \
  | kubectl --context "$ANYLINGS_CONTEXT" apply --server-side --filename -
kubectl --context "$ANYLINGS_CONTEXT" --namespace local-path-storage \
  rollout status deployment/local-path-provisioner --timeout=180s
