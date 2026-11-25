#!/usr/bin/env bash
set -euo pipefail

for app in argocd glance; do
  "$@" -n argocd wait "application/$app" --for=create --timeout=180s
  "$@" -n argocd wait "application/$app" \
    --for=jsonpath='{.status.sync.status}'=Synced \
    --for=jsonpath='{.status.health.status}'=Healthy --timeout=180s
done

page=
for _ in {1..15}; do
  if page=$("$@" get --raw /api/v1/namespaces/glance/services/http:glance:http/proxy/api/pages/anylings/content/ 2>/dev/null); then
    break
  fi
  sleep 2
done
grep -Fq 'href="https://localhost:8081"' <<< "$page"
grep -Fq 'href="https://github.com/PsiACE/anylings"' <<< "$page"

printf 'Glance serves both bookmarks.\n'
