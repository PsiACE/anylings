# anylings

A personal cloud on Fedora Workstation.

## Tools

On Fedora 45 x86_64, run this as your login user:

```sh
./scripts/bootstrap-tools.sh
.tools/mise exec -- tofu version
.tools/mise exec -- k0sctl version
.tools/mise exec -- kubectl version --client
.tools/mise exec -- uv run --locked ansible-playbook --version
```

No system packages are installed. This step does not start a cluster.

## Fedora

After bootstrapping the tools, run:

```sh
.tools/mise exec -- uv run --locked ansible-playbook -i localhost, --ask-become-pass ansible/fedora.yml
```

The playbook enables firewalld and IPv4 forwarding for pod networking. It
leaves SELinux unchanged. It does not install k0s or touch storage.
