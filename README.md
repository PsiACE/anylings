> **Anylings** was forked from [ajaykumar4/cluster-template](https://github.com/ajaykumar4/cluster-template) at commit [`83e79b8bee6280fe36c7fae38b0718722f46335f`](https://github.com/ajaykumar4/cluster-template/commit/83e79b8bee6280fe36c7fae38b0718722f46335f) for refactoring and adaptation. The complete upstream history is preserved.

# Anylings

A GitOps platform template for a single Kubernetes cluster, provisioned with
OpenTofu on either native k0s or DigitalOcean Kubernetes. Both backends share
one configuration, one render, and one Argo CD reconciliation path, so the
platform behaves the same way whichever control plane you bring.

## What it supports

| Capability                | Default | Behaviour and limits                                                                                                                                          |
| ------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| k0s cluster               | —       | Hosts you name explicitly, applied through the official k0sctl orchestration pattern; a single controller is not HA                                           |
| DOKS cluster              | —       | Explicit release slug, one fixed non-HA worker pool; no autoscaling and no automatic upgrade                                                                  |
| Argo CD GitOps            | always  | One bootstrap Application owns Argo itself, a projects root and an ApplicationSet that discovers `apps/*/*/config.json`                                       |
| Cloudflare Tunnel         | always  | Outbound-only connector in the cluster; the tunnel, routes, DNS and Access applications belong to the `cloudflare` OpenTofu root                              |
| Magpie model gateway      | always  | One replica, gateway-key authentication, no vendor credential in Git; `/config` is ephemeral unless you name an existing claim                                |
| Bub agent runtime         | opt-in  | One replica with a browser surface, behind the server's own pairing or bearer tokens; you publish the image and choose the provider and model at runtime      |
| Self-hosted observability | opt-in  | Metrics, probe status, workload state, kubelet usage and operational logs as ClusterIP Services; no application or node log collection, no traces, no Grafana |
| SpinKube                  | opt-in  | cert-manager plus the Spin operator and one sample WASM app, all ClusterIP-only                                                                               |

Retention values are query bounds, not proof of deletion: an `emptyDir` store
loses everything when its Pod is replaced, and a retention window stops queries
rather than guaranteeing the bytes are gone. Nothing in this repository creates a
PersistentVolumeClaim on your behalf.

## Getting started

You need `mise`, an age identity, a Git repository Argo can read, and a reviewed
OpenTofu backend for the roots you apply. The short path:

```bash
mise trust && mise install --locked

export ANYLINGS_CONFIG="$PWD/.local/instance/cluster.toml"
export ANYLINGS_OUTPUT_DIR="$PWD/.local/instance/rendered"
export SOPS_AGE_KEY_FILE="$PWD/.local/instance/age.key"
just init platform          # cluster.toml sample + age key, created once
just configure platform     # render, encrypt and validate
just check platform         # encryption and resolvable references, read-only
```

Then publish only the rendered `kubernetes/` directory to the configured Git
prefix, install Argo once with `just bootstrap platform`, and let Argo reconcile
the rest. [Operating the platform](docs/how-to/operate.md) walks through a
complete first installation, day-two changes and teardown; [configuration
reference](docs/reference/configuration.md) has every input and default.

## Observability

Opt in with `[observability] enabled = true`. A disabled section publishes
nothing. The slice keeps one metric store, one operational log store, probe
status, kubelet usage and workload state as ClusterIP Services; retention and
sizing are in the [configuration
reference](docs/reference/configuration.md#optional-slices-observability-and-spinkube).

Log coverage is Kubernetes Events plus a structured OTLP record — not application
or node container logs — and retention is a query bound, not erasure. The exact
field contract, rejected-record behaviour and verification commands are in the
[operational logs how-to](docs/observability/operational-logs.md).

## Documentation

- [Operating the platform](docs/how-to/operate.md) — first installation, publishing, changes, teardown.
- [Configuration reference](docs/reference/configuration.md) — `cluster.toml`, environment variables, published-tree and security rules.
- [Architecture](docs/explanation/architecture.md) — how ownership flows from OpenTofu to Argo, and what deletion means.
- [OpenTofu roots](infra/tofu/README.md) — the four roots, their inputs, state and validation.
- [k0s lifecycle](infra/tofu/k0s/README.md) — reset, drift and destruction behaviour for the native backend.
- [Operational logs](docs/observability/operational-logs.md) — the exact log record contract, retention and verification.
- [Retained volume preflight](docs/how-to/rebind-retained-volume.md) — read-only inspection only; it rebinds nothing.

## Security boundaries

- Repository configuration holds no credentials. Secrets are SOPS-encrypted
  files or references to objects you create; the tunnel token is a file path,
  never a TOML value.
- The published Git tree is `kubernetes/` alone. Age keys, kubeconfigs,
  inspection manifests and OpenTofu state stay outside it.
- Every cluster call needs an explicit kubeconfig and context; no implicit
  default context is used.
- Deleting the bootstrap Application, including `just destroy platform`,
  cascades through the platform tree. It is teardown, not a GitOps detach.

Licensed under the MIT terms in [LICENSE](LICENSE).
