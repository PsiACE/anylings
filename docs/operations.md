# Operations

Day-to-day tasks for running the cluster. Most of these take a single
`anylings` command; this page is the map of which one does which.

## Configure the host

Fedora is the only supported host. To reapply the Ansible playbook — for
example after a package or kernel change — run:

```sh
anylings apply-host
```

## Sync dotfiles

Minimal user dotfiles for Bash (`.bashrc`, `.bash_profile`), Git (`.gitconfig`,
with per-machine identity in `~/.gitconfig.local`), and global `mise` (Node 24
with `.nvmrc`/`.node-version` support) live in `dotfiles/` and are applied with
chezmoi:

```sh
anylings diff-dotfiles
anylings apply-dotfiles
```

`anylings install` does not apply dotfiles automatically.

## Deploy an app

Adding a workload is a Git operation, not a CLI one. Drop an
`apps/<name>.yaml` values file into the repository on `main`, and Argo CD syncs
it in via the ApplicationSet: the shared `app-template` chart renders the
values and creates the `<name>` namespace. Sync is automatic, self-heal is on,
and pruning stays off.

If you edit Argo CD itself — its chart, values, or the ApplicationSet — do it
through Git as well. Argo CD re-applies `platform/argocd/` from `main`; don't
tamper with it via `helm upgrade` or `helm uninstall`.

## Open local UIs

Both services are reachable locally through a port forward. Run each in its own
terminal:

```sh
anylings ui-glance
anylings ui-argocd
```

Then open [Glance](http://localhost:8088) or
[Argo CD](http://localhost:8081/argocd/). The relative `/argocd` bookmark on
the Glance start page works when both services share one hostname, as in the
[remote access guide](remote-access.md).

## Add remote workers

Kine/SQLite supports one controller with additional workers. If the detected
API address is unreachable from the workers, set `spec.api.address` in
`infra/k0s/k0sctl.yaml` and make sure Kubernetes/CNI traffic — including pod
access to Konnectivity — flows over the private network. Workers additionally
need the generated k0s configuration applied on them.

## Back up and restore k0s state

Cluster state lives in Kine and is backed up as a single archive:

```sh
anylings backup-k0s /path/to/backups
```

On a fresh host with k0s installed but stopped, restore before starting the
controller:

```sh
anylings restore-k0s /path/to/backup.tar.gz
```

Keep archives private and off-machine. Backups are separate from OpenTofu
state and do not include persistent volume data — back PVs up separately.
