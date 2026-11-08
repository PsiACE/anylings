#!/usr/bin/env bash
# Thin invocation adapter: backend configuration remains an operator input.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
case "${2:-}" in
  platform)
    dir="${ANYLINGS_TOFU_PLATFORM_DIR:-$ROOT/platform}"
    export TF_VAR_kubeconfig_path="${KUBECONFIG:?set one explicit kubeconfig file}"
    [ -f "$KUBECONFIG" ] || { echo "KUBECONFIG must name one existing file" >&2; exit 1; }
    export TF_VAR_context="${ANYLINGS_CONTEXT:?set an explicit context}"
    export TF_VAR_rendered_dir="${ANYLINGS_OUTPUT_DIR:?set the rendered output directory}"
    ;;
  k0s) dir="${ANYLINGS_TOFU_K0S_DIR:-$ROOT/k0s}" ;;
  doks) dir="${ANYLINGS_TOFU_DOKS_DIR:-$ROOT/doks}" ;;
  cloudflare) dir="${ANYLINGS_TOFU_CLOUDFLARE_DIR:-$ROOT/cloudflare}" ;;
  *) echo 'expected platform, k0s, doks or cloudflare' >&2; exit 2 ;;
esac
case "${1:-}" in
  export) exec tofu -chdir="$dir" output -raw kubeconfig ;;
  plan|apply|destroy) ;;
  *) echo 'expected plan, apply, destroy or export' >&2; exit 2 ;;
esac
tofu -chdir="$dir" init -input=false -lockfile=readonly -no-color
# Reset uses recorded host configuration and must work even when the live
# kubeconfig data source cannot be read. Keep refresh for every other operation.
if [ "$1:$2" = destroy:k0s ]; then
  exec tofu -chdir="$dir" destroy -refresh=false
fi
exec tofu -chdir="$dir" "$1"
