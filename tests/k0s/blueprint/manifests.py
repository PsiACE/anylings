"""Fixture manifests generated for one run, plus the two static ones.

Static fixtures hold the writers and readers whose shape is the reviewed
production shape. What cannot be static is the volume name: the local
provisioner chooses it, so a claim that must reserve one specific volume has to
name it at run time. Generating that manifest from the observed name keeps the
"explicit volumeName" property real instead of templated to a guess.
"""

from __future__ import annotations


def claim(namespace: str, name: str, volume: str | None = None) -> dict:
    """A claim that either reserves one volume or requests a fresh one."""
    spec = {
        "storageClassName": "local-path",
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    if volume:
        spec["volumeName"] = volume
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
            # The label is the ownership claim that makes a repeat run and a
            # cleanup safe: this run may remove its own claims and nothing else.
            "labels": {"anylings.dev/owner": "blueprint-fixture"},
        },
        "spec": spec,
    }


def probe_claim() -> dict:
    """A throwaway claim of the same kind, so guard proofs spare real objects."""
    return claim("blueprint-probe", "probe-claim")


def verify_pod(namespace: str, name: str, claim_name: str) -> dict:
    """A read-only Pod whose only job is to hash a dataset through a fresh mount.

    This is how data is compared after a rebind: the old writer Pod is gone, so
    exec into it would be a fabrication. A new mount of the same claim reads the
    bytes that survived, which is the property under test.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "anylings.dev/owner": "blueprint-fixture",
                "anylings.dev/utility": "read-only-reader",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "reader",
                    "image": (
                        "docker.io/library/busybox:1.37.0"
                        "@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec"
                    ),
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "8Mi"},
                        "limits": {"memory": "32Mi"},
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/src", "readOnly": True}
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": claim_name, "readOnly": True},
                }
            ],
        },
    }


def namespaces() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
            for name in (
                "blueprint-source",
                "blueprint-agents",
                "blueprint-target",
                "blueprint-probe",
            )
        ],
    }
