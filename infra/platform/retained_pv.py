"""Read-only inspection of a retained PersistentVolume snapshot.

An operator transport collects plain Kubernetes JSON and passes it here; this
module performs no I/O and never mutates its inputs. It answers one question:
is the snapshot consistent with the expected identity after the writers have
quiesced? It does not execute, journal, roll back
or decide a migration, and it is not evidence about a live CSI driver.

``inspect_snapshot(snapshot, expected)`` returns ``{"ready": bool, "issues":
[code, ...]}``. Codes are a closed vocabulary and never echo object fields, so
a report is safe to log: it cannot leak a volume handle, a planted Secret
marker or a provider message.

``expected-invalid``            expected identity missing, empty or malformed
``snapshot-malformed``          a required snapshot key or list is unusable
``pv-name-mismatch``            PV name differs from expected
``pv-uid-mismatch``             PV uid differs from expected
``csi-driver-mismatch``         PV CSI driver differs from expected
``csi-handle-mismatch``         PV CSI volume handle differs from expected
``pv-claimref-mismatch``        PV claimRef does not name the source claim
``pv-not-retained``             PV reclaim policy is not Retain
``source-claim-mismatch``       source PVC namespace/name/uid differ from expected
``source-claim-unbound``        source PVC is not Bound
``source-volume-mismatch``      source PVC volumeName is not the expected PV
``storage-class-mismatch``      PV and source PVC storage classes differ
``access-modes-mismatch``       PV and source PVC access modes differ
``volume-mode-mismatch``        PV and source PVC volume modes differ
``capacity-mismatch``           PV and source PVC capacity strings differ
``target-namespace-missing``    destination namespace object is absent
``target-namespace-mismatch``   destination namespace name differs
``target-claim-malformed``      destination PVC object is unusable
``target-claim-mismatch``       destination PVC namespace/name differ
``target-claim-foreign``        destination PVC exists without proven ownership
``writers-unproven``            expected writer set is empty
``writer-malformed``            a Deployment or its replica counts is unusable
``writer-missing``              an expected writer Deployment is absent
``writer-not-quiesced``         a writer Deployment has non-zero replicas
``writer-unobserved``           a writer has not observed its current generation
``pod-malformed``               a Pod or its volume list is unusable
``pod-references-claim``        a Pod (even a terminating one) uses a claim

Capacity is compared as the exact string Kubernetes returned: there is no
Quantity parser here, so an unexpected unit shape fails closed rather than
being coerced. An empty expected writer set is reported as unproven, never as
quiescence.
"""

from __future__ import annotations

SNAPSHOT_KEYS = ("pv", "source_pvc", "target_namespace", "target_pvc", "deployments", "pods")
EXPECTED_KEYS = (
    "pv_name", "pv_uid", "csi_driver", "csi_handle", "source_namespace",
    "source_name", "source_uid", "target_namespace", "target_name",
)


def _text(value: object) -> str | None:
    """A non-blank string, or None: blanks never satisfy an identity check."""
    return value if isinstance(value, str) and value.strip() else None


