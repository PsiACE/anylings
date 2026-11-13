"""Unit tests for the read-only retained-PV snapshot inspection core.

Synthetic Kubernetes-shaped dicts only: no cluster, no transport and no
credentials. Every case deep-copies its fixture so the mutation checks can
prove that inspection leaves its inputs untouched.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra" / "platform"))

from retained_pv import inspect_snapshot  # noqa: E402

SECRET_MARKER = "synthetic-marker-never-in-a-report"


def snapshot() -> dict:
    """A consistent, quiescent snapshot for the expected values below."""
    return {
        "pv": {
            "metadata": {"name": "pv-data", "uid": "pv-uid"},
            "spec": {
                "csi": {"driver": "dobs.csi.digitalocean.com", "volumeHandle": SECRET_MARKER},
                "claimRef": {"namespace": "src", "name": "data", "uid": "claim-uid"},
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "do-block-storage",
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Filesystem",
                "capacity": {"storage": "10Gi"},
            },
        },
        "source_pvc": {
            "metadata": {"namespace": "src", "name": "data", "uid": "claim-uid"},
            "spec": {
                "volumeName": "pv-data",
                "storageClassName": "do-block-storage",
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Filesystem",
                "resources": {"requests": {"storage": "10Gi"}},
            },
            "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}},
        },
        "target_namespace": {"metadata": {"name": "dst"}},
        "target_pvc": None,
        "deployments": [
            {
                "metadata": {"namespace": "src", "name": "app", "generation": 1},
                "spec": {"replicas": 0},
                "status": {"replicas": 0, "readyReplicas": 0, "availableReplicas": 0, "observedGeneration": 1},
            }
        ],
        "pods": [],
    }


def expected() -> dict:
    return {
        "pv_name": "pv-data",
        "pv_uid": "pv-uid",
        "csi_driver": "dobs.csi.digitalocean.com",
        "csi_handle": SECRET_MARKER,
        "source_namespace": "src",
        "source_name": "data",
        "source_uid": "claim-uid",
        "target_namespace": "dst",
        "target_name": "data",
        "writers": [{"namespace": "src", "name": "app"}],
    }


def target_claim(volume: str | None = None, phase: str = "Pending") -> dict:
    return {
        "metadata": {"namespace": "dst", "name": "data", "uid": "unowned-uid"},
        "spec": {"volumeName": volume} if volume else {},
        "status": {"phase": phase},
    }


def pod(namespace: str, claim: str, terminating: bool = False) -> dict:
    metadata = {"namespace": namespace, "name": f"{claim}-pod"}
    if terminating:
        metadata["deletionTimestamp"] = "2026-11-13T00:00:00Z"
    return {
        "metadata": metadata,
        "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]},
    }


class InspectionTest(unittest.TestCase):
    def inspect(self, snapshot_data: object = None, expected_data: object = None) -> dict:
        return inspect_snapshot(
            snapshot() if snapshot_data is None else snapshot_data,
            expected() if expected_data is None else expected_data,
        )

    def issues(self, snapshot_data: object = None, expected_data: object = None) -> list[str]:
        return self.inspect(snapshot_data, expected_data)["issues"]

    def assert_fails_with(self, code: str, snapshot_data: object = None, expected_data: object = None) -> None:
        report = self.inspect(snapshot_data, expected_data)
        self.assertFalse(report["ready"], report)
        self.assertIn(code, report["issues"])

    # ---------------------------------------------------------- ready cases --

    def test_consistent_snapshot_is_ready(self):
        self.assertEqual(self.inspect(), {"ready": True, "issues": []})

    def test_pending_destination_claim_is_not_implicitly_adopted(self):
        data = snapshot()
        data["target_pvc"] = target_claim()
        self.assert_fails_with("target-claim-foreign", data)

    def test_same_volume_destination_still_requires_ownership_proof(self):
        data = snapshot()
        data["target_pvc"] = target_claim("pv-data", "Bound")
        self.assert_fails_with("target-claim-foreign", data)

    # ------------------------------------------------- identity and CSI checks --

    def test_missing_expected_identity_fails_closed(self):
        for key in expected():
            if key == "writers":
                continue
            broken = expected()
            del broken[key]
            self.assert_fails_with("expected-invalid", expected_data=broken)

    def test_empty_expected_identity_fails_closed(self):
        blank = expected()
        blank["pv_uid"] = "   "
        self.assert_fails_with("expected-invalid", expected_data=blank)

    def test_wrong_pv_name_and_uid(self):
        data = snapshot()
        data["pv"]["metadata"].update(name="pv-other", uid="other-uid")
        self.assertEqual(self.issues(data), ["pv-name-mismatch", "pv-uid-mismatch"])

    def test_wrong_csi_driver_and_handle(self):
        data = snapshot()
        data["pv"]["spec"]["csi"] = {"driver": "other.csi", "volumeHandle": "other-handle"}
        self.assertEqual(self.issues(data), ["csi-driver-mismatch", "csi-handle-mismatch"])

    def test_missing_csi_block_is_not_ready(self):
        data = snapshot()
        del data["pv"]["spec"]["csi"]
        self.assert_fails_with("csi-driver-mismatch", data)

    # ------------------------------------------------ PV and source PVC checks --

    def test_wrong_claim_ref(self):
        data = snapshot()
        data["pv"]["spec"]["claimRef"]["uid"] = "other-uid"
        self.assertEqual(self.issues(data), ["pv-claimref-mismatch"])

    def test_delete_reclaim_policy_is_rejected(self):
        data = snapshot()
        data["pv"]["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
        self.assertEqual(self.issues(data), ["pv-not-retained"])

    def test_source_claim_mismatch(self):
        data = snapshot()
        data["source_pvc"]["metadata"]["name"] = "other"
        self.assertIn("source-claim-mismatch", self.issues(data))

    def test_source_claim_unbound(self):
        data = snapshot()
        data["source_pvc"]["status"]["phase"] = "Pending"
        self.assert_fails_with("source-claim-unbound", data)

    def test_source_claim_volume_name_mismatch(self):
        data = snapshot()
        data["source_pvc"]["spec"]["volumeName"] = "pv-other"
        self.assert_fails_with("source-volume-mismatch", data)

    def test_spec_agreement_mismatches(self):
        for field, value, code in (
            ("storageClassName", "other-class", "storage-class-mismatch"),
            ("accessModes", ["ReadWriteMany"], "access-modes-mismatch"),
            ("volumeMode", "Block", "volume-mode-mismatch"),
        ):
            data = snapshot()
            data["pv"]["spec"][field] = value
            self.assertEqual(self.issues(data), [code], field)

    def test_capacity_mismatch_without_quantity_parsing(self):
        data = snapshot()
        data["pv"]["spec"]["capacity"]["storage"] = "10000Mi"
        self.assertEqual(self.issues(data), ["capacity-mismatch"])

    def test_unknown_capacity_unit_fails_closed(self):
        data = snapshot()
        data["source_pvc"]["status"]["capacity"]["storage"] = "ten-gibibytes"
        self.assertEqual(self.issues(data), ["capacity-mismatch"])

    # ------------------------------------------------------ destination checks --

    def test_missing_target_namespace(self):
        data = snapshot()
        data["target_namespace"] = None
        self.assertEqual(self.issues(data), ["target-namespace-missing"])

    def test_wrong_target_namespace(self):
        data = snapshot()
        data["target_namespace"]["metadata"]["name"] = "elsewhere"
        self.assertEqual(self.issues(data), ["target-namespace-mismatch"])

    def test_foreign_destination_claim_bound_to_other_pv(self):
        data = snapshot()
        data["target_pvc"] = target_claim("pv-foreign", "Bound")
        self.assertEqual(self.issues(data), ["target-claim-foreign"])

    def test_destination_claim_in_wrong_namespace(self):
        data = snapshot()
        data["target_pvc"] = target_claim()
        data["target_pvc"]["metadata"]["namespace"] = "elsewhere"
        self.assert_fails_with("target-claim-mismatch", data)

    # ---------------------------------------------------------- writer checks --

    def test_empty_expected_writers_is_not_quiescence(self):
        blank = expected()
        blank["writers"] = []
        self.assertEqual(self.issues(expected_data=blank), ["writers-unproven"])

    def test_missing_writer_deployment(self):
        data = snapshot()
        data["deployments"] = []
        self.assertEqual(self.issues(data), ["writer-missing"])

    def test_nonzero_writer_replicas(self):
        for field, value in (("replicas", 1), ("readyReplicas", 2), ("availableReplicas", 1)):
            data = snapshot()
            target = data["deployments"][0]["spec" if field == "replicas" else "status"]
            target[field] = value
            self.assertEqual(self.issues(data), ["writer-not-quiesced"], field)

    def test_writer_with_nonsense_replica_count(self):
        data = snapshot()
        data["deployments"][0]["status"]["readyReplicas"] = "two"
        self.assertEqual(self.issues(data), ["writer-malformed"])

    # ------------------------------------------------------------- pod checks --

    def test_source_pod_referencing_claim(self):
        data = snapshot()
        data["pods"] = [pod("src", "data")]
        self.assertEqual(self.issues(data), ["pod-references-claim"])

    def test_terminating_pod_still_counts(self):
        data = snapshot()
        data["pods"] = [pod("src", "data", terminating=True)]
        self.assertEqual(self.issues(data), ["pod-references-claim"])

    def test_destination_pod_referencing_target_claim(self):
        data = snapshot()
        data["pods"] = [pod("dst", "data")]
        self.assertEqual(self.issues(data), ["pod-references-claim"])

    def test_pod_in_another_namespace_is_ignored(self):
        data = snapshot()
        data["pods"] = [pod("other", "data")]
        self.assertEqual(self.issues(data), [])

    def test_malformed_pod_fails_closed(self):
        data = snapshot()
        data["pods"] = ["not-a-pod"]
        self.assertEqual(self.issues(data), ["pod-malformed"])

    # --------------------------------------------------- structural fail-closed --

    def test_missing_snapshot_key(self):
        for key in ("pv", "source_pvc", "target_namespace", "target_pvc", "deployments", "pods"):
            data = snapshot()
            del data[key]
            self.assertIn("snapshot-malformed", self.issues(data), key)

    def test_wrong_container_types_fail_closed(self):
        for key in ("pv", "source_pvc", "deployments", "pods"):
            data = snapshot()
            data[key] = "not-an-object"
            self.assertFalse(self.inspect(data)["ready"], key)
        self.assert_fails_with("snapshot-malformed", [])

    def test_unusable_expected_object(self):
        # Passed directly: None must reach the core, not select the fixture.
        for broken in (None, [], {}, {"pv_name": "pv-data"}):
            self.assertEqual(inspect_snapshot(snapshot(), broken), {"ready": False, "issues": ["expected-invalid"]})

    # ------------------------------------------------------ purity and reporting --

    def test_inputs_are_not_mutated(self):
        data, wanted = snapshot(), expected()
        before = copy.deepcopy((data, wanted))
        self.inspect(data, wanted)
        self.assertEqual((data, wanted), before)

    def test_report_never_leaks_raw_identity_or_secret_marker(self):
        data = snapshot()
        data["pods"] = [pod("src", "data")]
        data["pv"]["spec"]["csi"]["volumeHandle"] = SECRET_MARKER
        report = self.inspect(data)
        rendered = repr(report)
        self.assertNotIn(SECRET_MARKER, rendered)
        self.assertNotIn("pv-uid", rendered)
        self.assertNotIn("provider", rendered.lower())
        self.assertEqual(sorted(report), ["issues", "ready"])
        self.assertIsInstance(report["ready"], bool)
        for code in report["issues"]:
            self.assertRegex(code, r"^[a-z]+(-[a-z]+)*$")



    # Parent review regressions: missing agreement is not evidence of safety.
    def test_missing_both_access_modes_is_not_agreement(self):
        data = snapshot()
        del data["pv"]["spec"]["accessModes"]
        del data["source_pvc"]["spec"]["accessModes"]
        self.assert_fails_with("access-modes-mismatch", data)

    def test_invalid_access_mode_member_is_not_silently_dropped(self):
        data = snapshot()
        data["pv"]["spec"]["accessModes"].append(None)
        self.assert_fails_with("access-modes-mismatch", data)

    def test_missing_all_capacity_is_not_agreement(self):
        data = snapshot()
        del data["pv"]["spec"]["capacity"]
        del data["source_pvc"]["status"]["capacity"]
        del data["source_pvc"]["spec"]["resources"]
        self.assert_fails_with("capacity-mismatch", data)

    def test_resize_request_cannot_be_hidden_by_old_status_capacity(self):
        data = snapshot()
        data["source_pvc"]["spec"]["resources"]["requests"]["storage"] = "20Gi"
        self.assert_fails_with("capacity-mismatch", data)

    def test_invalid_volume_modes_do_not_default_to_filesystem(self):
        data = snapshot()
        data["pv"]["spec"]["volumeMode"] = False
        data["source_pvc"]["spec"]["volumeMode"] = False
        self.assert_fails_with("volume-mode-mismatch", data)

    def test_omitted_volume_modes_use_kubernetes_filesystem_default(self):
        data = snapshot()
        del data["pv"]["spec"]["volumeMode"]
        del data["source_pvc"]["spec"]["volumeMode"]
        self.assertTrue(self.inspect(data)["ready"])

    def test_pod_missing_namespace_cannot_evade_claim_check(self):
        data = snapshot()
        holder = pod("src", "data")
        del holder["metadata"]["namespace"]
        data["pods"] = [holder]
        self.assert_fails_with("pod-malformed", data)

    def test_valid_pod_without_volumes_is_not_a_claim_holder(self):
        data = snapshot()
        data["pods"] = [{"metadata": {"name": "no-volume", "namespace": "other"}, "spec": {}}]
        self.assertTrue(self.inspect(data)["ready"])

    def test_non_object_volume_is_not_silently_ignored(self):
        data = snapshot()
        data["pods"] = [{"metadata": {"name": "bad", "namespace": "src"}, "spec": {"volumes": [42]}}]
        self.assert_fails_with("pod-malformed", data)

    def test_missing_writer_status_is_not_evidence_of_quiescence(self):
        data = snapshot()
        data["deployments"][0]["status"] = None
        self.assert_fails_with("writer-malformed", data)

    def test_writer_must_observe_current_generation(self):
        data = snapshot()
        data["deployments"][0]["metadata"]["generation"] = 2
        self.assert_fails_with("writer-unobserved", data)

    def test_missing_observed_generation_is_not_quiescence(self):
        data = snapshot()
        del data["deployments"][0]["status"]["observedGeneration"]
        self.assert_fails_with("writer-unobserved", data)

    def test_duplicate_writer_cannot_overwrite_active_state(self):
        data = snapshot()
        running = copy.deepcopy(data["deployments"][0])
        running["status"]["replicas"] = 1
        data["deployments"].insert(0, running)
        self.assert_fails_with("writer-malformed", data)

    def test_source_and_destination_must_be_distinct(self):
        wanted = expected()
        wanted["target_namespace"] = wanted["source_namespace"]
        wanted["target_name"] = wanted["source_name"]
        self.assert_fails_with("expected-invalid", expected_data=wanted)


if __name__ == "__main__":
    unittest.main()
