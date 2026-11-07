# Project instructions

Anylings adapts an existing Kubernetes template. Reuse its tools and keep changes scoped; do not add platform abstractions or operate real clusters from CI. Use concise English and semantic commits. Keep the template sources and native E2E workflow in the maintained repository; do not run `just template tidy` as a maintenance step.

## Validation and publishing

Use the prepared mise/uv environment. Run the relevant prek hooks and `uv run --locked pytest template/scripts/test_validate.py` for affected configuration code. For template changes, reuse the existing E2E fixtures with synthetic credentials; never use production keys or kubeconfig. Native E2E checks determine acceptance; Landing feedback is advisory.

After publishing a candidate with the workflow token, start native CI explicitly:

```bash
gh workflow run template-e2e.yaml --ref BRANCH -f number=PR_NUMBER -f head=FULL_CANDIDATE_SHA
```

A push or PR created with `GITHUB_TOKEN` does not start normal push/PR workflows. Use the full candidate SHA, link the pending checks, and do not wait for Landing review or its enclosing workflow. Maintainers decide merges; do not merge PRs or dismiss security alerts automatically.

## Dependency maintenance

- Maintain pinned mise tools and `.mise/mise.lock`, Python dependencies and `uv.lock`, Helm charts, container images, and GitHub Action revisions.
- Keep bootstrap and Argo self-management versions aligned when updating the same component. Keep `bubbuild/landing@main` tracking the latest Landing code; preserve other action commit pins and tool checksum validation.
- Prefer releases at least three days old; urgent security updates may be proposed immediately with an explanation. Keep major upgrades separate and describe compatibility risks.
- Reuse matching update PRs. Group only related changes and publish new candidates as draft PRs on `deps/landing-*`; repair a failed candidate on its existing branch.
- Regenerate locks with the existing tools. Do not disable checks, modify deployment policy, edit secrets, or introduce unrelated formatting changes to make an update pass.
