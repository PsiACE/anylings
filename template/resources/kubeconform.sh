#!/usr/bin/env bash
set -euo pipefail
kubernetes_dir="${1:?provide the rendered Kubernetes directory}"
args=(
  -strict -ignore-missing-schemas -skip Secret
  -schema-location default
  -schema-location 'https://k8s-schemas.home-operations.com/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
)

# `-ignore-missing-schemas` tolerates a schema that is absent (404). It does not
# tolerate a schema that could not be fetched: a schema-mirror blip is reported
# as `<kind> failed validation: failed downloading schema …`, exits non-zero and
# is therefore indistinguishable from a real manifest violation. That is exactly
# how a healthy tree failed this gate once already, with no way to tell the two
# apart afterwards.
#
# So retry the transport failure only. An invalid manifest carries no download
# error and still fails on the first attempt, so nothing here loosens the
# contract; a flaky mirror just stops being able to decide the result.
scratch="$(mktemp -d "${TMPDIR:-/tmp}/anylings-kubeconform.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

validate() {
  local target="$1" attempt output
  for attempt in 1 2 3; do
    if output="$(kubeconform "${args[@]}" "$target" 2>&1)"; then
      printf '%s\n' "$output"
      return 0
    fi
    if ! grep -q 'failed downloading schema' <<<"$output"; then
      printf '%s\n' "$output" >&2
      return 1
    fi
    printf 'schema mirror unavailable (attempt %s/3) for %s\n' "$attempt" "$target" >&2
    [ "$attempt" -lt 3 ] && sleep "$((attempt * 2))"
  done
  printf '%s\n' "$output" >&2
  return 1
}

validate "$kubernetes_dir/bootstrap.yaml"
while IFS= read -r -d '' file; do
  echo "Validating ${file%/kustomization.yaml}"
  # Rendered to a file first: a retry has to re-read the input, which a
  # consumed stdin pipe cannot do.
  kustomize build --enable-alpha-plugins --enable-exec "${file%/kustomization.yaml}" \
    >"$scratch/manifests.yaml"
  validate "$scratch/manifests.yaml"
done < <(find "$kubernetes_dir" -name kustomization.yaml -type f -print0)
