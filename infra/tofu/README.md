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
- **DOKS:** ordinary non-HA control plane, one fixed pool of two `s-2vcpu-4gb`
  workers by default, no autoscaling or automatic upgrade. `node_size` and
  `node_count` are the only pool inputs; the count accepts whole numbers from 1
  to 512. The version must be a full offered DOKS slug; region defaults to `sgp1`.
  DigitalOcean supplies default networking/CSI. There is no optional VPC factory,
  tag interface or autoscaling controller.
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

## DOKS worker profiles and cost

The two default workers are the baseline. A single-node economy profile is an
**explicit operator choice**, never a silent reduction:

```bash
# Baseline (unchanged): two s-2vcpu-4gb workers, non-HA, no autoscaling.
tofu -chdir=infra/tofu/doks plan -var kubernetes_version=1.36.3-do.0

# Economy profile: one s-2vcpu-4gb worker. Compute only.
tofu -chdir=infra/tofu/doks plan \
  -var kubernetes_version=1.36.3-do.0 \
  -var node_count=1
```

`node_size` and `node_count` carry the current fixed values as defaults, so
existing inputs render the same resource address and attributes; only the two
attributes now come from variables. `node_count` must be a whole number from 1
to 512.

The same inputs can live in a private tfvars file; the API token is never one of
them. It comes from `DIGITALOCEAN_TOKEN` in the environment.

```hcl
# Private file, never committed. Placeholders only.
kubernetes_version = "<full-doks-slug>" # for example 1.36.3-do.0
region             = "sgp1"
node_size          = "s-2vcpu-4gb"
node_count         = 1 # economy profile; omit for the two-worker baseline
```

### Verified prices (USD, DigitalOcean list, 2026-11-12)

| Item                                                   | Price                       |
| ------------------------------------------------------ | --------------------------- |
| DOKS control plane (non-HA, `ha = false`)              | $0, included                |
| HA control plane (`ha = true`)                         | $40.00/month                |
| One `s-2vcpu-4gb` worker (2 vCPU, 4 GiB)               | $24.00/month; $0.03571/hour |
| Its included resources: 80 GiB SSD, 4,000 GiB transfer | bundled                     |
| Two such workers (the default)                         | $48.00/month                |

**$24/month is the compute line item of the single-node profile, not a total.**
The control plane is free only while HA stays off; DOKS 1.36 and later enable HA
by default when `ha` is omitted, which is why this root keeps `ha = false`
explicit in the resource and in its offline test. A total monthly cost can also
include PVC-backed DigitalOcean Volumes ($0.10/GiB-month), R2 storage and
requests, load balancers if any are added later, pooled-bandwidth overage beyond
the node allowance ($0.01/GiB), and sales tax. None of those are part of the
$24 figure, and none are provisioned by this root.

The checked standard `s-2vcpu-4gb` list entry is $24/month; we have not verified a
cheaper like-for-like DOKS-eligible option for this shape. Other size families
exist, so this is not an enumeration of every SKU. DigitalOcean publishes one
global Droplet list and no per-region DOKS price table, so confirm eligibility and
pricing, and a one-node pool, in the DigitalOcean console for this region and
account; that check needs credentials and is outside this repository's offline
validation.

### What one node costs in behavior

- **Memory.** DOKS reserves memory for kubelet, containerd, kube-proxy, Cilium,
  CoreDNS, `do-node-agent`, `csi-do-node`, `konnectivity-agent`,
  `cpc-bridge-proxy` and the OS. On a 4 GiB node only **2.5 GiB is allocatable
  to Pods** (the same table gives 1 GiB on a 2 GiB node and 6 GiB on an 8 GiB
  node). Do not plan an application stack against the full 4 GiB, and do not
  claim the platform's workloads fit until they have been measured on the real
  cluster with `kubectl describe node` and observed usage.
- **Evictions.** With no second node, kubelet memory pressure evicts Pods on the
  only worker. Which Pods go first depends on QoS class, priority and how far
  requests are exceeded, so no fixed ordering is promised. An evicted singleton
  is unavailable until it reschedules on the same node, which can itself be under
  pressure.
- **No failure tolerance.** A node drain for maintenance or upgrade, or any node
  fault, stops all workloads: there is no other node to receive them.
- **Upgrade downtime.** `auto_upgrade = false` keeps upgrades manual, but a
  manual Kubernetes upgrade still replaces the only node. Expect a workload
  outage for the duration.
- **Pod density is not the limit.** 110 Pods per node is the platform maximum;
  memory and the single point of failure bind first.

Changing `node_size` on an existing cluster is not an in-place resize. DigitalOcean
documents a node pool's size slug as immutable and its recommended path as a new
pool, a drain, then removal of the old one; this root keeps one pool by design, so
that sequence is a reviewed migration with temporary double cost and downtime,
not a variable change an ordinary apply can complete. `node_count` can change in
place, and `prevent_destroy` still blocks replacement or deletion of the tracked
cluster.

On-demand elastic CPU capacity is a separate future feature, not part of this
profile. Here `auto_scale` stays `false` and no autoscaler is installed; sizing is
the fixed `node_count` an operator chooses.

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
   Existing workload names (`echo`, `reloader`, optional `cloudflare-tunnel`) are
   retained to avoid rename-driven replacement. Resolve ownership conflicts
   explicitly; do not force over another controller's resources.
5. Remove obsolete Git paths only after the new owners reconcile successfully.

This migration has not been exercised against a live existing installation.
Never substitute `destroy platform` or a whole-state reset for the handoff.

## Validation

```bash
tofu fmt -check -recursive infra/tofu
tofu -chdir=infra/tofu/ROOT init -backend=false -lockfile=readonly
tofu -chdir=infra/tofu/ROOT validate
tofu -chdir=infra/tofu/ROOT test
```

The DOKS root's `tests/cluster_pool.tftest.hcl` is an offline contract over a
mocked provider: it pins the default pool shape and the explicit `ha = false`,
and it rejects a zero, negative or fractional count and an empty size slug. It
needs no token, makes no DigitalOcean API call, and writes no state.

CI validates all four roots, runs those offline contracts, and applies the normal
k0s/platform roots on a disposable host. It does not apply cloud roots. Locks
cover Linux/macOS on amd64/arm64.
