# Configuration reference

Every input this template reads, its default and its enforced limits. Values come
from `template/scripts/validate.py` (which generates `cluster.schema.json`) and
from each OpenTofu root's `variables.tf`; a validation error names the offending
field and the accepted shape.

For the workflow that uses these inputs, see [operate the
platform](../how-to/operate.md).

## Environment

| Variable                                                                 | Required      | Meaning                                                                  |
| ------------------------------------------------------------------------ | ------------- | ------------------------------------------------------------------------ |
| `ANYLINGS_CONFIG`                                                        | no            | Path to `cluster.toml`; defaults to `<repo>/cluster.toml`                |
| `ANYLINGS_OUTPUT_DIR`                                                    | no            | Render destination; defaults to `<repo>/.local/rendered`                 |
| `SOPS_AGE_KEY_FILE`                                                      | yes           | Private age identity used to render and encrypt                          |
| `ANYLINGS_BOOTSTRAP_MANIFESTS_DIR`                                       | no            | Where first-install inspection manifests go; defaults beside the render  |
| `KUBECONFIG`, `ANYLINGS_CONTEXT`                                         | platform root | One explicit kubeconfig file and context; both are required              |
| `ANYLINGS_TOFU_K0S_DIR`, `_DOKS_DIR`, `_CLOUDFLARE_DIR`, `_PLATFORM_DIR` | no            | Prepared working roots; see [OpenTofu roots](../../infra/tofu/README.md) |

Cloud credentials are environment credentials of the official providers
(`DIGITALOCEAN_TOKEN`, `CLOUDFLARE_API_TOKEN`). No token, key or private path is
ever an input value in this repository.

## `cluster.toml`

Unknown sections and keys are rejected, so a stale input fails instead of being
ignored.

### `[repository]` — required

| Key           | Default | Rule                                                                                                      |
| ------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| `url`         | —       | `https://`, `http://`, `git://` or `ssh://git@` clone URL of the repository Argo reads                    |
| `branch`      | `main`  | Non-empty branch name                                                                                     |
| `path`        | `""`    | Relative directory prefix; rejects a leading `/` or `~`, backslashes, and empty, `.` or `..` segments     |
| `known_hosts` | `""`    | Required for `ssh://` hosts other than `github.com`, `gitlab.com`, `codeberg.org`, whose keys are bundled |

`ssh://` URLs also make `just init platform` create a deploy key beside the
configuration.

### `[argocd]` — optional

| Key             | Default       | Rule                                   |
| --------------- | ------------- | -------------------------------------- |
| `chart_version` | `10.14.0`     | Semantic version, optional leading `v` |
| `namespace`     | `argo-system` | DNS label; dots are not allowed        |

The chart version is the single source for both the first installation and Argo
self-management, so the two cannot drift.

### `[ingress]` — required

| Key                 | Default | Rule                                                                |
| ------------------- | ------- | ------------------------------------------------------------------- |
| `tunnel_token_file` | —       | Non-blank path to the tunnel token file, resolved against this file |

The connector is baseline; there is no mode switch. The token is read from the
file at render time and the rendered Secret is encrypted whole.

### `[magpie]` — optional

| Key            | Default | Rule                                                                                                                                    |
| -------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `config_claim` | `""`    | Empty keeps an `emptyDir`; otherwise an existing PVC name (lowercase DNS subdomain, at most 253 characters) in the `gateways` namespace |
| `seed_dir`     | `""`    | Empty renders no seed; otherwise a directory holding `providers.json` and `caller-keys.json`                                            |

Naming `seed_dir` renders the `magpie-seed` Secret, its KSOPS generator and an init
container that copies each file into `/config/magpie` **only when absent**, so a
restart never overwrites state the running gateway has since changed. This
template never creates the claim, so removing the application leaves the claim and
its data to you.

Both claim inputs (this one and `agents.storage_existing_claim`) reject a name
longer than 253 characters, and they differ on the label bound: the agent
runtime's input also rejects a dot-separated label longer than 63 characters,
while this one does not — so an over-long label passes the render and is only
rejected later by the API server. Keep every claim name within Kubernetes' own
limits: 253 characters overall with labels of at most 63.

### `[agents]` — optional, no default

Omit the section and no agent runtime is rendered.

