# OpenTofu roots

These are repository configurations, not general-purpose cloud modules. API
lifecycle belongs to established providers; retain only the required resource
shape, inputs and safety policy here.

| Root         | Implementation                                               | Required inputs                                           |
| ------------ | ------------------------------------------------------------ | --------------------------------------------------------- |
| `k0s`        | Official k0s OpenTofu orchestration pattern; internal k0sctl | `config_path`, explicit existing hosts                    |
| `doks`       | `digitalocean/digitalocean` provider                         | `kubernetes_version`, DigitalOcean token                  |
| `cloudflare` | `cloudflare/cloudflare` provider                             | Account, existing zone, explicit routes, Cloudflare token |
| `platform`   | Kubernetes provider; one bootstrap Application               | Rendered output, kubeconfig and context                   |

Provider versions and checksums are locked. `just plan/apply/destroy <root>` invokes
`run.sh`, which initializes the selected backend before the requested operation.
It does not silently auto-approve. `just export k0s|doks` prints only the sensitive
kubeconfig. Direct OpenTofu commands remain supported.

## State and execution

Source roots have no backend block; their default state is local. For real
infrastructure, prepare a **private, encrypted, backed-up remote backend with
locking**. Keep configuration and credentials until teardown succeeds.
`TF_DATA_DIR` moves plugin/backend metadata, not state.

Use `ANYLINGS_TOFU_K0S_DIR`, `ANYLINGS_TOFU_DOKS_DIR`,
`ANYLINGS_TOFU_CLOUDFLARE_DIR` or `ANYLINGS_TOFU_PLATFORM_DIR` to select a prepared
working root. For example, a disposable local backend can be prepared as follows;
production must substitute its actual backend declaration/configuration:

```bash
umask 077
work=/private/instance/platform
mkdir -p "$work"
cp infra/tofu/platform/*.tf infra/tofu/platform/.terraform.lock.hcl "$work/"
cat > "$work/backend_override.tf" <<EOF_BACKEND
terraform {
  backend "local" {
    path = "$work/platform.tfstate"
  }
}
EOF_BACKEND
export ANYLINGS_TOFU_PLATFORM_DIR="$work"
```

The platform adapter passes absolute `ANYLINGS_OUTPUT_DIR`, `KUBECONFIG` and
explicit `ANYLINGS_CONTEXT`. Publish the encrypted configuration and run the
shared bootstrap/readiness commands before its first plan. Other roots use
ordinary `TF_VAR_*`/tfvars inputs. Never use the same backend state for two roots.

## Backend-specific policy

- **k0s:** see [k0s/README.md](k0s/README.md). OpenTofu tracks desired inputs;
  this is not full runtime drift detection. Reset must not require live kubeconfig.
- **DOKS:** ordinary non-HA control plane, two fixed `s-2vcpu-4gb` workers,
  no autoscaling or automatic upgrade. The version must be a full offered DOKS
  slug; region defaults to `sgp1`. DigitalOcean supplies default networking/CSI.
  There is no optional VPC factory, tag interface or configurable node-pool module.
- **Cloudflare:** explicit HTTP(S) origins in the selected zone. Routes default
  to Access-protected and require allowed emails or existing group IDs. An
  intentionally public route must say `access = false`. Policies precede tunnel
  configuration, then DNS; no admin hostname or allow-everyone policy is invented.
  `r2_buckets` is a set of optional bucket names in `apac`, using provider defaults.
  WARP routes and R2 lock-rule configuration are not wrapped by this root.
- **Platform:** OpenTofu owns only `bootstrap`, sourced from
  `kubernetes/bootstrap.yaml`. Argo owns `argo` and `root`; the latter applies the
  project and its ApplicationSet. Apply waits for bootstrap sync/health, not all
  generated workloads. Deletion semantics match Autopilot: bootstrap/root carry
  cascading finalizers, generated Applications do not preserve their workloads,
  and all apps sync with prune/self-heal/allow-empty. Argo self-management alone
  omits the deletion finalizer. `just destroy platform` therefore tears down the
  managed platform tree; it is not a non-destructive detach operation.

DOKS/k0s clusters and R2 buckets have `prevent_destroy`; replacement/deletion needs
a reviewed lifecycle change. This does not protect resources if their blocks/state
are removed, and is not a backup. Review migration of any previously managed VPC,
private routes or bucket-lock policies before applying the reduced configuration.

Cloud tokens stay in environment variables (`DIGITALOCEAN_TOKEN`,
`CLOUDFLARE_API_TOKEN`). Tunnel credentials and kubeconfig outputs/state are sensitive.
Read the tunnel token into a private file referenced by platform configuration.
Live cloud behavior, Access propagation and data recovery are not proven by `validate`.

## Platform migration

Fresh installations use the normal bootstrap -> wait -> platform plan/apply flow.
For an existing `projects` / `repositories` / `apps` installation, do **not** apply
this root directly: deleting its old HCL resources otherwise schedules deletion.

1. Back up the private state and inventory existing Application finalizers and
   ownership. Publish the new tree, keeping old paths available during handoff.
   Keep the age identity, Git credentials, backend and context unchanged.
2. Pause automatic sync on the old root Applications, then explicitly remove only
   those three Application objects with **non-cascading deletion**. Do not delete
   their children, project, Secrets or workloads. Verify finalizers before removal.
3. Remove only the retired addresses `kubernetes_manifest.projects`,
   `kubernetes_manifest.repositories` and `kubernetes_manifest.apps` from the
   existing platform state (`tofu state rm` after the backup). This is a reviewed
   ownership handoff, not permission to discard infrastructure state.
4. Review the platform plan: only the new bootstrap Application should be added.
   Existing Argo must not be reinstalled. Apply and check `bootstrap`, `root`,
   `argo`, the `kubernetes` ApplicationSet and all generated workload Applications.
   Retained workload names avoid rename-driven replacement; `cloudflare-tunnel`
   is baseline where enabled. The retired `echo` sample has no replacement by
   that name: it is removed by Git deletion like any other application, and the
   real `magpie` workload is added separately. Resolve ownership conflicts
   explicitly; do not force over another controller's resources.
5. Remove obsolete Git paths only after the new owners reconcile successfully.

This migration has not been exercised against a live existing installation.
Never substitute `destroy platform` or a whole-state reset for the handoff.

## Validation

```bash
tofu fmt -check -recursive infra/tofu
tofu -chdir=infra/tofu/ROOT init -backend=false -lockfile=readonly
tofu -chdir=infra/tofu/ROOT validate
```

CI validates all four roots and applies the normal k0s/platform roots on a disposable
host. It does not apply cloud roots. Locks cover Linux/macOS on amd64/arm64.
