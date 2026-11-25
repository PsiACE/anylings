# Quickstart

This is the whole setup: clone the repo, install the toolchain, bootstrap the
host and cluster, and check that everything is in sync. It assumes a fresh,
networked Fedora 45 Workstation (x86_64) and a regular user with sudo access.
It does not migrate an existing etcd cluster.

```sh
git clone https://github.com/PsiACE/anylings.git
cd anylings
```

::::{steps}
:::{step} Install the toolchain and the CLI
```sh
./install.sh
```

That links the `anylings` command into `~/.local/bin`. It installs the locked
tools (uv, OpenTofu, k0sctl, kubectl, Helm, 1Password CLI) and nothing else —
no host changes, no cluster.
:::

:::{step} Bootstrap the host and cluster
```sh
anylings install
```

This runs `apply-host`, `apply-k0s`, and `apply-argocd` in order. Reruns reuse
k0s state and skip the Argo CD seed once it exists. It does not apply optional
Cloudflare resources, and it needs no cloud credentials.
:::

:::{step} Check that everything converged
```sh
anylings status-k0s
anylings status-argocd
```

The node should report `Ready`, and Argo CD should show Argo CD itself as
`Synced` and `Healthy`. Glance syncs separately and may take a little longer.
:::
::::

If anything looks off, the [operations guide](operations.md) covers routine
recovery and app deployment. Once you are happy with the local setup, the
[remote access guide](remote-access.md) adds optional HTTPS via Cloudflare.
