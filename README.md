> **Anylings** was forked from [ajaykumar4/cluster-template](https://github.com/ajaykumar4/cluster-template) at commit [`83e79b8bee6280fe36c7fae38b0718722f46335f`](https://github.com/ajaykumar4/cluster-template/commit/83e79b8bee6280fe36c7fae38b0718722f46335f) for refactoring and adaptation. The complete upstream history is preserved.

# Anylings

A maintained GitOps repository for native k0s and DigitalOcean Kubernetes. Both
cluster backends use OpenTofu; they share the same configuration, bootstrap and
Argo reconciliation path. CI exercises that path, not a separate platform mode.

## Ownership and flow

```text
OpenTofu k0s root              OpenTofu DOKS root
  internal k0sctl                official DigitalOcean provider
        |                                |
        +--- kubeconfig + context -------+
                         |
                  wait for cluster
                         |
              configure / encrypt / check
                         |
                  publish Kubernetes Git
                         |
                  first-install bootstrap
                         |
             OpenTofu bootstrap Application
                         |
          +--------------+----------------+
          |              |                |
     Git credentials   Argo itself   root -> projects/
                                           |
                               AppProject + ApplicationSet
                                           |
                                 apps/*/*/config.json
                                           |
                                 workload Applications
```

- Kubernetes providers own networking, DNS and storage capabilities.
- Bootstrap seeds age/Git access and installs Argo/CRDs once, using the same chart
  settings as Argo self-management. No persistent Helm release competes with Argo.
- Following [Argo CD Autopilot](https://github.com/argoproj-labs/argocd-autopilot),
  OpenTofu seeds one bootstrap Application. It manages Argo itself, repository
  credentials and a projects root. The project combines an AppProject with a
  Git-file ApplicationSet; no external projects/repositories/apps orchestration.
- Initial Git access and the age key are seeded before reconciliation. The
  AppProject is applied before its ApplicationSet. Bootstrap health does not
  imply generated workload health; those reconcile asynchronously.
- The optional Cloudflare root uses its official provider for Access, Tunnel,
  DNS and R2. Access precedes route activation and DNS; the cluster runs only
  the connector. R2 retention/backups remain an explicit operational concern.

## Use

```bash
mise trust
mise install --locked
umask 077
export ANYLINGS_CONFIG="$PWD/.local/instance/cluster.toml"
export ANYLINGS_OUTPUT_DIR="$PWD/.local/instance/rendered"
export SOPS_AGE_KEY_FILE="$PWD/.local/instance/age.key"
```

Configuration migration: remove the unused `[cluster]` / `name` section from existing
`cluster.toml` files. Cluster provisioning names remain backend inputs, not platform settings.

Prepare the selected infrastructure root's private backend and inputs as described
in [infra/tofu](infra/tofu/README.md), then:

```bash
just plan k0s                 # or doks
just apply k0s                # or doks
export KUBECONFIG=/secure/path/instance.kubeconfig
just export k0s > "$KUBECONFIG"  # or doks
export ANYLINGS_CONTEXT="$(kubectl --kubeconfig "$KUBECONFIG" config current-context)"
just wait cluster

just init platform
# Edit cluster.toml: repository URL, branch/prefix, ingress and chart settings.
just configure platform
# Review and publish only rendered/kubernetes/ to the configured Git location.
just bootstrap platform      # first install only; existing Argo is rejected
just wait platform
just plan platform
just apply platform
```

Provide a StorageClass when applications need PVCs. DOKS supplies CSI; the native
profile optionally uses `just apply k0s-storage` with `ANYLINGS_STORAGE_PATH` set
to a dedicated node-local directory. Select storage classes in each application's
values/PVC, not a global platform knob. Local-path storage is not HA.

Normal application changes are configure/review/publish followed by Argo
reconciliation. Do not rerun bootstrap as an upgrade mechanism.

ApplicationSet discovers `kubernetes/apps/*/*/config.json` in the published Git
repository. Each config supplies the app name, namespace, chart coordinates,
values path and optional manifest path; it contains no credentials. The Git
source carries values only unless `manifestPath` is set (the tunnel's KSOPS
Secret). Argo self-management stays outside this generated workload set.

The applications in that set are the platform's real workloads. `magpie` is the
shared model gateway and the first of them; `reloader` and, where the configured
inputs enable it, `cloudflare-tunnel` and `observability` accompany it.

Deletion follows [Autopilot's application semantics](https://github.com/argoproj-labs/argocd-autopilot/blob/c102467016841f0532a6def064d618322156a7a7/cmd/commands/common.go):
Git removal deletes the generated Application and its workloads. Automated sync
uses prune, self-heal and allow-empty; ApplicationSet uses its default `sync`
policy and `preserveResourcesOnDeletion: false`. Bootstrap/root carry deletion
finalizers. Argo self-management alone has no deletion finalizer, as upstream
specifies; its normal sync still prunes. There is no retention mode or toggle.

**Deleting the bootstrap Application, including `just destroy platform`, is a
cascading platform teardown, not merely detaching GitOps.** It can delete workload
data according to resource/storage policies. Argo's installation is not cascaded
through its self-management Application, but its Git credential may be removed.
Keep recovery credentials and backups outside this tree. Only administrators may
write bootstrap/projects/app configs, which control deployment and deletion.

## Magpie

[Magpie](https://github.com/yetone/magpie) is the first real workload:
`kubernetes/apps/default/magpie/` in the rendered tree. It runs the upstream
container image rather than a local rebuild.

- Image `ghcr.io/yetone/magpie:0.1.3078`, pinned to its multi-arch index digest.
  Upstream releases several times a day, so tag and digest move together through
  dependency maintenance instead of tracking `latest`.
- Gateway on port `3425` (`MAGPIE_ADDR=0.0.0.0:3425`). Upstream's separate
  browser page on `3430` is not exposed.
- Probes use the image's own `magpie healthcheck`, which succeeds only while the
  gateway answers.
- Runs as uid/gid `65532` with a read-only root filesystem; `/config` holds all
  state and `/tmp` is scratch, both provided as `emptyDir` volumes.

Because the gateway binds `0.0.0.0`, any caller outside the pod is remote, and
upstream answers a remote caller without an enabled gateway key with HTTP 401.
The deployed gateway is therefore closed to unauthenticated clients; mint a key
before publishing it.

### Operator inputs and prerequisites

1. **Persistent storage for `/config`.** With `emptyDir`, providers, gateway
   keys and sign-ins are lost when the pod is replaced, so this instance is not
   a durable gateway yet. Choose a StorageClass for the cluster (see above) and
   switch the `config` entry to a `persistentVolumeClaim` sized for it.
2. **Provider credentials.** Add vendor credentials to the running instance's
   `/config`; they are not part of this repository. Do not put vendor API keys in
   `values.sops.yaml` or add a second secret-delivery path for them.
3. **Gateway keys.** Mint one per client with `magpie gateway-key add <name>`
   inside the pod and give it to that client as `Authorization: Bearer <key>`.
   Upstream generates the value, so it is never committed.
4. **Reaching the gateway.** The Service is cluster-internal. Publishing it
   beyond the cluster is an ingress decision made elsewhere, and any public route
   must keep gateway-key authentication on.

The rehearsal covers rendering, reconciliation and gateway-key enforcement. It
does not cover vendor access, durable storage or public exposure.

## Observability

Opt in with `[observability] enabled = true`. That publishes two applications
into the `observability` namespace; leaving it disabled publishes nothing, adds
no namespace and allows no extra chart registry.

- **victoria-metrics** — single-node VictoriaMetrics (`victoria-metrics-single`
  chart), the only metric store. It scrapes its own `/metrics` and Gatus, and
  serves queries and dashboards through vmui on `/vmui/`. No Prometheus operator,
  vmagent, Loki or database is deployed with it.
- **gatus** — probe-based status and alerting. Its dashboard is a status page;
  it also exports `gatus_results_*` metrics that VictoriaMetrics scrapes, so
  probe history that outlives the status page is read from metrics.

Read these as operational facts, not guarantees:

- **Retention is a target, not erasure.** `-retentionPeriod=7d` bounds what you
  can query. VictoriaMetrics prunes whole per-month partitions, and its docs
  state that background merges may never run for a month that stopped receiving
  data, so old bytes can outlive seven days on disk. Do not treat the store as a
  hard seven-day audit boundary. Audit retention belongs in a separate store with
  its own policy.
- **Gatus history is bounded memory, not a seven-day archive.** It runs with
  `storage.type: memory`, so its own results and events are capped by count and
  lost on restart. Longer probe history means querying the scraped metrics.
- **Nothing survives a reschedule by default.** VictoriaMetrics uses a bounded
  `emptyDir`, and no `PersistentVolumeClaim` is provisioned for either
  application, so no cloud volume is created or billed implicitly. To keep
  metrics across a pod replacement, create a claim yourself and point
  `server.persistentVolume.existingClaim` at it.
- **Access is in-cluster.** Both Services are `ClusterIP`; no Ingress, route,
  LoadBalancer or NodePort is rendered. Reaching vmui or Gatus from outside is a
  separate ingress decision, and it must keep authentication in front of them.

Alerting is off unless you provide credentials. Gatus reads
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from an optional Secret reference
named `gatus-telegram` in the `observability` namespace. Without that Secret the
pod still starts, and Gatus ignores the declared Telegram alerts because the
provider does not validate. Create the Secret with your normal secret-delivery
mechanism; no token is stored in this repository:

```bash
kubectl --namespace observability create secret generic gatus-telegram \
  --from-literal=TELEGRAM_BOT_TOKEN=... --from-literal=TELEGRAM_CHAT_ID=...
```

The rendered values do not carry resource sizing for your environment: the
committed limits (`768Mi` for the metric store, `128Mi` for Gatus) are a small
default, and ingestion is additionally capped by series and label budgets.
Dashboards live in the published `victoria-metrics-dashboards` ConfigMap and are
loaded by vmui, not by a separate dashboard server.

Web analytics and access-log analysis are not part of this slice. Aggregate
usage reporting would need its own store; until then, treat web analytics as
pending rather than inferring it from these metrics.

Other action-first commands:

| Command                     | Purpose                                                               |
| --------------------------- | --------------------------------------------------------------------- |
| `just check config`         | Validate input configuration                                          |
| `just check platform`       | Validate existing output, including complete encryption; no rewriting |
| `just render platform`      | Configure and render private first-install inspection manifests       |
| `just schema config`        | Regenerate the editor schema                                          |
| `just reset platform`       | Remove owned generated output, not cluster resources                  |
| `just destroy k0s` / `doks` | Explicit infrastructure teardown, subject to lifecycle protection     |

## Source and publication boundaries

- `template/`: shared configuration sources, validation and rendering.
- `infra/tofu/`: independent OpenTofu configurations for `k0s`, `doks`,
  `cloudflare` and `platform`, each with separate state. These are not four Argo roots.
- `infra/platform/`: shared readiness; `infra/k0s/`: optional native storage manifests.
- `tests/config/`: supported configuration inputs; `tests/ci/`: integration and safety checks.

Publish **only `$ANYLINGS_OUTPUT_DIR/kubernetes/`**, retaining the `kubernetes/`
directory beneath `repository.path`. Bootstrap inputs, age identities, kubeconfigs
and state never belong in that Git publication. Inspection manifests live in a
private sibling `bootstrap-manifests/` directory. Keep recovery keys outside Git
and the cluster; encrypt whole sensitive files, not just Secret data fields.

## CI

One platform renderer is exercised with two inputs: `tests/config/base.toml`
(defaults, no ingress) and `tests/config/cloudflare-tunnel.toml` (encrypted tunnel
credentials, a Git subdirectory and a custom Argo namespace). They are configuration
cases, not k0s/DOKS implementations. Infrastructure validation checks the independent
OpenTofu configurations; the native integration run uses the same base input.

Every pull request runs the offline gate: safety contracts, both platform render
inputs, and OpenTofu format/validate/test. Those checks take minutes, need no
credentials and no cluster.

The native rehearsal is heavier: it needs a disposable runner, passwordless
sudo for k0s and most of an hour, so it has its own workflow and never runs for
an ordinary pull request. Add the `ci/native-rehearsal` label to a pull request
from this repository, or push a new commit to one that already carries it, to
start it. No other label change starts or cancels it, and it shares no check
name with the gate, so its absence is never mistaken for a passed gate. A newer
commit supersedes an older rehearsal of the same pull request. Removing the
label cancels nothing already running. `tests/ci/run.sh` still refuses to start
without an explicitly disposable runner, so the label selects evidence
collection rather than relaxing the fixture's own authorization. The Landing
review needs only the offline gate. Diagnostics are ordinary operational data,
retained for seven days and uploaded best effort: a transport failure warns and
never turns a passing rehearsal red.

`bash tests/ci/local-gate.sh` runs the offline gate locally with the checkout's
own tools. It writes nothing to the repository and is safe on a workstation.

Both entry points call `tests/ci/check-render.sh` for the render assertions, so
the tunnel Secret-source/plaintext check, the rendered formatting check and the
partially-encrypted negative test cannot exist in one and be missing from the
other. It is still offline only: it renders and validates, but proves no runtime
behavior, so it does not stand in for a native run, DOKS CSI, Cloudflare or
multi-host checks.

`tests/ci/check-observability.sh` renders `tests/config/observability.toml` and
then renders the actually pinned charts from its output, so the optional slice is
checked against real artifacts rather than against the values file's intent. It
asserts that the default input publishes nothing, then on the enabled render:
explicit 7d retention, resource limits, non-root and read-only root filesystems,
ClusterIP-only Services with no route or LoadBalancer, no automatically
provisioned claim, no cluster-scoped metrics RBAC, an optional notification
Secret reference rather than embedded credentials, and that the vmui dashboard
flag, its mount, the ConfigMap, the scrape target and the probe Services all name
things that render actually creates, with no empty dashboard panel. The
contracts job and `tests/ci/local-gate.sh` both call it, before any native run
exists.

Git's upstream **git daemon** is the only Git fixture. The runner pushes encrypted
configuration into a local bare repository; Argo reads that one repository through
a read-only `git://` listener on the native node's internal address. Network pushes
are disabled. There is no Soft Serve deployment, SSH administration, GitHub
publication branch or repository-write token. Plaintext unauthenticated Git transport
is restricted to this disposable network; deployments should use HTTPS or trusted SSH.

The expected application set is read from the published `config.json` files, so
replacing a workload does not weaken the assertion. CI checks the pushed
revision, `Synced` for every Application and `Healthy` for the credential-free
ones, actual decryption, the magpie gateway refusing unkeyed callers from another
pod while serving a keyed one, PVC persistence and fresh-volume restore,
Git-only Argo configuration change and restoration, Git removal/recreation of
magpie through ApplicationSet, and no-change OpenTofu repeats. Narrow safety
checks cover confidentiality and failed reset/state retention. Cleanup resets the
recorded cluster before deleting its disposable volume data; only sanitized
diagnostics are uploaded.

Single-host CI does not prove multi-host upgrades, HA, database-consistent recovery,
DOKS CSI recovery, Cloudflare Access propagation or R2 permissions. Those require
separate authorization and environment-specific validation. No workstation cluster
installation or cloud apply is implied by rendering/static checks.

## Migration and review

Existing three-root installations require a reviewed ownership handoff, not a
blind apply: see [platform migration](infra/tofu/README.md#platform-migration).
The single entrypoint is `kubernetes/bootstrap.yaml`; its source is
`kubernetes/bootstrap/`, and the project root reads `kubernetes/projects/`.
The repository Secret remains `platform-repository`.
Remove the unused `cluster.storage_class` input; it never affected generated PVCs.
Retain backend/state when renaming the former `edge` root to `cloudflare`.

This is deliberately not a generic cloud module: DOKS keeps one fixed, non-HA
worker pool whose size and count are its only pool inputs, and Cloudflare does
not wrap WARP private routes or R2 retention rules. If older roots were already
applied, retain or migrate their VPC/private-route/bucket-lock resources before
applying the simplified configuration. Never discard state or accept unintended
data/policy deletion merely to complete a refactor.

[Landing](https://github.com/bubbuild/landing) provides advisory reviews and dependency
maintenance; native CI determines acceptance. Workflows were adapted from
[bubbuild/republic](https://github.com/bubbuild/republic/tree/2fa76b5407ba1cf035293121bfab9e782ea199f3/.github/workflows)
and intentionally track `bubbuild/landing@main`.

| Actions setting                    | Value                         |
| ---------------------------------- | ----------------------------- |
| `LANDING_API_KEY` secret           | DeepSeek official API key     |
| `LANDING_MODEL` variable           | `openai:deepseek-flash`       |
| `LANDING_API_BASE` variable        | `https://api.deepseek.com/v1` |
| `LANDING_CLIENT_ARGS` variable     | `{"api_format":"chat"}`       |
| `LANDING_COMPLETION_ARGS` variable | `{"reasoning_effort":"high"}` |

No local gateway is required. Fork PRs receive no Landing credentials. Leave
`LANDING_MODEL` unset to disable reviews. See [AGENTS.md](AGENTS.md) for maintenance
and automatic PR checks; agents must not manually dispatch or rerun CI.
Maintainers decide merges.
