# Remote access

One hostname serves Glance at `/` and Argo CD at `/argocd`. Cloudflare Access
allows only your email on both paths. Argo CD still requires its own login.
Cloudflare terminates HTTPS; `cloudflared` reaches the two ClusterIP services
over HTTP. Do not publish their ports or the Kubernetes API directly.

## Provision the edge

Use a Cloudflare-managed DNS zone with Zero Trust and a working login method.
Create a scoped API token with **Cloudflare Tunnel: Edit** and **Access: Apps
and Policies: Edit** for the account and **DNS: Edit** for the zone. Save it in
1Password. With desktop integration, sign in:

```sh
anylings sign-in-1password
```

If your CLI account was [added manually](https://developer.1password.com/docs/cli/sign-in-manually/),
load the session in Bash:

```bash
eval "$(anylings sign-in-1password)"
```

After [setting up the host and cluster](install.md), create the local configuration files:

```sh
anylings configure-cloudflare
```

Set the API token's `op://` reference in `.env` and the deployment settings in
`infra/cloudflare/terraform.tfvars`. The command leaves existing files alone.
Restore any existing state before applying. Keep state off-machine and do not
commit local inputs, state, or saved plans.

```sh
anylings plan-cloudflare
anylings apply-cloudflare
```

`apply` prompts for approval. Both commands initialize the same local backend
and resolve the API token only for OpenTofu.

## Connect the cluster

Save the connector token from the Cloudflare dashboard in a separate
1Password item. On the k0s host, after Argo CD creates the `cloudflared`
namespace, run:

```sh
anylings connect-cloudflare 'op://your-vault/cloudflare-tunnel/credential'
```

Set `replicas: 1` in `apps/cloudflared.yaml` and let Argo CD sync it. Keep the
connector token out of `.env` and OpenTofu. After rotating the token in
Cloudflare and updating its 1Password item, run `anylings rotate-cloudflare` with
the same reference to update the Secret and restart the connector. Use
`anylings status-cloudflare` to check the local Deployment, not the public route.

In a private browser window, verify both `/` and `/argocd` require Access.
Sign in and follow the Glance bookmark to the Argo CD login page. Local
port-forwarding is still available if Access is down.
