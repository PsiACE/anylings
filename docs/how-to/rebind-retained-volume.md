# Inspect a retained volume before a rebind

This page describes the read-only preflight for a retained PersistentVolume.
It is a **precondition report only**. It does not quiesce, rebind, resume or
migrate anything, and a `ready` result is not permission to do so.

## What it checks

`infra/platform/retained_pv.py` compares a snapshot of plain Kubernetes objects
with an expected identity and returns `{"ready": bool, "issues": [code]}`:

- PersistentVolume name, UID, CSI driver, volume handle and `claimRef`
- `Retain` reclaim policy
- source claim namespace, name, UID, `Bound` phase and `volumeName`
- agreement between the volume and the claim: storage class, access modes,
  volume mode, capacity strings
- destination namespace present and named as expected
- no existing destination claim, since this phase has no journal-owned claim UID
- every expected writer Deployment present, observed at its current generation,
  with all replica counts at zero
- no Pod, terminating included, still holding either claim

Capacity is compared as the exact string the API returned. There is no Quantity
parser, so an unexpected unit fails closed instead of being coerced.

Issue codes are bounded and never echo an object field, so a report is safe to
log: it cannot carry a volume handle, a Secret value or a provider message.

## Running the preflight

The CLI is a thin transport over that core. Provide an explicit kubeconfig,
context and a private expected-identity file:

```console
$ uv run --quiet --locked --no-dev python infra/platform/inspect-retained-pv.py \
    --kubeconfig /path/to/explicit.conf \
    --context platform-admin@platform \
    --expected /path/to/expected.json
```

The expected file is JSON, kept private and untracked:

```json
{
  "pv_name": "pvc-00000000-0000-0000-0000-000000000000",
  "pv_uid": "00000000-0000-0000-0000-000000000000",
  "csi_driver": "dobs.csi.digitalocean.com",
  "csi_handle": "00000000-0000-0000-0000-000000000000",
  "source_namespace": "app",
  "source_name": "data",
  "source_uid": "00000000-0000-0000-0000-000000000000",
  "target_namespace": "app-next",
  "target_name": "data",
  "writers": [{"namespace": "app", "name": "release-name"}]
}
```

Exit codes: `0` ready, `1` not ready, `2` input or transport error. Every
cluster call is a bounded `kubectl get` with the given kubeconfig, context and
request timeout; absence is requested with `--ignore-not-found`. Transport
failures print a single sanitized code such as `api-forbidden`,
`api-timeout`, `api-response-invalid` and never raw stderr.

## What this phase does not do

The following gates are unfinished, and this tool is not evidence for any:

1. Quiescing the remaining writers and confirming the volume size, including
   the `10Gi` home volume.
2. Journal-owned destination claim creation and UID proof.
3. Execute, resume, rollback and the reverse rebind direction.
4. Reverse-direction and resume support in the core.
5. Live CSI acceptance: driver behavior, attachment and real volume state are
   never observed, because nothing here contacts a cluster during tests.

Treat a `ready` report as one input to a later decision, not as the decision.
