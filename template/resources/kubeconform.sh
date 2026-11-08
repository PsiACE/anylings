#!/usr/bin/env bash
set -euo pipefail
kubernetes_dir="${1:?provide the rendered Kubernetes directory}"
args=(
  -strict -ignore-missing-schemas -skip Secret
  -schema-location default
  -schema-location 'https://k8s-schemas.home-operations.com/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
)
kubeconform "${args[@]}" "$kubernetes_dir/bootstrap.yaml"
while IFS= read -r -d '' file; do
  echo "Validating ${file%/kustomization.yaml}"
  kustomize build --enable-alpha-plugins --enable-exec "${file%/kustomization.yaml}" \
    | kubeconform "${args[@]}"
done < <(find "$kubernetes_dir" -name kustomization.yaml -type f -print0)
