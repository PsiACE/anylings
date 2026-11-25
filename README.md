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

## k0s

On the prepared Fedora host, with sudo access:

```sh
sudo -v
~/.local/bin/mise exec -- tofu -chdir=tofu/k0s init
~/.local/bin/mise exec -- tofu -chdir=tofu/k0s apply
sudo k0s kubectl get nodes
```

OpenTofu runs k0sctl locally when the node config changes. No SSH is needed.
The state tracks the config, not host drift. `tofu destroy` does not remove k0s.

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
