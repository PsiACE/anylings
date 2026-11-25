# Reference

## CLI

`anylings --list` prints the full task list with descriptions. Two of the most
used commands deserve a hint: `./install.sh` bootstraps the toolchain and the
`anylings` command itself, while `anylings install` is the one that configures
the host and the cluster — and deliberately does not touch Cloudflare.

| Task | Commands |
| --- | --- |
| Host and cluster | `apply-host`, `apply-k0s`, `apply-argocd`, `install` |
| Status and UI | `status-k0s`, `status-argocd`, `ui-glance`, `ui-argocd` |
| Backup | `backup-k0s <directory>`, `restore-k0s <archive>` |
| Remote access | `configure-cloudflare`, `plan-cloudflare`, `apply-cloudflare`, `connect-cloudflare <reference>`, `rotate-cloudflare <reference>`, `status-cloudflare` |
| Credentials and tests | `sign-in-1password`, `test` |

## Repository layout

| Path | Purpose |
| --- | --- |
| `infra/` | Host, k0s, and optional Cloudflare configuration |
| `platform/` | Argo CD chart and GitOps bootstrap |
| `apps/` | One Helm values file per workload |
| `bin/`, `duties.py`, `scripts/` | CLI entry point, workflow definitions, and component tasks |
| `tests/` | Live-cluster smoke tests |
| `docs/` | Architecture, guides, and this reference |
| `mise.toml`, `mise.lock` | Locked CLI toolchain |
| `pyproject.toml`, `uv.lock` | Python dependencies |

## Local inputs and state

Cloudflare inputs are local only: `.env` holds the token reference and
`infra/cloudflare/terraform.tfvars` holds the deployment values. OpenTofu
state lives at `$XDG_STATE_HOME/anylings/cloudflare.tfstate` (default:
`~/.local/state/anylings/cloudflare.tfstate`). Keep this state backups off-machine
and restore it before applying changes. Neither file, nor saved plans, nor state
is committed.

## Development

`anylings test` needs a running cluster. It verifies that Argo CD syncs the
apps and that the local services respond — it does not check Cloudflare Access.

Preview the docs while editing:

```sh
uv run --locked --group docs sphinx-autobuild docs docs/_build/html
```

Build with CI's strict warning checks (and ship to anylings.com the same way
CI does):

```sh
uv run --locked --group docs sphinx-build -E -b html -W --keep-going docs site
```

The `Docs` workflow builds pull requests and deploys `main` to
[anylings.com](https://anylings.com/).
