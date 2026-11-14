# Operate the platform

Task recipes for one Anylings platform instance: install it, publish a change,
and tear it down. Every command is the repository's own `just` recipe or a
documented OpenTofu call; nothing here needs a bespoke script.

For the meaning of each input, use the [configuration
reference](../reference/configuration.md). For why ownership is split this way,
read the [architecture](../explanation/architecture.md).

## Prepare a workstation

```bash
mise trust
mise install --locked
```

The repository pins its toolchain in `mise` and its Python dependencies in
`uv.lock`; `just` derives `ANYLINGS_CONFIG` from `cluster.toml` and
`ANYLINGS_OUTPUT_DIR` from `.local/rendered` unless you export them.

Point every path at a private location. A working set:

```bash
umask 077
export ANYLINGS_CONFIG="$PWD/.local/instance/cluster.toml"
export ANYLINGS_OUTPUT_DIR="$PWD/.local/instance/rendered"
export SOPS_AGE_KEY_FILE="$PWD/.local/instance/age.key"
```

`just init platform` copies the sample configuration and creates an age identity
only when one is missing; it never overwrites either. The render reads the
identity's public key from `SOPS_AGE_KEY_FILE` to encrypt every secret file, so
keep that private key outside Git and available wherever you render. A second
workstation needs the same identity to produce a matching render.

## Provision the cluster

Both backends use OpenTofu through `just`, with a prepared working root and
backend for that root. See [OpenTofu roots](../../infra/tofu/README.md) for how to
prepare one and for the state rules.

k0s reads a reviewed k0sctl configuration file; DOKS takes an explicit Kubernetes
release slug plus a fixed worker pool:

```bash
# k0s: export TF_VAR_config_path=/secure/path/k0s.yaml first
just plan k0s
just apply k0s

# or DOKS: export DIGITALOCEAN_TOKEN and TF_VAR_kubernetes_version first
just plan doks
just apply doks
```

For the k0s backend, optional node-local storage is a separate step
(`just apply k0s-storage` with `ANYLINGS_STORAGE_PATH`); it is deliberately not
the cluster default and is documented with the other inputs in the
[configuration reference](../reference/configuration.md#node-local-storage-k0s).

Then export the issued kubeconfig to a private file and select a context
explicitly — the platform root refuses to fall back to a current context:

```bash
export KUBECONFIG="/secure/path/instance.kubeconfig"
just export k0s > "$KUBECONFIG"          # or: just export doks
export ANYLINGS_CONTEXT="$(kubectl --kubeconfig "$KUBECONFIG" config current-context)"
just wait cluster
```

## Prepare the ingress credential

The Cloudflare connector is part of the baseline platform, so a render needs the
tunnel token as a **file**. Obtain it from the `tunnel_token` output of the
`cloudflare` root, which is sensitive, and write it to a private file outside
Git:

```bash
tofu -chdir="$ANYLINGS_TOFU_CLOUDFLARE_DIR" output -raw tunnel_token \
  > /secure/path/cloudflare-tunnel.token
chmod 600 /secure/path/cloudflare-tunnel.token
```

Point `ingress.tunnel_token_file` at that path. The renderer never invents a token
path, and a configuration without one is rejected.

## Render, publish and install

```bash
just init platform
# edit $ANYLINGS_CONFIG: repository URL, branch and optional prefix
just configure platform
just check platform
```

`configure` renders and encrypts; `check` verifies encryption and that every
application reference resolves inside the published tree. Review the generated
diff before publishing, then push **only** `$ANYLINGS_OUTPUT_DIR/kubernetes/` to
the configured repository, branch and prefix. Bootstrap inputs, the age identity
and any inspection manifests stay behind.

A render that fails partway leaves an output directory without its ownership
marker, and every later render and `just --yes reset platform` refuse it rather
than adopt or delete it. That refusal is the safety property, so finish the
missing prerequisite first (most often the token file), then remove only the
generated paths the render owns inside that directory — `bootstrap/`,
`kubernetes/` and `.sops.yaml` — and re-run `configure`. Never point the output
directory at a populated location, and do not delete a directory the renderer
did not create.

First installation only:

```bash
just bootstrap platform    # installs Argo; an existing Argo is rejected
just wait platform
just plan platform
just apply platform
```

`just wait platform` waits for the Argo CD component rollouts and for its
Application, AppProject and ApplicationSet CRDs to be established — not for the
bootstrap Application to sync, and not for every generated workload.
Applications appear asynchronously as the ApplicationSet discovers them. Do not
rerun bootstrap to upgrade.

## Change the platform

Normal changes are Git changes:

```bash
# edit $ANYLINGS_CONFIG
just configure platform
just check platform
# review, then publish kubernetes/ and let Argo reconcile
```

Adding a workload means adding its application directory with a `config.json`
that names the chart, values path and optional manifest path; see the reference
for the exact fields. Removing that directory deletes the generated Application
and its workloads on the next sync.

Two inputs need a deliberate migration instead of a re-render:

- an existing installation moving from the older projects/repositories/apps
  ownership to a single bootstrap Application;
- a deployment whose `cluster.toml` still sets fields the current revision no
  longer accepts — rendering fails with the field named, so edit the input
  before rendering.

Both are described in [OpenTofu roots](../../infra/tofu/README.md#platform-migration).

## Verify before you trust

```bash
bash tests/ci/local-gate.sh     # format, validate and test every root, render both inputs
```

Or run one root at a time, without a backend:

```bash
tofu fmt -check -recursive infra/tofu
for root in platform k0s doks cloudflare; do
  tofu -chdir="infra/tofu/$root" init -backend=false -lockfile=readonly
  tofu -chdir="infra/tofu/$root" validate
  tofu -chdir="infra/tofu/$root" test
done
```

These are offline: mocked or absent cloud providers, no cluster, no credential.
They prove the configuration renders and its contracts hold, not that a live
provider, Access propagation, CSI recovery or multi-host upgrade works. Those
need an authorized environment of your own.

## Tear down

Order matters, and every step is destructive. Both cluster owners carry an
explicit `prevent_destroy` guard, so the last step is a **reviewed lifecycle
change**, not a plain command that quietly removes a cluster: OpenTofu refuses
the destroy until you have decided to remove that protection and recorded why.

1. Remove workload directories from Git and let Argo prune them.
2. `just destroy platform` deletes the bootstrap Application and cascades through
   the platform tree. Data goes according to each resource's own policy; no claim
   or volume this repository did not create is deleted, and none is backed up for
   you.
3. `just destroy cloudflare` removes tunnel routes, DNS records and Access
   applications. R2 buckets also carry `prevent_destroy`, so removing or replacing
   them needs the same kind of reviewed change.
4. The cluster itself. `just destroy k0s` or `just destroy doks` attempts the
   teardown, and the owner's guard blocks it on a permanent cluster: review the
   lifecycle change in the root, then apply it deliberately. k0s keeps its
   recorded hosts and configuration in state, so a reset still needs access to
   them.

Ordering is a precondition, not a convenience. Stop writers and confirm the
storage and access preconditions, including a retained-volume inspection before
any rebind, **before** removing the cluster that serves them. Destruction is not
a rollback: nothing here restores data, reverses a DNS or Access change, or
re-creates a deleted claim.

Keep the backend state, the age identity and the configuration until teardown
succeeds. A failed reset is an error, not a partially completed teardown: retry
it rather than deleting state.
