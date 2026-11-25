# anylings

A personal cloud on Fedora Workstation.

## Tools

On Fedora 45 x86_64, run this as your login user:

```sh
./scripts/bootstrap-tools.sh
~/.local/bin/mise exec -- tofu version
~/.local/bin/mise exec -- k0sctl version
~/.local/bin/mise exec -- kubectl version --client
~/.local/bin/mise exec -- uv run --locked ansible-playbook --version
```

The official installer puts mise in `~/.local/bin`. No shell setup is needed.

No system packages are installed. This step does not start a cluster.

## Fedora

After bootstrapping the tools, run:

```sh
~/.local/bin/mise exec -- uv run --locked ansible-playbook -i localhost, --ask-become-pass ansible/fedora.yml
```

The playbook enables firewalld and IPv4 forwarding for pod networking. It
leaves SELinux unchanged. It does not install k0s or touch storage.
