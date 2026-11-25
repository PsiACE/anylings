# anylings

A personal cloud on Fedora Workstation.

## Layout

- `infra/`: prepare Fedora; provision k0s and optional Cloudflare access.
- `platform/`: seed Argo CD once; it then manages itself and the ApplicationSet.
- `apps/`: one Helm values file per app using the shared `app-template` chart.
- `tests/`: smoke tests for a running test cluster.

## Install

On a fresh Fedora 45 Workstation (x86_64), clone this repository after the
manifests are on GitHub `main`. Run as your login user with sudo access:

```sh
./install.sh
```

The installer sets up locked tools, prepares Fedora, provisions one k0s node,
and waits for Argo CD to take over. You can rerun it without reseeding Argo CD.
No `dnf`, Git credentials, or cloud credentials are needed. The pinned
1Password CLI is installed but only used for optional Cloudflare access.

## k0s

OpenTofu runs k0sctl locally. Its state tracks the config, not host drift.
`tofu destroy` does not remove k0s.

The controller stores cluster state in Kine/SQLite. This setup supports remote
workers, but not additional controllers. Use it for a fresh cluster; it does
not migrate an existing etcd datastore.

For remote workers, set `spec.api.address` in the embedded k0s config if the
auto-detected IP is not reachable over the private network. Allow Kubernetes
and CNI traffic on that network, including pod access to Konnectivity.

Use `k0s backup` for cluster state. On a fresh host, use
`k0s restore --config-out=/etc/k0s/k0s.yaml <archive>` and start the controller
with the restored config. Keep backups private and off-machine. Back up
persistent volume data separately.

## Argo CD

Argo CD syncs `platform/argocd/`, including its own Application and the
ApplicationSet. Add `apps/<name>.yaml` to deploy an app in namespace `<name>`.
Automatic sync and self-heal are on; automatic pruning is off. No Git
credentials or public endpoint are needed.

The first install uses Helm. Argo CD then renders the same chart and values
from Git. After handoff, upgrade from Git, not with `helm upgrade` or
`helm uninstall`.

### Glance

Glance is a small start page for Argo CD and the anylings repository. Run these
in separate terminals on the machine with your browser and cluster access:

```sh
sudo k0s kubectl -n glance port-forward svc/glance 8088:8080
sudo k0s kubectl -n argocd port-forward svc/argocd-server 8081:80
```

Open Glance at <http://localhost:8088> and Argo CD at
<http://localhost:8081/argocd/>. The relative bookmark works when both share
one hostname. See [Cloudflare access](docs/cloudflare.md) for optional remote
access with 1Password.

## Test

On the test cluster, authenticate with `sudo -v` if needed, then run:

```sh
~/.local/bin/mise exec -- uv run --locked pytest
```
