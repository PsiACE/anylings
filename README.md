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
- The Cloudflare root uses its official provider for Access, Tunnel, DNS and
  R2. Tunnel, routes, Access and DNS are baseline responsibilities, applied for
  every deployment without a toggle; R2 buckets are optional. Access precedes
  route activation and DNS; the cluster runs only the connector. R2
  retention/backups remain an explicit operational concern.

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
# Edit cluster.toml: repository URL, branch/prefix, tunnel token file and chart
# settings.
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
Secret). Argo self-management stays outside this generated workload set. The
`cloudflare-tunnel` connector is part of that baseline set, not an add-on.

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
(defaults) and `tests/config/repository-prefix.toml` (a Git subdirectory and a
custom Argo namespace). Both supply only a synthetic tunnel token file, because
the connector is baseline. They are configuration cases, not k0s/DOKS
implementations. Infrastructure validation checks the independent OpenTofu
configurations; the native integration run uses the same base input.

The workflow runs `tests/ci/run.sh` only on an explicitly disposable GitHub runner.
Each run creates a fresh private temporary directory; there is no resume/local
rehearsal interface or public CI command.

Git's upstream **git daemon** is the only Git fixture. The runner pushes encrypted
configuration into a local bare repository; Argo reads that one repository through
a read-only `git://` listener on the native node's internal address. Network pushes
are disabled. There is no Soft Serve deployment, SSH administration, GitHub
publication branch or repository-write token. Plaintext unauthenticated Git transport
is restricted to this disposable network; deployments should use HTTPS or trusted SSH.

CI checks the pushed revision and application health, actual decryption and HTTP,
PVC persistence/fresh-volume restore, Git-only Argo configuration change/restoration,
Git removal/recreation of echo through ApplicationSet, and no-change OpenTofu
repeats. Narrow safety checks cover confidentiality and failed reset/state retention. Cleanup resets the recorded cluster before deleting
its disposable volume data; only sanitized diagnostics are uploaded.

Single-host CI does not prove multi-host upgrades, HA, database-consistent recovery,
DOKS CSI recovery, Cloudflare Access propagation or R2 permissions. Its
credential-free runner has no Cloudflare account, so the connector is verified as
published and Synced from Git, not as edge-connected. Those require separate
authorization and environment-specific validation. No workstation cluster
installation or cloud apply is implied by rendering/static checks.

## Migration and review

Existing three-root installations require a reviewed ownership handoff, not a
blind apply: see [platform migration](infra/tofu/README.md#platform-migration).
The single entrypoint is `kubernetes/bootstrap.yaml`; its source is
`kubernetes/bootstrap/`, and the project root reads `kubernetes/projects/`.
The repository Secret remains `platform-repository`.
Remove the unused `cluster.storage_class` input; it never affected generated PVCs.
Remove `ingress.mode` from existing `cluster.toml` files: the Cloudflare Tunnel
connector is baseline, so only `ingress.tunnel_token_file` remains and configs
that still set a mode are rejected. Retain backend/state when renaming the former
`edge` root to `cloudflare`.

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
