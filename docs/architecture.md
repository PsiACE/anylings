# Architecture

`anylings install` does two things: it configures the Fedora host (Ansible),
and then provisions k0s and seeds Argo CD (OpenTofu, k0sctl, Helm). Once that
seed is in place, Argo CD takes over — it reconciles its own installation and
everything in `apps/*.yaml` from GitHub `main`, the same way it did when the
cluster came up. There is no separate deploy step.

```text
Fedora host --Ansible----------------------> Host configuration
     |
     +--OpenTofu--> k0sctl--> k0s --Helm seed--> Argo CD
                                                  |  \
GitHub main -------------------------------------+   +--> Argo CD itself
                                                     +--> apps/*.yaml

Browser --optional--> Cloudflare Access/Tunnel --> Glance / Argo CD
```

Two boundaries matter. The Git repository is the single source of truth for
what runs — Argo CD and the apps both read from it. Everything else is local
state that shouldn't drift: k0s state and OpenTofu state live on the
workstation, and Cloudflare state stays local and off-machine.

## Components

| Component | Role |
| --- | --- |
| [Fedora Workstation](https://fedoraproject.org/workstation/) | Native desktop and bare-metal host |
| [Ansible](https://www.ansible.com/) | Configures the host |
| [OpenTofu](https://opentofu.org/) | Provisions k0s and optional Cloudflare resources |
| [k0s](https://k0sproject.io/) | Kubernetes with Kine/SQLite |
| [Helm](https://helm.sh/) | Seeds Argo CD; the shared app chart deploys workloads |
| [Argo CD](https://argo-cd.readthedocs.io/) | Reconciles itself and the apps from Git |
| [External Secrets Operator](https://external-secrets.io/) | Syncs 1Password secrets into Kubernetes via `ClusterSecretStore` |
| [Glance](https://github.com/glanceapp/glance) | Start page for the homelab |
| [Cloudflare](https://developers.cloudflare.com/cloudflare-one/) | Optionally publishes Glance and Argo CD behind Access |
| [1Password CLI](https://www.1password.dev/cli/) | Reads deployment credentials on demand |
| [mise](https://mise.jdx.dev/) | Installs the locked toolchain |
| [uv](https://docs.astral.sh/uv/) | Manages Python and locked dependencies |
| [Duty](https://pawamoy.github.io/duty/) | Runs the `anylings` CLI |
| [pytest](https://docs.pytest.org/) | Live cluster smoke tests |
