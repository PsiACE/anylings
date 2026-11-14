"""Guarded patches and identity fingerprints for a fixed-target rebind.

Two writes in this rehearsal are irreversible if they are wrong: switching a
volume to ``Retain`` and replacing its ``claimRef``. Both are sent as one JSON
patch whose ``test`` operations assert every identity the decision depends on,
so a concurrent change fails closed with a 422 instead of applying the replace.

Backend identity differs by storage capability, and the difference is the point:

- node-local volumes have ``spec.local.path``;
- CSI volumes have ``spec.csi.driver`` and ``spec.csi.volumeHandle``.

Both are reduced to a hash for the journal. A volume handle is a provider
credential-adjacent value, so it is compared and hashed here but never printed.
"""

from __future__ import annotations

import hashlib

from api import replace_op, test_op


class IdentityError(Exception):
    """The object does not have the identity the step declares."""


def fingerprint(value: object) -> str:
    """A stable short hash of an identity; safe to journal and report."""
    encoded = repr(value).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def backend(pv: dict) -> tuple:
    """(kind, ...identity) for a volume, by the backend it actually has.

    Node-local storage is expressed either as `spec.local.path` (a real
    node-local volume) or `spec.hostPath.path` (what the local-path provisioner
    creates). Both are the same identity claim to the rehearsal: a directory on
    this node, with no attach phase. CSI has a driver and a handle instead.
    """
    spec = pv.get("spec") or {}
    for kind, section in (("local", "local"), ("hostpath", "hostPath")):
        path = (spec.get(section) or {}).get("path")
        if isinstance(path, str) and path:
            return (kind, path)
    driver, handle = (spec.get("csi") or {}).get("driver"), (spec.get("csi") or {}).get("volumeHandle")
    if isinstance(driver, str) and driver and isinstance(handle, str) and handle:
        return ("csi", driver, handle)
    raise IdentityError("backend-identity-unrecognised")


def _backend_tests(kind: str, rest: list) -> list[dict]:
    """The `test` operations that pin the storage backend of this volume."""
    if kind == "local":
        return [test_op("/spec/local/path", rest[0])]
    if kind == "hostpath":
        return [test_op("/spec/hostPath/path", rest[0])]
    return [
        test_op("/spec/csi/driver", rest[0]),
        test_op("/spec/csi/volumeHandle", rest[1]),
    ]


def retained(pv: dict, expect_rv: str, claim: tuple) -> list[dict]:
    """Guard then set `Retain`, keeping every other identity fixed.

    ``claim`` is the (namespace, name, uid) the volume must still be bound to.
    A volume already retained is reported by the caller as adoption, not
    patched again: re-patching would restate a decision nobody re-verified.
    """
    kind, *rest = backend(pv)
    spec = pv["spec"]
    claim_ref = spec.get("claimRef") or {}
    operations = [
        test_op("/metadata/uid", pv["metadata"]["uid"]),
        test_op("/metadata/resourceVersion", expect_rv),
        test_op("/status/phase", pv["status"]["phase"]),
        test_op("/spec/persistentVolumeReclaimPolicy", spec["persistentVolumeReclaimPolicy"]),
        test_op("/spec/claimRef/namespace", claim_ref.get("namespace")),
        test_op("/spec/claimRef/name", claim_ref.get("name")),
        test_op("/spec/claimRef/uid", claim_ref.get("uid")),
    ]
    if claim != (claim_ref.get("namespace"), claim_ref.get("name"), claim_ref.get("uid")):
        raise IdentityError("claimref-not-the-declared-source")
    operations += _backend_tests(kind, rest)
    operations.append(replace_op("/spec/persistentVolumeReclaimPolicy", "Retain"))
    return operations


def rebound(pv: dict, expect_rv: str, source: tuple, target: tuple) -> list[dict]:
    """Guard then replace `claimRef` directly with the reserved target UID.

    The source claim must already be gone and the volume `Released`; the target
    claim must already exist `Pending` with an explicit `volumeName`. There is
    no intermediate clear, so no other claim can win the volume in a gap.
    """
    kind, *rest = backend(pv)
    spec = pv["spec"]
    claim_ref = spec.get("claimRef") or {}
    observed = (claim_ref.get("namespace"), claim_ref.get("name"), claim_ref.get("uid"))
    operations = [
        test_op("/metadata/uid", pv["metadata"]["uid"]),
        test_op("/metadata/resourceVersion", expect_rv),
        test_op("/status/phase", "Released"),
        test_op("/spec/persistentVolumeReclaimPolicy", "Retain"),
        test_op("/spec/claimRef/namespace", source[0]),
        test_op("/spec/claimRef/name", source[1]),
        test_op("/spec/claimRef/uid", source[2]),
    ]
    if observed != source:
        raise IdentityError("claimref-drifted-from-declared-source")
    if (pv.get("status") or {}).get("phase") != "Released":
        # The patch would also be refused by the API; refusing locally keeps a
        # wrong-phase request from being sent at all.
        raise IdentityError("volume-not-released")
    if (spec.get("persistentVolumeReclaimPolicy")) != "Retain":
        raise IdentityError("volume-not-retained")
    operations += _backend_tests(kind, rest)
    operations.append(
        replace_op(
            "/spec/claimRef",
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "namespace": target[0],
                "name": target[1],
                "uid": target[2],
            },
        )
    )
    return operations
