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

From the repo root, copy the two templates:

```sh
cp -i .env.example .env
cp -i infra/cloudflare/terraform.tfvars.example infra/cloudflare/terraform.tfvars
```

Set the API token's `op://` reference in `.env` and the deployment settings in
`infra/cloudflare/terraform.tfvars`. OpenTofu loads `terraform.tfvars`
automatically. Only `op run` loads `.env`.

```sh
mise="$HOME/.local/bin/mise"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/anylings"
mkdir -m 700 -p "$state_dir"
"$mise" exec -- tofu -chdir=infra/cloudflare init \
  -backend-config="path=$state_dir/cloudflare.tfstate"
"$mise" exec -- op run --env-file=.env -- tofu -chdir=infra/cloudflare plan
"$mise" exec -- op run --env-file=.env -- tofu -chdir=infra/cloudflare apply
```

Restore any existing state before applying. Do not commit local inputs,
state, or saved plans.

## Connect the cluster

Copy just the connector token for the `anylings` tunnel from the Cloudflare
dashboard into a separate 1Password item. On the k0s host, after Argo CD creates
the `cloudflared` namespace, replace the example reference and run this in
Bash. Do not enable the connector token in `.env`: `op run` would try to
resolve it during `plan` and `apply`, before the tunnel exists.

```bash
(
  set -euo pipefail
  sudo -v
  token_file=$(mktemp)
  trap 'rm -f -- "$token_file"' EXIT
  "$HOME/.local/bin/mise" exec -- op read --no-newline \
    'op://your-vault/cloudflare-tunnel/credential' > "$token_file"
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
