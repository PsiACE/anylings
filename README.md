# anylings

My Fedora workstation is also my homelab. Everything — the host configuration,
the k0s cluster, and the Argo CD setup — lives in this one repository, so the
machine is reproducible and the homelab has a single source of truth.

## How it works

A single command, `anylings install`, sets everything up: it applies an Ansible
playbook to the workstation, provisions a single-node k0s cluster with
Kine/SQLite, and seeds Argo CD. From then on Argo CD does the maintaining — it
reconciles its own installation and the `apps/*.yaml` manifests from this
repository's `main`, so every future change is just a commit and a push.

Remote access (HTTPS through Cloudflare Access and a tunnel) is deliberately
separate — it is optional and requires its own credentials.

Two boundaries to keep in mind:

- **Git is the source of truth** for Argo CD and the apps — change things there, not on the cluster.
- **State stays local**: k0s state lives on the workstation, OpenTofu state
  (k0s and Cloudflare) stays local and off-machine.

## Quick start

For a fresh, networked Fedora 45 Workstation (x86_64) and a regular user with
sudo access:

```sh
./install.sh              # install the locked toolchain and link the CLI
anylings install          # configure the host and bootstrap the cluster
anylings status-k0s       # node should be Ready
anylings status-argocd    # Argo CD should be Synced and Healthy
```

`./install.sh` does not touch the host, and `anylings install` does not
configure the optional Cloudflare resources. This setup does not migrate an
existing etcd cluster.

## Documentation

The site lives at [anylings.com](https://anylings.com/):

| Page | What it covers |
| --- | --- |
| [Quickstart](https://anylings.com/quickstart.html) | Install and bootstrap, step by step |
| [Operations](https://anylings.com/operations.html) | Deploy apps, open UIs, remote workers, backups |
| [Remote access](https://anylings.com/remote-access.html) | Optional HTTPS via Cloudflare Access and Tunnel |
| [Architecture](https://anylings.com/architecture.html) | The flow and the component inventory |
| [Reference](https://anylings.com/reference.html) | CLI, repository layout, local state, development |

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