def _map(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _dig(value: object, *keys: str) -> object:
    for key in keys:
        value = _map(value).get(key)
    return value


def _identity(value: object, flat: bool = False) -> tuple[object, ...]:
    """Namespace/name/uid from metadata, or from a claimRef itself (flat)."""
    fields = ("namespace", "name", "uid")
    return tuple(_text(_dig(value, key) if flat else _dig(value, "metadata", key)) for key in fields)


def _mode(value: object) -> str | None:
    """Only an omitted mode uses the Kubernetes default, not malformed values."""
    if value is None:
        return "Filesystem"
    return value if isinstance(value, str) and value in {"Filesystem", "Block"} else None


def _modes(value: object) -> frozenset[str] | None:
    if not isinstance(value, list) or not value:
        return None
    modes = [_text(item) for item in value]
    allowed = {"ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany", "ReadWriteOncePod"}
    return frozenset(modes) if all(mode in allowed for mode in modes) else None


def _count(value: object, default: int) -> int | None:
    """A replica count, defaulted when absent and rejected when nonsensical."""
    if value is None:
        return default
    usable = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value if usable else None


def _writer(value: object) -> tuple[str, str] | None:
    """Parse one expected writer as a namespace/name pair or a "ns/name" string."""
    if isinstance(value, str) and value.count("/") == 1:
        namespace, name = (_text(part) for part in value.split("/"))
    else:
        namespace, name = _text(_dig(value, "namespace")), _text(_dig(value, "name"))
    return (namespace, name) if namespace and name else None


def _expectation(expected: object) -> tuple[dict[str, str], list[tuple[str, str]]] | None:
    """Parse the expected identity; any gap means the whole input is unusable."""
    if not isinstance(expected, dict):
        return None
    names = {key: _text(expected.get(key)) for key in EXPECTED_KEYS}
    writers = expected.get("writers")
    members = [_writer(item) for item in writers] if isinstance(writers, list) else [None]
    if any(name is None for name in names.values()) or None in members:
        return None
    if (names["source_namespace"], names["source_name"]) == (names["target_namespace"], names["target_name"]):
        return None
    return names, members


def _check_volume(snapshot: dict, names: dict[str, str], issues: set[str]) -> None:
    """Check the PV identity, the source claim, and the fields they must share."""
    pv, claim = snapshot.get("pv"), snapshot.get("source_pvc")
    if not isinstance(pv, dict) or not isinstance(claim, dict):
        issues.add("snapshot-malformed")
        return
    spec, claim_spec = _map(pv.get("spec")), _map(claim.get("spec"))
    source = (names["source_namespace"], names["source_name"], names["source_uid"])
    capacity = _text(_dig(claim_spec, "resources", "requests", "storage"))
    status_capacity = _text(_dig(claim, "status", "capacity", "storage"))
    declared = (
        ("pv-name-mismatch", _identity(pv)[1], names["pv_name"]),
        ("pv-uid-mismatch", _identity(pv)[2], names["pv_uid"]),
        ("csi-driver-mismatch", _dig(spec, "csi", "driver"), names["csi_driver"]),
        ("csi-handle-mismatch", _dig(spec, "csi", "volumeHandle"), names["csi_handle"]),
        ("pv-claimref-mismatch", _identity(spec.get("claimRef"), flat=True), source),
        ("pv-not-retained", _text(spec.get("persistentVolumeReclaimPolicy")), "Retain"),
        ("source-claim-mismatch", _identity(claim), source),
        ("source-claim-unbound", _text(_dig(claim, "status", "phase")), "Bound"),
        ("source-volume-mismatch", _text(claim_spec.get("volumeName")), names["pv_name"]),
        ("storage-class-mismatch", _text(spec.get("storageClassName")), _text(claim_spec.get("storageClassName"))),
        ("access-modes-mismatch", _modes(spec.get("accessModes")), _modes(claim_spec.get("accessModes"))),
        ("volume-mode-mismatch", _mode(spec.get("volumeMode")), _mode(claim_spec.get("volumeMode"))),
        ("capacity-mismatch", _text(_dig(spec, "capacity", "storage")), capacity),
    )
    required = {"access-modes-mismatch", "volume-mode-mismatch", "capacity-mismatch"}
    for code, actual, wanted in declared:
        if actual != wanted or (code in required and (actual is None or wanted is None)):
            issues.add(code)
    if status_capacity is not None and status_capacity != _text(_dig(spec, "capacity", "storage")):
        issues.add("capacity-mismatch")


def _check_destination(snapshot: dict, names: dict[str, str], issues: set[str]) -> None:
    """The destination namespace must exist, and no foreign claim may sit in it."""
    namespace = snapshot.get("target_namespace")
    if not isinstance(namespace, dict):
        issues.add("target-namespace-missing")
    elif _identity(namespace)[1] != names["target_namespace"]:
        issues.add("target-namespace-mismatch")
    claim = snapshot.get("target_pvc")
    if claim is None:
        return
    if not isinstance(claim, dict):
        issues.add("target-claim-malformed")
        return
    if _identity(claim)[:2] != (names["target_namespace"], names["target_name"]):
        issues.add("target-claim-mismatch")
    # This initial inspection has no journal-owned target UID. Even a matching
    # volumeName is not permission to adopt an existing claim. Resume is separate.
    issues.add("target-claim-foreign")


def _check_writers(snapshot: dict, writers: list[tuple[str, str]], issues: set[str]) -> None:
    """Every expected writer Deployment must exist with all replica counts at 0."""
    if not writers:
        issues.add("writers-unproven")
        return
    deployments = snapshot.get("deployments")
    if not isinstance(deployments, list):
        issues.add("snapshot-malformed")
        return
    found: dict[tuple[str, str], dict] = {}
    for deployment in deployments:
        key = _identity(deployment)[:2]
        if not isinstance(deployment, dict) or None in key:
            issues.add("writer-malformed")
        else:
            if key in found:
                issues.add("writer-malformed")
            found[key] = deployment
    for key in writers:
        deployment = found.get(key)
        if deployment is None:
            issues.add("writer-missing")
            continue
        if not isinstance(deployment.get("spec"), dict) or not isinstance(deployment.get("status"), dict):
            issues.add("writer-malformed")
            continue
        status = deployment["status"]
        generation = _count(_dig(deployment, "metadata", "generation"), 0)
        observed = _count(status.get("observedGeneration"), 0)
        if generation is None or generation < 1 or observed != generation:
            issues.add("writer-unobserved")
        counts = [_count(_dig(deployment, "spec", "replicas"), 1)] + [
            _count(status.get(key), 0) for key in ("replicas", "readyReplicas", "availableReplicas")
        ]
        if None in counts:
            issues.add("writer-malformed")
        elif any(counts):
            issues.add("writer-not-quiesced")


def _check_pods(snapshot: dict, names: dict[str, str], issues: set[str]) -> None:
    """Reject any Pod, terminating or not, holding the source or target claim."""
    pods = snapshot.get("pods")
    if not isinstance(pods, list):
        issues.add("snapshot-malformed")
        return
    claims = {
        (names["source_namespace"], names["source_name"]),
        (names["target_namespace"], names["target_name"]),
    }
    for pod in pods:
        spec = _dig(pod, "spec")
        namespace = _text(_dig(pod, "metadata", "namespace"))
        if not isinstance(spec, dict) or namespace is None:
            issues.add("pod-malformed")
            continue
        volumes = spec.get("volumes", [])
        if not isinstance(volumes, list):
            issues.add("pod-malformed")
            continue
        for volume in volumes:
            if not isinstance(volume, dict):
                issues.add("pod-malformed")
                continue
            reference = _dig(volume, "persistentVolumeClaim")
            if reference is None:
                continue
            claim_name = _text(_dig(reference, "claimName"))
            if claim_name is None:
                issues.add("pod-malformed")
            elif (_text(_dig(pod, "metadata", "namespace")), claim_name) in claims:
                issues.add("pod-references-claim")


def _report(issues: set[str]) -> dict[str, object]:
    problems = sorted(issues)
    return {"ready": not problems, "issues": problems}


def inspect_snapshot(snapshot: object, expected: object) -> dict[str, object]:
    """Report whether a retained-PV snapshot matches the expected identity.

    Pure read-only and fail-closed: any absent, malformed or unrecognised input
    shape yields an issue code instead of readiness.
    """
    if not isinstance(snapshot, dict):
        return _report({"snapshot-malformed"})
    parsed = _expectation(expected)
    if parsed is None:
        return _report({"expected-invalid"})
    names, writers = parsed
    issues = {"snapshot-malformed"} if any(key not in snapshot for key in SNAPSHOT_KEYS) else set()
    _check_volume(snapshot, names, issues)
    _check_destination(snapshot, names, issues)
    _check_writers(snapshot, writers, issues)
    _check_pods(snapshot, names, issues)
    return _report(issues)
