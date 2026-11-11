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
inputs enable it, `cloudflare-tunnel` accompany it.

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
  state and `/tmp` is scratch. `/tmp` is always an `emptyDir`; `/config` is an
  `emptyDir` by default (see below).

Because the gateway binds `0.0.0.0`, any caller outside the pod is remote, and
upstream answers a remote caller without an enabled gateway key with HTTP 401.
The deployed gateway is therefore closed to unauthenticated clients; mint a key
before publishing it.

### Operator inputs and prerequisites

1. **Persistent storage for `/config`.** By default `/config` is an `emptyDir`,
   so providers, gateway keys and sign-ins are lost when the pod is replaced.
   This is unchanged for anyone who has not chosen durable storage. To keep them
   across pod replacement, set one input:

    ```toml
    [magpie]
    config_claim = "magpie-config"
    ```

    That names an **existing** `PersistentVolumeClaim` in the application's own
    namespace (`default`). Prerequisites, all outside this repository:

    - Create the claim before syncing the Application, with the StorageClass and
      size you want. The platform mounts it and never creates it.
    - You own the claim and its data: this tree renders no
      `PersistentVolumeClaim` for it, so removing Magpie from Git removes the
      workload but leaves the claim. The claim and its volume still follow the
      cluster's own storage and reclaim policy, so back up what you need as you
      would any application data.
    - The pod runs as uid/gid `65532` and sets `fsGroup: 65532`, so `/config`
      must be writable by gid `65532`. Whether the driver enforces that group on
      mount is driver- and volume-dependent: check the CSI driver or volume
      configuration in use rather than assuming all of them do, and set matching
      ownership on a volume type that does not.

    Leaving `config_claim` empty keeps the ephemeral default.

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
does not cover vendor access, durable storage or public exposure. The
existing-claim rendering is covered; a pod replacement or re-mount on real CSI
storage is not tested, and moving data onto a claim does not by itself show that
a replaced volume keeps it.

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

The workflow runs `tests/ci/run.sh` only on an explicitly disposable GitHub runner.
Each run creates a fresh private temporary directory; there is no resume/local
rehearsal interface or public CI command.

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
