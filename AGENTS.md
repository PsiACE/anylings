# Project instructions

Anylings runs a shared GitOps platform on native k0s and DOKS. Reuse established tools, keep changes scoped, and use concise English and semantic commits. Keep platform sources and native CI in the repository; do not convert it into a one-shot template export.

Name components and test inputs by their actual responsibility or behavior, not an unrelated backend. Keep one implementation and one lifecycle policy for each responsibility; remove redundant examples, checks and abstractions rather than maintaining parallel variants without a distinct purpose.

## Resource ownership

- OpenTofu manages both cluster backends. The k0s root uses the official terraform_data/k0sctl orchestration pattern internally; DOKS uses its cloud provider. k0s or DOKS provides the Kubernetes control plane, node networking, DNS, and environment-specific storage capabilities.
- OpenTofu manages cloud resources and the single GitOps bootstrap Application, not Argo-managed child resources.
- A thin bootstrap installs Argo and its initial secret material. Argo then manages itself and applications; do not also maintain a persistent OpenTofu Helm release for the same installation.
- Follow Autopilot's single deletion model: Git removal deletes workload Applications/resources; bootstrap/root cascade, while Argo self-management alone omits its deletion finalizer. Do not add a parallel retention mode or disable ApplicationSet deletion. Infrastructure/data lifecycle policies remain explicit.
- Require an explicit kubeconfig and context for cluster operations. Never overwrite or remove an unrelated cluster or service.

## Validation and publishing

Use the prepared mise/uv environment and the relevant prek hooks. Exercise configuration changes through `tests/config/` inputs and the render checks; do not duplicate model validation in a separate unit-test layer. Use action-first commands (`just init platform`, `just configure platform`, `just check config`). CI owns only disposable inputs, an upstream read-only Git daemon and assertions in `tests/ci/`; do not build Git service management or publish temporary hosted branches. k0s lifecycle belongs to the normal OpenTofu root, with k0sctl as its internal execution dependency, not a separate public CLI path or test script. The workflow invokes the test runner directly; do not add a public CI domain or a separate CI deployment implementation. The fixture calls the same cluster lifecycle, readiness, rendering, bootstrap and platform commands used for deployments. Only backend provisioning/capabilities may differ; do not maintain test-only cluster code.

Test observable contracts through supported inputs and entrypoints. Do not pin private helper structure, internal tool state formats, or incidental default versions. Keep narrow safety checks where they protect explicit promises such as secret confidentiality and non-destructive cleanup.

CI integration uses synthetic credentials and isolated state in a fresh private directory under `$RUNNER_TEMP`. The integration fixture runner requires explicit disposable GitHub runner authorization; do not spoof these guards or install k0s on the workstation. CI may create and clean up its own disposable k0s instance, but must never operate production clusters or use production keys. Host service installation and paid cloud operations require explicit authorization. Collect sanitized diagnostics on failure; do not publish private keys, decrypted manifests, kubeconfig credentials, or OpenTofu state as artifacts.

The per-pull-request gate is the offline set: safety contracts, both render inputs
and OpenTofu format/validate/test. The native rehearsal has its own workflow and
is opt-in through the `ci/native-rehearsal` label, so it needs a maintainer
decision rather than every push. Keep it out of the gate workflow, out of the
advisory review's dependencies and out of its check names: a skipped job reports
success, so a disabled heavy job in the gate would accept code it never checked.

Push candidates normally and let pull-request CI run automatically. Do not manually dispatch or rerun CI, and do not continuously poll it. If a workflow-token push suppresses normal PR events, report that checks were not triggered rather than dispatching a substitute run. Native evidence determines runtime acceptance once collected; Landing feedback is advisory. Maintainers decide merges and alert dismissal.

Publish only the rendered `kubernetes/` tree at the configured Git prefix. Keep bootstrap inspection manifests outside rendered output, private and untracked. Infrastructure roots are `k0s`, `doks` and `cloudflare`; the platform follows Argo CD Autopilot: one bootstrap Application owns Argo self-management and a projects root; the project ApplicationSet discovers application config files. Preserve existing backend/state when moving root directories. No automatic Release substitutes for an explicit Git publication.

## Dependency maintenance

- Maintain mise tools and locks, Python dependencies and `uv.lock`, Helm charts, container images, and GitHub Actions.
- Keep bootstrap and Argo self-management versions aligned. Keep `bubbuild/landing@main` tracking latest; preserve other action commit pins and tool checksum verification.
- Prefer releases at least three days old; explain urgent security exceptions. Keep major upgrades separate and describe compatibility risks.
- Reuse matching update PRs. Publish new candidates as draft PRs on `deps/landing-*`; repair a failed candidate on its existing branch.
- Regenerate locks with existing tools. Do not disable checks, change deployment policy, edit real secrets, or reformat unrelated files to make an update pass.
