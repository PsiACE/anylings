# Remote access

Optional, if you want to reach the homelab from somewhere other than the
workstation. One hostname serves Glance at `/` and Argo CD at `/argocd`, both
behind Cloudflare Access; Argo CD still asks for its own login, and HTTPS is
terminated at the edge — the tunnel reaches the ClusterIP services over plain
HTTP. Do not expose service ports or the Kubernetes API.

## Provision the Cloudflare side

Create a scoped API token — **Cloudflare Tunnel: Edit** and **Access: Apps and
Policies: Edit** on the account, plus **DNS: Edit** on the zone — and store it
in 1Password. Keep the connector token (added later) in a separate item.

```sh
anylings sign-in-1password
anylings configure-cloudflare
```

For a manually added 1Password account, run
`eval "$(anylings sign-in-1password)"` in Bash first. `configure-cloudflare`
only copies templates into place — it never overwrites an existing `.env` or
`terraform.tfvars`. Then set the token's `op://` reference in `.env` and fill
`infra/cloudflare/terraform.tfvars` with the account ID, zone ID, team name,
hostname, and allowed admin email.

If you're reusing an existing state file, restore it before applying. See the
[configuration reference](reference.md) for where state lives.

```sh
anylings plan-cloudflare
anylings apply-cloudflare
```

`apply` asks before changing anything. Both commands use the same local backend
and resolve the API token only for OpenTofu.

## Connect the cluster

Once Argo CD has created the `cloudflared` namespace, save the connector token
in 1Password and point the cluster at it:

```sh
anylings connect-cloudflare 'op://vault/item/credential'
```

Set `replicas: 1` in `apps/cloudflared.yaml` and let Argo CD sync. To rotate
the token later, update the 1Password item and run
`anylings rotate-cloudflare` with the same reference.
`anylings status-cloudflare` checks the local Deployment only — it does not
prove the public route works. Verify `/` and `/argocd` through Access.
