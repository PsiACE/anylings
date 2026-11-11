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

The applications in that set are the platform's real workloads, not samples.
`magpie` is the first of them, the shared model gateway; `reloader` and the
`cloudflare-tunnel` connector accompany it. Which set is published depends on
the configured inputs, so documentation names the workload rather than assuming
a fixed list.

## Magpie

[Magpie](https://github.com/yetone/magpie) is the first real workload:
`kubernetes/apps/default/magpie/` in the rendered tree. It is deployed as the
upstream container image, not rebuilt here.

- Image: `ghcr.io/yetone/magpie:0.1.3078`, pinned to its multi-arch index digest.
  The release satisfies the repository's three-day preference, and upstream
  publishes several releases a day, so tag and digest move together through
  dependency maintenance instead of tracking `latest`.
- Gateway port `3425` (upstream default, `MAGPIE_ADDR=0.0.0.0:3425`). Port
  `3430` is upstream's separate browser page and is not exposed here.
- Probes run the image's own `magpie healthcheck`, which exits 0 only while the
  gateway answers; no health endpoint was invented.
- The upstream image runs as uid/gid `65532`, keeps all state under `/config`,
  and works with a read-only root filesystem when `/config` and `/tmp` are
  writable, which the values provide as `emptyDir` volumes.

Binding the gateway to `0.0.0.0` makes every caller that is not the pod itself a
remote caller, and upstream refuses remote callers that do not present an enabled
gateway key with HTTP 401. That is why this workload must not be published
without a gateway key: provisioning one is an operator step below, so the
deployed gateway is closed to unauthenticated clients by default rather than open.

### Operator inputs and prerequisites

1. **Storage for `/config`.** The values use `emptyDir`, so providers, gateway
   keys and sign-ins do not survive pod replacement. Before treating this
   instance as the durable gateway, choose a StorageClass for the cluster (see
   above) and switch the `config` persistence entry to a `persistentVolumeClaim`
   sized for it. This is a deliberate, explicit decision; no class is guessed.
2. **Provider credentials.** Magpie reaches model vendors with credentials an
   administrator adds to its own `/config` volume. They are **not** part of this
   repository: do not put vendor API keys in `values.sops.yaml`, and do not add a
   second secret-delivery path for them. Add them to the running instance.
3. **Gateway keys.** Create one per client with
   `magpie gateway-key add <name>` inside the pod. Upstream mints the value; it
   must not be committed. Callers then send it as `Authorization: Bearer <key>`.
4. **Reaching the gateway.** The Service is cluster-internal. Exposing it beyond
   the cluster is an ingress decision owned by the ingress configuration and the
   Cloudflare root, and any public route must keep gateway-key authentication on.
   Ingress changes are not made by this workload definition.

These prerequisites are unexercised by CI: the rehearsal proves the rendered
Application reconciles and that the gateway answers and refuses unkeyed remote
callers. It does not prove vendor access, durable storage, or public exposure.

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

The expected workload set is derived from the published `config.json` files
rather than hardcoded, so replacing an application does not silently weaken the
assertion. CI checks the pushed revision, `Synced` for every Application and
`Healthy` for the credential-free ones, actual decryption, the magpie gateway
answering while refusing unkeyed remote callers, PVC persistence and
fresh-volume restore, Git-only Argo configuration change and restoration, Git
removal/recreation of magpie through ApplicationSet, and no-change OpenTofu
repeats. Narrow safety checks cover confidentiality and failed reset/state
retention. Cleanup resets the recorded cluster before deleting its disposable
volume data; only sanitized diagnostics are uploaded.

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

This is deliberately not a generic cloud module: DOKS has a fixed small pool;
Cloudflare does not wrap WARP private routes or R2 retention rules. If older roots
were already applied, retain or migrate their VPC/private-route/bucket-lock resources
before applying the simplified configuration. Never discard state or accept unintended
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
