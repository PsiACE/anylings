#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

./infra/bootstrap-tools.sh
mise="$HOME/.local/bin/mise"

sudo -n true 2>/dev/null || sudo -v
"$mise" exec -- uv run --locked --no-dev ansible-playbook -i localhost, infra/fedora/fedora.yml
"$mise" exec -- tofu -chdir=infra/k0s init
sudo -n true 2>/dev/null || sudo -v
"$mise" exec -- tofu -chdir=infra/k0s apply -auto-approve

kubectl() { sudo k0s kubectl "$@"; }
kubectl wait --for=create nodes --all --timeout=360s
kubectl wait --for=condition=Ready nodes --all --timeout=360s

crd=$(kubectl get crd applications.argoproj.io --ignore-not-found -o name)
seed=
if [[ -n "$crd" ]]; then
  seed=$(kubectl -n argocd get application argocd --ignore-not-found -o name)
fi
if [[ -z "$seed" ]]; then
  kubectl kustomize platform/argocd/install | kubectl apply --server-side -f -
  kubectl wait --for=condition=Established crd/applications.argoproj.io --timeout=60s
  kubectl -n argocd rollout status deployment/argocd-redis --timeout=180s
  kubectl -n argocd rollout status deployment/argocd-repo-server --timeout=180s
  kubectl apply -f platform/argocd/self.yaml
fi

kubectl -n argocd wait application/argocd \
  --for=jsonpath='{.status.sync.status}'=Synced --timeout=300s
kubectl -n argocd wait application/argocd \
  --for=jsonpath='{.status.health.status}'=Healthy --timeout=300s
