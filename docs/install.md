# Install anylings

Use a fresh, networked Fedora 45 Workstation (x86_64). Clone the repository
and run the following steps as your regular user.

## 1. Install the tools and CLI

From the checkout, run:

```sh
./install.sh
```

This installs mise and the locked tools, and links `anylings` into
`~/.local/bin`. It does not configure the host or start a cluster. If that
directory is not on `PATH`, use `~/.local/bin/anylings`. If you move the
checkout, remove the old link and rerun this step.

## 2. Set up the host and cluster

Run with sudo access:

```sh
anylings install
```

Duty runs `apply-host`, `apply-k0s`, and `apply-argocd` in order. Ansible
configures Fedora, OpenTofu provisions k0s, and Helm seeds Argo CD. Argo CD
then syncs from GitHub `main` and takes over its own installation. Reruns reuse
k0s state and skip the Argo CD seed if it already exists. This does not migrate
an existing etcd cluster or require cloud credentials.

Check the cluster:

```sh
anylings status-k0s
anylings status-argocd
```

The node should be `Ready` and Argo CD should be `Synced` and `Healthy`.
Glance syncs separately and may take longer. Follow [Remote access](remote-access.md)
only if you want to publish the services.
