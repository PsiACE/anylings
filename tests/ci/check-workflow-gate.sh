#!/usr/bin/env bash
# Contract for where the native rehearsal is allowed to run.
#
# GitHub reports a skipped job as success under the same check name, so the
# heavy rehearsal cannot live in the gate workflow and be merely disabled there:
# a label-only run would publish a passing gate context without the code. It
# lives in its own workflow, which the gate never names and the gate's review
# never waits on.
#
# This asserts workflow wiring and evaluates the job condition for the known
# event shapes. It reads YAML, starts no runner and contacts no GitHub, so it is
# safe on any host.
set -euo pipefail
cd "$(dirname "$0")/../.."

ci=".github/workflows/ci.yaml"
native=".github/workflows/native-rehearsal.yaml"
gate=('contracts' 'render-platform' 'tofu-static')
fail() { printf '%s\n' "$*" >&2; exit 1; }
q() { yq -r "$1" "$2"; }

[ -f "$native" ] || fail "the native rehearsal has no workflow of its own"

# ---------------------------------------------------------------- the gate --

# No label trigger: a label event must not create a gate run whose jobs would
# report success for work they never did.
types="$(q '.on.pull_request.types | join(" ")' "$ci")"
case " $types " in
  *labeled*|*unlabeled*) fail "the gate workflow still subscribes to label events: $types" ;;
  ' ') fail "the gate workflow declares no pull_request types" ;;
esac

if [ "$(q '.on | has("workflow_dispatch")' "$ci")" = true ]; then
  fail "the gate workflow gained a manual dispatch trigger"
fi

# Every gate job keeps its authorization, its read-only permissions, its real
# result, and appears in exactly one workflow.
for job in "${gate[@]}"; do
  job_if="$(q ".jobs.\"$job\".if" "$ci")"
  [ "$job_if" != null ] || fail "$job has no repository authorization"
  case "$job_if" in
    *"PsiACE/anylings"*) ;;
    *) fail "$job does not require the upstream repository" ;;
  esac
  case "$job_if" in
    *labeled*) fail "$job is conditioned on a label event" ;;
  esac
  [ "$(q ".jobs.\"$job\".permissions | keys | join(\" \")" "$ci")" = contents ] \
    || fail "$job no longer holds only contents: read"
  [ "$(q ".jobs.\"$job\".continue-on-error // false" "$ci")" = false ] \
    || fail "$job tolerates its own failure"
  [ "$(q ".jobs | has(\"$job\")" "$native")" = false ] \
    || fail "$job is duplicated into the native workflow"
done

# The gate workflow holds the gate and its review, and may also hold other
# offline checks. What it must never hold is the heavy rehearsal: that job would
# report success whenever it is skipped. Additional offline jobs are allowed, so
# independent work can add its own bounded check without editing this contract.
for job in "${gate[@]}" landing-feedback; do
  [ "$(q ".jobs | has(\"$job\")" "$ci")" = true ] \
    || fail "the gate workflow lost its $job job"
done
while read -r job; do
  case " ${gate[*]} landing-feedback " in
    *" $job "*) continue ;;
  esac
  # A new job must stay offline: no rehearsal entrypoint, no nested workflow,
  # no sudo, no longer than the gate's own bound.
  if yq ".jobs.\"$job\".steps[]? | .run // \"\"" "$ci" | grep -q 'tests/ci/run.sh'; then
    fail "the gate workflow runs the native rehearsal in job: $job"
  fi
  if [ "$(q ".jobs.\"$job\".uses // \"\"" "$ci")" != "" ]; then
    fail "the gate workflow delegates to another workflow in job: $job"
  fi
  if yq ".jobs.\"$job\".steps[]? | .run // \"\"" "$ci" | grep -Eq '(^|[^[:alnum:]_])sudo '; then
    fail "the gate workflow holds a privileged job: $job"
  fi
  timeout="$(q ".jobs.\"$job\".timeout-minutes // 45" "$ci")"
  [ "$timeout" -le 45 ] || fail "the gate workflow holds a long job: $job ($timeout minutes)"
done < <(q '.jobs | keys | .[]' "$ci")

# The advisory review waits on the gate, plus any other offline check that
# exists in the workflow. It must not wait on the rehearsal, which is what made
# a label-only run able to hold or skip the review.
read -r -a needs <<<"$(q '.jobs."landing-feedback".needs | join(" ")' "$ci")"
for job in "${needs[@]}"; do
  [ "$(q ".jobs | has(\"$job\")" "$ci")" = true ] \
    || fail "landing-feedback depends on a job that does not exist: $job"
  if yq ".jobs.\"$job\".steps[]? | .run // \"\"" "$ci" | grep -q 'tests/ci/run.sh'; then
    fail "landing-feedback waits on the native rehearsal: $job"
  fi
done
for job in "${gate[@]}"; do
  case " ${needs[*]} " in
    *" $job "*) ;;
    *) fail "landing-feedback lost its gate dependency: $job" ;;
  esac
done

# ------------------------------------------------------------ the rehearsal --

# Exactly two event shapes reach the workflow: adding the opt-in label, and a
# new commit on a candidate that already carries it.
native_types="$(q '.on.pull_request.types | join(" ")' "$native")"
[ "$native_types" = "labeled synchronize" ] \
  || fail "the native workflow triggers on unexpected events: $native_types"

[ "$(q '.jobs | keys | join(" ")' "$native")" = native-e2e ] \
  || fail "the native workflow holds more than the rehearsal job"