| Key                      | Default               | Rule                                                                                                                                                     |
| ------------------------ | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `image_repository`       | —                     | Required. Registry path without tag or digest                                                                                                            |
| `image_tag`              | `bub0.6.1-t3web0.1.8` | ≤128 characters; the image carries two components, so the tag names both                                                                                 |
| `image_digest`           | `""`                  | Empty, or `sha256:` plus 64 lowercase hex; combined into `repository:tag@digest`                                                                         |
| `storage_existing_claim` | `""`                  | Empty keeps an `emptyDir`; otherwise an existing PVC name (lowercase DNS subdomain, at most 253 characters, each label at most 63) mounted at `/home/t3` |
| `gateway_secret`         | `""`                  | File holding a gateway key **already** recorded by Magpie's `caller-keys.json`                                                                           |

`gateway_secret` requires `magpie.seed_dir`. A key the gateway never learned only
produces 401s, so the pair is validated together.

There is no registry-credential input: pulling from a private registry is the
cluster's concern, and this template does not configure it. See the
published-image section below for the supply requirements.

### Optional slices: observability and SpinKube

| Key                     | Default | Effect when `enabled = true`                                                                                                                                |
| ----------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `observability.enabled` | `false` | Seven ClusterIP applications: VictoriaMetrics, Gatus, kube-state-metrics, per-node kubelet collectors, VictoriaLogs, one event collector and VictoriaTraces |
| `spinkube.enabled`      | `false` | cert-manager, the Spin operator with its CRDs, one executor and one sample app in `spinkube`                                                                |

Disabled publishes nothing, adds no namespace and allows no extra chart registry.

Observability bounds, as rendered:

| Component          | Retention / bound                    | Requests    | Limits       |
| ------------------ | ------------------------------------ | ----------- | ------------ |
| VictoriaMetrics    | `7d`, 2Gi `emptyDir`                 | 50m / 128Mi | — / 768Mi    |
| VictoriaLogs       | `3d` and 1GiB, 2Gi `emptyDir`        | 50m / 128Mi | 500m / 512Mi |
| kube-state-metrics | 1 replica, explicit metric allowlist | 20m / 48Mi  | 100m / 128Mi |
| kubelet collector  | 1 Pod per node, 30s interval         | 30m / 64Mi  | 200m / 160Mi |
| event collector    | 1 replica, Events + structured OTLP  | 30m / 96Mi  | 300m / 256Mi |
| VictoriaTraces     | `7d` and 1GiB, 2Gi `emptyDir`        | 25m / 96Mi  | 400m / 384Mi |
| Gatus              | in-memory history, capped by count   | 10m / 32Mi  | — / 128Mi    |

Retention and size are **query and storage targets**, not erasure guarantees, and
the `emptyDir` loses everything when its Pod is replaced. No
PersistentVolumeClaim is provisioned automatically. The metric store bounds each
scrape to 5,000 samples, the whole store to 10,000 hourly / 20,000 daily series
and 30 labels per series, and a single query to 30 seconds; it stops ingesting
when less than 256MiB of free disk remains. Log coverage is Kubernetes Events plus a
structured OTLP record; see [operational
logs](../observability/operational-logs.md) for the field contract rather than
assuming container or node log collection.

Traces are OTLP spans a workload sends; the slice stores and queries them, it
does not instrument a workload or generate spans. The trace store keeps `7d` and
1GiB in a bounded `emptyDir`, under the same query-bound, not-erasure, caveat.
Accepted fields, the cleared caller surfaces and the query API are in the
[request traces](../observability/traces.md) page.

### Optional alert delivery

Probes and alerts are rendered with the slice, but delivery is opt-in through
Secrets you create in the `observability` namespace. Neither is required: the
Pod starts without them.

