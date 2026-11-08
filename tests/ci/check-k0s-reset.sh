#!/usr/bin/env bash
# Real public commands/OpenTofu, with only the host-execution boundary stubbed.
# No cluster, SSH connection or privileged operation is performed.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
fail() { cat "$scratch/command.log" >&2; echo "$*" >&2; exit 1; }
mkdir "$scratch/work" "$scratch/bin"
cp infra/tofu/k0s/*.tf infra/tofu/k0s/.terraform.lock.hcl "$scratch/work/"
cp infra/tofu/k0s/cluster.sample.yaml "$scratch/cluster.yaml"
cat >"$scratch/work/disposable_override.tf" <<'TF'
resource "terraform_data" "cluster" {
  lifecycle { prevent_destroy = false }
}
TF
cat >"$scratch/bin/k0sctl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
case "$1" in
  apply) ;;
  kubeconfig)
    [ ! -e "$RESET_PROBE/kubeconfig-unavailable" ] || exit 31
    printf 'apiVersion: v1\nkind: Config\ncurrent-context: synthetic\n'
    ;;
  reset)
    touch "$RESET_PROBE/reset-attempted"
    [ ! -e "$RESET_PROBE/reset-fails" ] || exit 32
    ;;
  *) exit 2 ;;
esac
STUB
chmod +x "$scratch/bin/k0sctl"
export PATH="$scratch/bin:$PATH" RESET_PROBE="$scratch"
export ANYLINGS_TOFU_K0S_DIR="$scratch/work" TF_DATA_DIR="$scratch/data"
export TF_VAR_config_path="$scratch/cluster.yaml"
export TF_CLI_ARGS_apply='-input=false -auto-approve -no-color'
export TF_CLI_ARGS_plan='-input=false -no-color'
export TF_CLI_ARGS_destroy='-input=false -auto-approve -no-color'
just apply k0s >"$scratch/command.log" 2>&1 || fail 'initial setup failed'
touch "$scratch/kubeconfig-unavailable"
for action in plan apply; do
  if just "$action" k0s >"$scratch/command.log" 2>&1; then
    fail "$action silently skipped its normal refresh"
  fi
done

# Reset must be attempted despite failed credential retrieval; a reset failure
# must still retain lifecycle state, allowing a subsequent successful retry.
touch "$scratch/reset-fails"
if just --yes destroy k0s >"$scratch/command.log" 2>&1; then
  fail 'failed reset reported success'
fi
[ -e "$scratch/reset-attempted" ] || fail 'destroy was blocked before reset'
[ -n "$(tofu -chdir="$scratch/work" state list)" ] || fail 'failed reset lost lifecycle state'
rm "$scratch/reset-fails" "$scratch/reset-attempted"
just --yes destroy k0s >"$scratch/command.log" 2>&1 || fail 'reset retry failed'
[ -e "$scratch/reset-attempted" ] || fail 'destroy did not reset hosts'
[ -z "$(tofu -chdir="$scratch/work" state list)" ] || fail 'successful reset left managed resources'
# Also cover partial creation: no kubeconfig was ever successfully recorded.
if just apply k0s >"$scratch/command.log" 2>&1; then
  fail 'initial credential failure reported a successful apply'
fi
rm "$scratch/reset-attempted"
just --yes destroy k0s >"$scratch/command.log" 2>&1 || fail 'partial-create cleanup failed'
[ -e "$scratch/reset-attempted" ] || fail 'partial-create cleanup did not reset hosts'
[ -z "$(tofu -chdir="$scratch/work" state list)" ] || fail 'partial-create cleanup left managed resources'
printf 'k0s reset survives unavailable kubeconfig and partial creation; failed reset retains state; plan/apply still refresh\n'
