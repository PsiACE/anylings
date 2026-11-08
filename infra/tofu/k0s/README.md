# k0s lifecycle

All public operations go through OpenTofu, as for DOKS. This root follows the
[official k0s OpenTofu orchestration pattern](https://github.com/k0sproject/k0s/blob/main/hack/ostests/modules/k0sctl/main.tf):
`terraform_data` invokes the pinned k0sctl tool; `external` reads kubeconfig.
It does not import a third-party k0s provider or the upstream OS-test framework.

```text
cluster   stable lifecycle owner + recorded config + reset on destroy
   |
apply     config hash changes -> k0sctl apply (no destroy hook)
   |
kubeconfig -> sensitive kubeconfig + parsed context outputs
```

Configuration changes reapply without resetting the cluster. Plan tracks desired
inputs, not all out-of-band runtime drift. To deliberately reapply unchanged input,
use a reviewed `tofu apply -replace=terraform_data.apply`; do not replace the owner.

## Inputs

Prepare the private backend/working root described in the [parent guide](../README.md).
Copy `cluster.sample.yaml` to a private file and review its hosts:

```bash
export ANYLINGS_TOFU_K0S_DIR=/secure/path/k0s-work
export TF_VAR_config_path=/secure/path/k0s.yaml
just plan k0s
just apply k0s
```

The sample explicitly targets the invoking host with controller+worker/no taints,
not k0s's `single` shortcut. It modifies services and networking: run only on a
dedicated authorized Linux host. Remote profiles use upstream SSH/openSSH fields
and trusted host keys. Host provisioning is separate; no VM provider is assumed.

The full configuration and issued admin kubeconfig are in state. Keep state,
configuration and referenced credentials private and available for future operations.
Both backends then follow the shared export/readiness/bootstrap/platform flow in
the repository README. Optional local-path storage is not replicated or HA.

## Destruction and recovery

The stable owner has `prevent_destroy = true`. Destruction/replacement requires a
reviewed lifecycle change; CI overrides only this policy for its disposable host.
`just destroy k0s` passes `-refresh=false` so a broken kubeconfig read cannot prevent
reset. Direct OpenTofu recovery commands need the same flag. Plan/apply still refresh.

Reset uses the hosts/config already recorded in state: verify those targets before
teardown. It still needs host access; reset failures remain errors and retain the
owner for retry. CI deletes its external volume directory only after successful reset.
Keep resource blocks and state through successful teardown. Removing them is not
cluster removal, and partial apply is not transactional. Multi-host upgrades and
disaster recovery need separate validation; single-host CI is not proof of either.