| Secret           | Keys required                            | When absent                                                                                    |
| ---------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `gatus-telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Gatus starts, does not validate the Telegram provider, and its Telegram alerts deliver nothing |
| `gatus-webhook`  | `GATUS_WEBHOOK_URL`                      | The generic HTTP provider has no URL and sends nothing                                         |

Supply the values with your own secret tooling; this repository carries no
token, chat identifier or URL, and defining a provider alone does not deliver
anything — an alert also has to select it by type. Notification delivery against
a real endpoint is not verified here.

## Published runtime image

A published runtime image exists so a cluster can run the agent workload without
building anything at deploy time. The repository builds it and, on a normal
merge to the default branch, the workflow
(`.github/workflows/runtime-image.yaml`) publishes it and records the reference
a render names. The publisher builds **linux/amd64 only**: the multi-architecture
source pins exist for operator-built images, but the
workflow does not publish a multi-architecture manifest, so its tags must not be
deployed on arm64 nodes regardless of any pull-secret configuration — an image
pull secret authenticates a registry, it does not make an amd64 image run on
arm64. If your registry requires authentication, a freshly created package is
private until its registry settings are changed and until then a cluster needs
its pulls authorized. If anonymous pulls fail, the symptom is `ImagePullBackOff`
rather than a rendering error. This repository does not create image pull secrets
and defines no registry-credential input.

## Node-local storage (k0s)

The k0s backend optionally installs a local-path provisioner on the cluster with
`just apply k0s-storage`. It needs an explicit kubeconfig and context, and
`ANYLINGS_STORAGE_PATH` set to an absolute node-local directory.

Choosing a directory dedicated to this purpose is your responsibility, not
something the command classifies for you. Its guard is a fixed denylist: it
refuses the filesystem root, `/etc`, `/usr`, `/var`, `/home`, `/tmp`, and the k0s
and kubelet directories together with everything below them. Paths under the
other listed roots are **not** refused — for example a subdirectory of `/var` or
of a home directory is accepted — so pick a dedicated directory deliberately
rather than relying on that refusal to catch a mistake.

The provisioner installs a `local-path` StorageClass deliberately **not** made
the cluster default; a claim must name that class explicitly. Node-local storage
is a single-node feature: it is not replicated, not HA and not a replacement for
a StorageClass your CSI driver provides. Nothing in this repository creates a
claim for it; you choose the class in each application's values/PVC.

## Runtime authentication

Two workloads authenticate their own callers; this template does not disable
either check.

- **Gateway.** The process binds a non-loopback address, so every caller outside
  the Pod is remote and must present a gateway key as an `Authorization: Bearer`
  header. An unkeyed remote request is refused with HTTP 401. Mint keys with the
  gateway's own CLI inside the Pod, so the value is generated rather than chosen
  and never committed. A key you seed must already exist in the gateway's caller
  store, or the runtime that presents it only receives 401s.
- **Agent runtime.** The web server is a cluster-internal `ClusterIP` Service.
  A caller that is not the Pod's own loopback needs a pairing token or a bearer
  token, minted with the server's own CLI inside the Pod. Provider sign-in,
  model choice and vendor keys happen in the running instance and are never
  inputs here.

Publishing either workload beyond the cluster is an ingress decision made
elsewhere, and any public route must keep these checks in front of it.

## Published tree

Only `$ANYLINGS_OUTPUT_DIR/kubernetes/` is published, under `repository.path`.
The ApplicationSet discovers `kubernetes/apps/*/*/config.json`; each file supplies:

| Field          | Meaning                                                                                     |
| -------------- | ------------------------------------------------------------------------------------------- |
| `appName`      | Workload name                                                                               |
| `namespace`    | Destination namespace                                                                       |
| `chartRepo`    | Helm repository URL                                                                         |
| `chart`        | Chart name                                                                                  |
| `chartVersion` | Pinned chart version                                                                        |
| `valuesFile`   | Repository-relative values path, encrypted when it carries a secret                         |
| `manifestPath` | Optional repository-relative directory carrying a KSOPS-encrypted manifest; empty otherwise |

Every reference must resolve inside the published tree. Absolute paths,
backslashes, `.`/`..` segments, a wrong `repository.path` prefix, a missing
target and a symlink that leaves the subtree are all rejected by `just check
platform`, because Argo could not resolve them.

## Safety rules

- Secrets are whole-file SOPS-encrypted (`*.sops.yaml` under `bootstrap/` and
  `kubernetes/`). Do not scope encryption to `data`/`stringData`: a Helm values
  file is not a Secret and its other keys would stay plaintext.
- The age identity, deploy key, kubeconfig, inspection manifests and OpenTofu
  state stay outside the published tree, and outside Git.
- Every cluster operation takes an explicit kubeconfig and context; the platform
  root rejects empty values rather than using a default context.
- Bootstrap, root, project and application configuration control deployment and
  deletion. Treat write access to them as administrative.
- Provider credentials live in the running instance's `/config`, never in
  `values.sops.yaml`.
