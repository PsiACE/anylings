# Operate anylings

Run `anylings --list` for commands. Targeted commands use `anylings <action>-<target>`.

| Action | Targets |
| --- | --- |
| `apply` | `host`, `k0s`, `argocd`, `cloudflare` |
| `status` | `k0s`, `argocd`, `cloudflare` (local connector) |
| `configure`, `plan`, `connect`, `rotate` | `cloudflare` |
| `sign-in` | `1password` |
| `backup`, `restore` | `k0s` |
| `ui` | `argocd`, `glance` |

## Configure the host

Run `anylings apply-host` to reapply the Ansible playbook. Fedora is the only
supported host; its playbook lives at `infra/host/fedora.yml`.

## Manage apps

Add `apps/<name>.yaml` to deploy an app using the shared `app-template`
chart in namespace `<name>`. For Argo CD upgrades, change its chart or values
in Git after the Helm handoff. Do not use `helm upgrade` or `helm uninstall`
for the bootstrap release. Automatic sync and self-heal are on; automatic
pruning is off.

## Open local UIs

Run each port forward in its own terminal:

```sh
anylings ui-glance
anylings ui-argocd
```

Open [Glance](http://localhost:8088) or [Argo CD](http://localhost:8081/argocd/).
The relative Argo CD bookmark in Glance works only when both share one hostname,
as in the [remote access setup](remote-access.md).

## Back up and restore k0s

Back up cluster state to an existing directory:

```sh
anylings backup-k0s /path/to/backups
```

On a fresh host with k0s installed but stopped, restore the archive before
starting the controller with the restored config:

```sh
anylings restore-k0s /path/to/backup.tar.gz
```

Keep backups private and off-machine. Back up persistent volume data
separately. OpenTofu tracks the k0s configuration, not host drift; destroying
its state resource does not uninstall k0s.

## Add remote workers

Kine/SQLite supports one controller with additional workers. If the detected
address is not reachable from a worker, set `spec.api.address` in the embedded
k0s config in `infra/k0s/k0sctl.yaml`. Allow Kubernetes and CNI traffic over the
private network, including pod access to Konnectivity.

## Run smoke tests

On a test cluster, run:

```sh
anylings test
```

This checks Argo CD sync and local pages. It does not test Cloudflare Access.
