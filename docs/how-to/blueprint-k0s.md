# Rehearse a fixed-target volume rebind on local k0s

This page describes one executable rehearsal: move a dataset to a claim in
another namespace and back again, on a real Kubernetes API and the real PV
binder, using synthetic data. It exists because the production cutover of the
gateway volume rests on decisions that must be proved before a writer is
stopped:

- a guarded delete really refuses a wrong UID or a stale resource version;
- a `Retain` patch really tests the volume identity before it changes policy;
- a read-only mount really refuses writes while the writer is live;
- a quiesced dataset can be backed up, encrypted, restored and hash-compared;
- the claim reference can be replaced in one guarded step, without a window in
  which the volume is unclaimed;
- the same volume, UID and bytes survive the move, forward and reverse.

It is deliberately **not** a proof about cloud storage. Node-local volumes have
no attach or detach phase, so nothing here is evidence about a CSI driver, a
provider volume handle, or a cloud data plane. Treat it as the binder and
transport half of a cutover rehearsal, not the whole of one.

## What runs where

```text
host                                    disposable guest (2 vCPU / 4 GiB)
  |                                       |
  |  checkout + pinned tools  --copy-->   |  just apply k0s          (k0s v1.37.2+k0s.0)
  |                                       |  just export k0s         (admin kubeconfig, 0600)
  |                                       |  just wait cluster
  |                                       |  just apply k0s-storage  (local-path class)
  |                                       |
  |                                       |  rehearse.py
  |                                       |    kubectl proxy 127.0.0.1 (its own PID)
  |                                       |    guarded DELETE / PATCH
  |                                       |    read-only mounts, backups
  v                                       v
  custody: encrypted archives + journal (private, 0700/0600)
```

Everything reusable lives in `tests/k0s/blueprint/`:

| Module         | Responsibility                                                      |
| -------------- | ------------------------------------------------------------------- |
| `guard.py`     | refuse a non-local API, context, node, namespace or storage backend |
| `api.py`       | the request transport: `DeleteOptions` preconditions, JSON patch    |
| `identity.py`  | guarded patch bodies and backend fingerprints                       |
| `backup.py`    | encrypt, decrypt, parse and hash-compare one dataset archive        |
| `cluster.py`   | explicit kubeconfig/context `kubectl` calls, no global state        |
| `manifests.py` | the few manifests that must be generated, not templated             |
| `rehearse.py`  | the ordered rehearsal and its private journal                       |
| `test_*.py`    | offline proofs of the transport, the guard and the custody rules    |

## Prerequisites

The guest needs the repository's pinned tools on `PATH` (`tofu`, `k0sctl`,
`kubectl`, `sops`, `age`, `jq`, `yq`, `just`, `kubeconform`), `python3`, and a
kernel with `/dev/kvm` on the host. No host service, host k0s install or `sudo`
is involved: the guest owns its own runtime.

The rehearsal refuses to start unless `ANYLINGS_BLUEPRINT_LOCAL=1` is set and
the target looks disposable. The refusal is the point, so do not work around it
on a shared cluster.

## Run it

Inside the guest, from a checkout of this repository:

```console
$ export ANYLINGS_BLUEPRINT_LOCAL=1
$ export KUBECONFIG=/secure/kubeconfig
$ age-keygen -o /secure/custody/age.key          # synthetic, local only
$ umask 077
$ python3 tests/k0s/blueprint/rehearse.py \
    --kubeconfig "$KUBECONFIG" \
    --context anylings-blueprint \
    --custody /secure/custody/run \
    --recipient "$(age-keygen -y /secure/custody/age.key)" \
    --key /secure/custody/age.key
```

The exit code is `0` only when every assertion held. Capture it — from the
process itself, not from a log tail and not from a backgrounded shell: a run
whose `$?` nobody recorded has not reported a result. A failure prints one code
such as `old-writer-still-holding-magpie-config`; the journal keeps the ordered
record. Codes name a rule, never an object field, a payload or a key.

