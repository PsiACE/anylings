# Cloudflare access

One hostname serves Glance at `/` and Argo CD at `/argocd`. Cloudflare Access
allows only your email on both paths. Argo CD still requires its own login.
Cloudflare terminates HTTPS; `cloudflared` reaches the two ClusterIP services
over HTTP. Do not publish their ports or the Kubernetes API directly.

## Provision the edge

Use a Cloudflare-managed DNS zone with Zero Trust and a working login method.
Create a scoped API token with **Cloudflare Tunnel: Edit** and **Access: Apps
and Policies: Edit** for the account and **DNS: Edit** for the zone. Save it in
1Password. The installer provides a pinned `op` through mise. Sign in to
[1Password CLI](https://developer.1password.com/docs/cli/sign-in-manually/)
locally with `~/.local/bin/mise exec -- op`. Keep the state outside the
checkout and back it up privately off-machine.

Create `infra/cloudflare/terraform.tfvars` locally:

```hcl
account_id  = "your-account-id"
zone_id     = "your-zone-id"
team_name   = "your-zero-trust-team"
hostname    = "home.example.com"
admin_email = "you@example.com"
```

```sh
mise="$HOME/.local/bin/mise"
state_dir="$HOME/.local/state/anylings"
mkdir -m 700 -p "$state_dir"
export CLOUDFLARE_API_TOKEN='op://Private/anylings Cloudflare API/credential'
"$mise" exec -- tofu -chdir=infra/cloudflare init \
  -backend-config="path=$state_dir/cloudflare.tfstate"
"$mise" exec -- op run -- tofu -chdir=infra/cloudflare plan
"$mise" exec -- op run -- tofu -chdir=infra/cloudflare apply
unset CLOUDFLARE_API_TOKEN
```

Replace the `op://` references with your item names. If you already have a
state file, restore it before applying. Do not commit state, plans, or inputs.

## Connect the cluster

Copy just the connector token for the `anylings` tunnel from the Cloudflare
dashboard into a separate 1Password item. On the k0s host, after Argo CD creates
the `cloudflared` namespace, run this in Bash:

```bash
(
  set -euo pipefail
  sudo -v
  token_file=$(mktemp)
  trap 'rm -f -- "$token_file"' EXIT
  "$HOME/.local/bin/mise" exec -- op read --no-newline 'op://Private/anylings Tunnel/credential' > "$token_file"
  test -s "$token_file"
  sudo k0s kubectl -n cloudflared create secret generic cloudflared-token \
    --from-file=token="$token_file" --dry-run=client -o yaml |
    sudo k0s kubectl apply --server-side -f -
)
```

Set `replicas: 1` in `apps/cloudflared.yaml` and let Argo CD sync it. The token
never goes through OpenTofu. When rotating it, update 1Password, repeat the
Secret command, and restart the connector Deployment.

In a private browser window, verify both `/` and `/argocd` require Access.
Sign in and follow the Glance bookmark to the Argo CD login page. Local
port-forwarding is still available if Access is down.
