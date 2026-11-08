#!/usr/bin/env bash
# Provider-independent readiness gate. Do not stop at NodeReady: API-to-kubelet
# connectivity and provider networking/DNS must work before platform bootstrap.
set -euo pipefail
: "${KUBECONFIG:?set one explicit kubeconfig file}" "${ANYLINGS_CONTEXT:?set an explicit context}"
[ -f "$KUBECONFIG" ] || { echo 'KUBECONFIG must name one existing file' >&2; exit 1; }
kc() { kubectl --context "$ANYLINGS_CONTEXT" "$@"; }
# A ready API can precede the first kubelet registration. kubectl wait does
# not wait for an empty resource set, so first wait for a registered node.
deadline=$((SECONDS + 600))
until nodes="$(kc get nodes -o name)" && [ -n "$nodes" ]; do
  (( SECONDS < deadline )) || { echo 'no node registered with the cluster API' >&2; exit 1; }
  sleep 2
done
kc wait nodes --all --for=condition=Ready --timeout=10m
resources="$(kc --namespace kube-system get daemonsets,deployments -o name)"
while IFS= read -r resource; do
  [ -z "$resource" ] || kc --namespace kube-system rollout status "$resource" --timeout=10m
done <<<"$resources"
nodes="$(kc get nodes -o name)"
while IFS= read -r node; do
  node="${node#node/}"
  deadline=$((SECONDS + 300))
  until kc get --raw="/api/v1/nodes/$node/proxy/healthz" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { echo "API-to-kubelet transport unavailable: $node" >&2; exit 1; }
    sleep 2
  done
done <<<"$nodes"
