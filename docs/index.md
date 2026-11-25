# anylings

Documentation for managing a Fedora workstation and its homelab cluster with
Ansible, OpenTofu, k0s, and Argo CD.

## Get started

- [Install anylings](install.md) in two explicit stages: install the tools,
  then configure the host and bootstrap the cluster.
- [Operate the cluster](operations.md): manage apps, check status, and back up
  k0s state.
- [Enable optional remote access](remote-access.md) through Cloudflare Access
  and Tunnel.

```{toctree}
:maxdepth: 2
:caption: Guides

install
operations
remote-access
```

The source repository is [PsiACE/anylings](https://github.com/PsiACE/anylings).