Run one attempt per custody directory. A journal that contains an earlier
failure and a later success in the same directory is two attempts, and reading
it as one clean run overstates the evidence.

## Custody is private from creation

The archives, the streamed plaintext tar, the restore copy and the journal are
all created `0600` through an explicit descriptor, and every custody directory
`0700` one level at a time, so the caller's umask cannot widen them. The
encrypt and decrypt children additionally run under a restricted umask, and a
failed encrypt or decrypt removes its own partial output. The plaintext tar and
the restored copy are deleted after their hashes are compared: the ciphertext is
the archive, and plaintext never stays on disk.

The run journals a `source-identity` entry (a hash over every file in
`tests/k0s/blueprint`, plus a per-file map in private custody) before it touches
the cluster, and a `generated-identity` entry after the manifests are generated.
That is what ties the result to the exact code and inputs that produced it.

## The order, and why

1. **Admission.** A snapshot of the API server, context, nodes, namespaces and
   volume backends is checked before any fixture object exists. A production
   name, a cloud CSI driver or a public endpoint stops the run here.
2. **Writers.** Two fixture claims, one per dataset, each with a live writer
   that fills a deterministic record set. The T3-like claim is the control: its
   UID must be identical at every later phase.
3. **Read-only co-mount.** Both claims get a read-only reader while the writers
   are still healthy. The rehearsal proves reads succeed and that a write fails
   in the filesystem, not merely that the flag is set.
4. **Quiesce.** Both writers scale to zero, and only then is the holder set
   checked: the old writer UIDs must be gone and only the two recorded reader
   UIDs may remain.
5. **Backup.** Each dataset is streamed once through its reader, encrypted to a
   private archive, decrypted again, parsed as a tar, and hash-compared. An
   archive that cannot be restored fails here, so no unverified file is ever
   called a backup.
6. **Negative guards.** A throwaway claim proves a wrong UID and a stale
   resource version both return `409` and leave the object in place. The real
   volume then proves a patch with a wrong backend identity, and one with a
   stale resource version, both return `422` and change nothing.
7. **Retain.** The gateway volume is switched to `Retain` under a guard on uid,
   resource version, phase, policy, claim reference and backend identity. The
   T3-like volume's policy is asserted unchanged.
8. **Release.** Each reader is deleted with its UID as a precondition, and both
   claims must report zero holders afterwards.
9. **Reservation.** The target claim is created `Pending` with an explicit
   `volumeName`, while the old claim reference is still in place. The volume is
   never unclaimed.
10. **Rebind.** The old claim is deleted under `Retain` with a uid and resource
    version precondition, the volume must reach `Released` with the same UID,
    and one guarded patch replaces `claimRef` directly with the reserved target
    UID. There is no clear-then-set step.
11. **Forward result.** The target claim binds with the same volume UID, a new
    claim UID, and a read-only mount of the same claim returns the same data
    hash. The T3-like claim UID is unchanged.
12. **Reverse.** The reverse stops nothing that still runs, deletes the current
    claim under the same guards, creates a fresh source claim (a new UID, since
    the original cannot be restored), replaces `claimRef` back under the same
    guards, and re-checks the data hash. A Git revert does not perform this
    step.

## What this rehearsal does not claim

- No CSI driver, cloud volume handle, attach/detach or provider snapshot is
  exercised. `local-path` volumes are `hostPath` directories on one node.
- No multi-node or networked storage is involved, so no fencing behaviour is
  proved.
- The read-only readers are one-time utility Pods, not a GitOps workload.
- A passing run says the binder and transport decisions are sound. It does not
  authorise a production stage.

## Cleanup

The guest, its disks, the custody directory and the journal belong to the run
that created them. Stop the guest by the PID recorded for it, delete the custody
directory with its synthetic key, and confirm no `kubectl proxy` process
remains. Nothing here is shared with another checkout.
