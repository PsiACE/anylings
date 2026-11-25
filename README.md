# anylings

My personal workstation and homelab. I use Fedora as my daily desktop and run
home services on the same machine.

## How it works

```text
Fedora -> Ansible + OpenTofu/k0sctl -> k0s -> Helm seed -> Argo CD
GitHub main -> Argo CD -> Argo CD itself + apps
Browser -> Cloudflare Access + Tunnel (optional) -> Glance / Argo CD
```

Argo CD manages itself and deploys apps from `apps/*.yaml` through an
ApplicationSet. OpenTofu manages the k0s node and optional Cloudflare resources,
not the workloads.

## Layout

| Path | Purpose |
| --- | --- |
| `infra/` | Host, k0s, and Cloudflare configuration |
| `platform/` | Argo CD chart and GitOps configuration |
| `apps/` | One Helm values file per app |
| `bin/`, `duties.py`, `scripts/` | CLI, install workflow, and component tasks |
| `tests/` | Live-cluster smoke tests |
| `docs/` | Setup and operations guides |

## Commands

```sh
./install.sh            # Install tools and the CLI
anylings install        # Set up the host and cluster
anylings --list          # List operations
anylings status-argocd   # Check GitOps status
anylings ui-glance       # Open the local start page
anylings test            # Run smoke tests
```

Read the [published documentation](https://anylings.com/) or browse
[Installation](docs/install.md), [Cluster operations](docs/operations.md), and
[Remote access](docs/remote-access.md) in this checkout. To preview the docs
locally, run `uv run --group docs sphinx-autobuild docs docs/_build/html`.

## Components

### Workstation and cluster

| Component | Role |
| --- | --- |
| <img src="https://cdn.simpleicons.org/fedora" width="22" alt=""> [Fedora Workstation](https://fedoraproject.org/workstation/) | Native desktop and bare-metal host. |
| <img src="https://cdn.simpleicons.org/ansible" width="22" alt=""> [Ansible](https://www.ansible.com/) | Configures the host (currently Fedora). |
| <img src="https://cdn.simpleicons.org/opentofu" width="22" alt=""> [OpenTofu](https://opentofu.org/) | Provisions k0s and optional Cloudflare resources. |
| <img src="https://raw.githubusercontent.com/k0sproject/k0s/main/docs/img/k0s-logo-2025-horizontal.svg" width="42" alt=""> [k0s](https://k0sproject.io/) | Runs Kubernetes with Kine/SQLite on the workstation. |

### GitOps and apps

| Component | Role |
| --- | --- |
| <img src="https://cdn.simpleicons.org/helm" width="22" alt=""> [Helm](https://helm.sh/) | Seeds Argo CD and supplies a shared chart for apps. |
| <img src="https://cdn.simpleicons.org/argo" width="22" alt=""> [Argo CD](https://argo-cd.readthedocs.io/) | Reconciles its own installation and apps from Git. |
| <img src="https://raw.githubusercontent.com/glanceapp/glance/main/docs/logo.png" width="22" alt=""> [Glance](https://github.com/glanceapp/glance) | Start page with links to homelab services and this repository. |

### Remote access and credentials

| Component | Role |
| --- | --- |
| <img src="https://cdn.simpleicons.org/cloudflare" width="22" alt=""> [Cloudflare](https://developers.cloudflare.com/cloudflare-one/) | Optionally publishes Glance and Argo CD behind Access. |
| <img src="https://cdn.simpleicons.org/1password" width="22" alt=""> [1Password CLI](https://www.1password.dev/cli/) | Reads deployment credentials on demand. |

### Operations and testing

| Component | Role |
| --- | --- |
| <img src="https://raw.githubusercontent.com/jdx/mise/main/docs/public/logo-light.svg" width="22" alt=""> [mise](https://mise.jdx.dev/) | Installs the locked CLI toolchain. |
| <img src="https://cdn.simpleicons.org/uv" width="22" alt=""> [uv](https://docs.astral.sh/uv/) | Manages Python and locked dependencies. |
| <img src="https://pawamoy.github.io/duty/assets/images/favicon.png" width="22" alt=""> [Duty](https://pawamoy.github.io/duty/) | Runs `anylings` commands and orders their dependencies. |
| <img src="https://cdn.simpleicons.org/pytest" width="22" alt=""> [pytest](https://docs.pytest.org/) | Checks GitOps and local web endpoints. |
