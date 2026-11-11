#!/usr/bin/env bash
# Contract for what the pull-request gate is allowed to cost.
#
# The native rehearsal needs a disposable runner, root for k0s and most of the
# workflow's wall time, so it must stay opt-in: selected by a maintainer label,
# never a dependency of the ordinary gate and never a dependency of the
# advisory review. This asserts the workflow wiring only. It starts no runner,
# no cluster and no job, so it is safe on any host.
set -euo pipefail
cd "$(dirname "$0")/../.."

workflow=".github/workflows/ci.yaml"
gate=('contracts' 'render-platform' 'tofu-static')
fail() { printf '%s\n' "$*" >&2; exit 1; }

# yq reads the workflow as data: no YAML re-serialisation, no comment churn.
q() { yq -r "$1" "$workflow"; }

# The gate is every job a candidate must pass. Each one keeps the upstream
# repository authorization it had, costs a bounded amount, and writes nothing.
for job in "${gate[@]}"; do
  job_if="$(q ".jobs.\"$job\".if")"
  [ "$job_if" != null ] || fail "$job has no repository authorization"
  case "$job_if" in
    *"PsiACE/anylings"*) ;;
    *) fail "$job does not require the upstream repository" ;;
  esac
  # Label events only select the opt-in rehearsal; re-running the gate for them
  # would make labelling as expensive as the run it replaced.
  case "$job_if" in
    *labeled*) ;;
    *) fail "$job would re-run on a label-only event" ;;
  esac
  [ "$(q ".jobs.\"$job\".permissions | to_entries | map(.key == \"contents\" and .value == \"read\") | all")" = true ] \
    || fail "$job does not hold exactly contents: read"
  # Optional evidence only: a skipped gate job must be skipped, not a success.
  [ "$(q ".jobs.\"$job\".continue-on-error // false")" = false ] \
    || fail "$job tolerates its own failure"
done

# The rehearsal must be reachable only through the opt-in label on an
# in-repository pull request. It declares no needs, because a label event skips
# the gate jobs and a dependency on them would select nothing.
if [ "$(q '.jobs."native-e2e".needs')" != null ]; then
  fail "native-e2e still declares needs: $(q '.jobs."native-e2e".needs')"
fi
native_if="$(q '.jobs."native-e2e".if')"
case "$native_if" in
  *ci/native-rehearsal*) ;;
  *) fail "native-e2e is not gated on the ci/native-rehearsal opt-in label" ;;
esac
case "$native_if" in
  *"github.event_name == 'pull_request'"*) ;;
  *) fail "native-e2e is not restricted to pull requests" ;;
esac
case "$native_if" in
  *"head.repo.full_name == github.repository"*) ;;
  *) fail "native-e2e does not require an in-repository head" ;;
esac
[ "$(q '.jobs."native-e2e".permissions | keys | join(" ")')" = contents ] \
  || fail "native-e2e no longer holds only contents: read"

# The advisory review reports the gate it can observe; native evidence is
# separate and must not hold the review back.
read -r -a needs <<<"$(q '.jobs."landing-feedback".needs | join(" ")')"
[ "${#needs[@]}" -gt 0 ] || fail "landing-feedback declares no needs"
for job in "${needs[@]}"; do
  case " ${gate[*]} " in
    *" $job "*) ;;
    *) fail "landing-feedback depends on unexpected job: $job" ;;
  esac
done
for job in "${gate[@]}"; do
  case " ${needs[*]} " in
    *" $job "*) ;;
    *) fail "landing-feedback lost its gate dependency: $job" ;;
  esac
done

# The rehearsal itself must stay a real check inside the opt-in job: the test
# command keeps running and keeps deciding the job, so opting in never means
# weaker assertions.
rehearsal_step="$(yq -r '.jobs."native-e2e".steps[] | select(.run == "bash tests/ci/run.sh") | .name' "$workflow")"
[ "$rehearsal_step" = "Run native rehearsal" ] \
  || fail "native-e2e no longer runs tests/ci/run.sh as its own step"
[ "$(yq -r '.jobs."native-e2e".steps[] | select(.name == "Run native rehearsal") | .continue-on-error // false' "$workflow")" = false ] \
  || fail "native-e2e tolerates a rehearsal failure"

# Diagnostics are optional evidence: a transport failure must warn, not paint a
# passing rehearsal red, and must not be silently swallowed either.
[ "$(yq -r '.jobs."native-e2e".steps[] | select(.name == "Upload sanitized diagnostics") | .continue-on-error' "$workflow")" = true ] \
  || fail "diagnostics upload can still fail the native rehearsal"
[ "$(yq -r '.jobs."native-e2e".steps[] | select(.name == "Upload sanitized diagnostics") | has("if")' "$workflow")" = true ] \
  || fail "diagnostics upload runs only on success, so failures lose their evidence"
grep -q 'steps.diagnostics.outcome' "$workflow" \
  || fail "an unavailable diagnostics upload is not reported"

# The opt-in label only works if it exists: label-sync deletes anything the
# repository does not declare, so the selector must be a declared label.
grep -q '^- name: ci/native-rehearsal$' .github/labels.yaml \
  || fail "the ci/native-rehearsal label is not declared in .github/labels.yaml"

# The runner, not the job selector, owns CI authorization: tests/ci/run.sh
# still refuses to start without an explicitly disposable runner, which
# check-safety.sh exercises. A workflow change must not have added a bypass.
grep -q 'ANYLINGS_CI_DISPOSABLE:-' tests/ci/run.sh \
  || fail "tests/ci/run.sh no longer requires explicit disposable-runner authorization"

printf 'Native rehearsal is opt-in; the pull-request gate and Landing review do not depend on it\n'