job_if="$(q '.jobs."native-e2e".if' "$native")"
for fragment in \
  "github.repository == 'PsiACE/anylings'" \
  "head.repo.full_name == github.repository" \
  "contains(github.event.pull_request.labels.*.name, 'ci/native-rehearsal')" \
  "github.event.action == 'synchronize'" \
  "github.event.label.name == 'ci/native-rehearsal'"
do
  case "$job_if" in
    *"$fragment"*) ;;
    *) fail "the native condition lost: $fragment" ;;
  esac
done

# The two workflows must not publish a check under one another's names, or a
# skipped job would satisfy the other workflow's context.
for job in "${gate[@]}"; do
  [ "$(q ".jobs.\"$job\".name" "$ci")" != "$(q '.jobs."native-e2e".name' "$native")" ] \
    || fail "$job shares the rehearsal's check name"
done
[ "$(q '.jobs."native-e2e".name' "$native")" != "native-rehearsal (opt-in)" ] \
  || fail "the rehearsal still reports the label-selector name"

# The fixture, not the selector, owns CI authorization. The rehearsal keeps its
# own disposable-runner requirement, a bounded timeout and read-only contents.
grep -q 'ANYLINGS_CI_DISPOSABLE:-' tests/ci/run.sh \
  || fail "tests/ci/run.sh no longer requires explicit disposable-runner authorization"
[ "$(q '.jobs."native-e2e".permissions | keys | join(" ")' "$native")" = contents ] \
  || fail "the rehearsal no longer holds only contents: read"
[ "$(q '.jobs."native-e2e".timeout-minutes' "$native")" -le 45 ] \
  || fail "the rehearsal lost its bounded timeout"

# Diagnostics are ordinary operational data: short retention, best effort, and
# their transport never decides the check.
[ "$(q '.jobs."native-e2e".steps[] | select(.name == "Run native rehearsal") | .continue-on-error // false' "$native")" = false ] \
  || fail "the rehearsal tolerates its own failure"
[ "$(q '.jobs."native-e2e".steps[] | select(.name == "Upload sanitized diagnostics") | .continue-on-error' "$native")" = true ] \
  || fail "diagnostics upload can still fail the rehearsal"
[ "$(q '.jobs."native-e2e".steps[] | select(.name == "Upload sanitized diagnostics") | .with."retention-days"' "$native")" = 7 ] \
  || fail "native diagnostics are no longer retained for 7 days"
[ "$(q '.jobs."native-e2e".steps[] | select(.name == "Upload sanitized diagnostics") | .if' "$native")" = always\(\) ] \
  || fail "diagnostics upload runs only on success, so failures lose their evidence"
grep -q 'steps.diagnostics.outcome' "$native" \
  || fail "an unavailable diagnostics upload is not reported"

# One group per pull request: an unrelated label selects no job, so it cannot
# start or cancel rehearsal work, while a newer selected commit supersedes an
# older selected run of the same candidate.
group="$(q '.jobs."native-e2e".concurrency.group' "$native")"
case "$group" in
  *pull_request.number*) ;;
  *) fail "the rehearsal concurrency is not keyed by pull request" ;;
esac
case "$group" in
  *head.sha*|*event.action*)
    fail "unrelated events can cancel rehearsal work: $group" ;;
esac

grep -q '^- name: ci/native-rehearsal$' .github/labels.yaml \
  || fail "the ci/native-rehearsal label is not declared in .github/labels.yaml"

# ------------------------------------------------- known event outcomes --
#
# The condition was asserted above by fragment. Here the same decision is stated
# once as a readable rule and exercised for the cases that matter, so a change in
# intent has to change both places. This is not a general GitHub expression
# engine: it encodes exactly the two starts the workflow is allowed to have.
native_starts() {
  # action, changed label name, all label names, head repository owner
  local action="$1" label="$2" labels="$3" owner="$4"
  # An in-repository head, and the opt-in label present on the candidate.
  [ "$owner" = PsiACE ] || return 1
  case " $labels " in
    *" ci/native-rehearsal "*) ;;
    *) return 1 ;;
  esac
  # A new commit on an opted-in candidate.
  [ "$action" = synchronize ] && return 0
  # Adding the opt-in label itself, not any other label change.
  [ "$action" = labeled ] && [ "$label" = ci/native-rehearsal ]
}

expect_start() {
  local expected="$1" name="$2" action="$3" label="$4" labels="$5" owner="$6"
  if native_starts "$action" "$label" "$labels" "$owner"; then
    [ "$expected" = start ] || fail "case failed: $name started, expected no start"
  else
    [ "$expected" = skip ] || fail "case failed: $name did not start, expected start"
  fi
}

expect_start start "adding the opt-in label"        labeled     ci/native-rehearsal "ci/native-rehearsal" PsiACE
expect_start start "new commit with opt-in"         synchronize ""                  "ci/native-rehearsal" PsiACE
expect_start skip  "new commit without opt-in"      synchronize ""                  "type/patch"          PsiACE
expect_start skip  "unrelated label with opt-in"    labeled     type/minor          "ci/native-rehearsal type/minor" PsiACE
expect_start skip  "unrelated label without opt-in" labeled     type/minor          "type/minor"          PsiACE
expect_start skip  "opt-in label from a fork"       labeled     ci/native-rehearsal "ci/native-rehearsal" someone

# The evaluator above is only meaningful if it agrees with the workflow. Keep
# the two in step: the fragments the real condition uses are asserted, and this
# states the reference condition explicitly for readers.
printf 'Gate owns no label trigger; rehearsal starts only for the opt-in label or a new opted-in commit\n'
