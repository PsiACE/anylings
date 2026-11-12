# Route profiles

`production.routes.tfvars.json` is the reviewed public route mapping: three
hostnames, their in-cluster origins, and whether Cloudflare Access protects them.
It contains no account ID, email, identity provider, token, tunnel ID or R2
setting, so it is tracked. Identity inputs stay in the operator's private files.

Committing this profile changes nothing in Cloudflare. It must land before an
apply, and the current tunnel keeps serving the old mapping until the operator
applies during a controlled maintenance window, after the target Services are
ready.

The operator's plan reuses the existing production working root, backend and
`TF_DATA_DIR`, passes the current private inputs first and this profile last:

```bash
tofu -chdir="$ANYLINGS_TOFU_CLOUDFLARE_DIR" plan \
  -var-file=/private/instance/cloudflare/identity.tfvars.json \
  -var-file="$PWD/infra/tofu/cloudflare/profiles/production.routes.tfvars.json"
```

Use absolute paths. The later `-var-file` overrides only `routes`; the account,
tunnel, allowed emails, existing GitHub identity provider IDs, 24h session and R2
inputs are unchanged. Do not create a new state root for this file.

`bub.anylings.ai` and `argo.anylings.com` stay GitHub-only, single-allowed-email,
behind the services' own authentication. `magpie.anylings.ai` carries no Access
application: only its `/v1` path is published and the gateway still requires its
native bearer token. Old hostnames, old namespace origins and any UI port are
absent by design.

Old writers and PersistentVolume constraints belong to other stages; this
preparation batch pins no storage and rebinds nothing. The offline test
`tests/routes_profile.tftest.hcl` plans the committed profile against the mocked
provider: no token, no API call, no state.
