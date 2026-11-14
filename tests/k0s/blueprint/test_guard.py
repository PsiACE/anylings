"""Admission: the rehearsal must refuse every non-local target.

These are the checks that keep a destructive delete away from a production or
cloud cluster. Each case is a synthetic snapshot, so no cluster is needed and no
credential is read.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard  # noqa: E402

OPT_IN = {guard.LOCAL_ENV: "1"}


def local_snapshot() -> dict:
    return {
        "server": "https://127.0.0.1:6443",
        "context": "anylings-blueprint",
        "nodes": ["blueprint-k0s"],
        "namespaces": ["blueprint-source", "blueprint-agents", "kube-system"],
        "pv_drivers": [None, None],
        "pv_local": [True, True],
    }


class Admission(unittest.TestCase):
    def test_local_target_is_accepted(self) -> None:
        self.assertEqual(guard.inspect(local_snapshot(), OPT_IN), [])

    def test_missing_opt_in_flag_is_refused(self) -> None:
        self.assertIn("local-opt-in-missing", guard.inspect(local_snapshot(), {}))
        self.assertIn(
            "local-opt-in-missing",
            guard.inspect(local_snapshot(), {guard.LOCAL_ENV: "true"}),
        )

    def test_public_api_endpoint_is_refused(self) -> None:
        snapshot = local_snapshot() | {"server": "https://api.example.com:6443"}
        self.assertIn("api-not-local", guard.inspect(snapshot, OPT_IN))

    def test_private_network_endpoint_is_accepted(self) -> None:
        # A disposable guest reaches its API over its own private address.
        snapshot = local_snapshot() | {"server": "https://10.0.2.15:6443"}
        self.assertNotIn("api-not-local", guard.inspect(snapshot, OPT_IN))

    def test_missing_endpoint_is_refused(self) -> None:
        for server in ("", None, "https://"):
            with self.subTest(server=server):
                snapshot = local_snapshot() | {"server": server}
                self.assertIn("api-not-local", guard.inspect(snapshot, OPT_IN))

    def test_cloud_context_name_is_refused(self) -> None:
        for context in ("do-sgp1-anylings", "doks", "gke-prod", "eks-blue"):
            with self.subTest(context=context):
                snapshot = local_snapshot() | {"context": context}
                self.assertIn("context-names-a-cloud", guard.inspect(snapshot, OPT_IN))

    def test_blank_context_is_refused(self) -> None:
        snapshot = local_snapshot() | {"context": "   "}
        self.assertIn("context-missing", guard.inspect(snapshot, OPT_IN))

    def test_production_node_name_is_refused(self) -> None:
        snapshot = local_snapshot() | {"nodes": ["blueprint-k0s", "anylings-default-3arcgs"]}
        self.assertIn("production-node", guard.inspect(snapshot, OPT_IN))

    def test_empty_node_list_is_refused(self) -> None:
        self.assertIn("no-nodes", guard.inspect(local_snapshot() | {"nodes": []}, OPT_IN))

    def test_production_namespace_is_refused(self) -> None:
        for namespace in guard.PRODUCTION_NAMESPACES:
            with self.subTest(namespace=namespace):
                snapshot = local_snapshot() | {
                    "namespaces": ["blueprint-source", namespace]
                }
                self.assertIn("production-namespace", guard.inspect(snapshot, OPT_IN))

    def test_cloud_csi_driver_is_refused(self) -> None:
        for driver in guard.CLOUD_CSI_DRIVERS:
            with self.subTest(driver=driver):
                snapshot = local_snapshot() | {
                    "pv_drivers": [driver],
                    "pv_local": [False],
                }
                self.assertIn("cloud-csi-driver", guard.inspect(snapshot, OPT_IN))

    def test_unknown_driver_without_a_local_volume_is_refused(self) -> None:
        snapshot = local_snapshot() | {
            "pv_drivers": ["example.internal/storage"],
            "pv_local": [False],
        }
        self.assertIn("driver-not-attributable", guard.inspect(snapshot, OPT_IN))

    def test_every_issue_is_reported_not_just_the_first(self) -> None:
        snapshot = {
            "server": "https://api.example.com:6443",
            "context": "do-sgp1-anylings",
            "nodes": ["anylings-default-3arcgs"],
            "namespaces": ["gateways"],
            "pv_drivers": ["dobs.csi.digitalocean.com"],
            "pv_local": [False],
        }
        issues = guard.inspect(snapshot, {})
        self.assertEqual(
            set(issues),
            {
                "local-opt-in-missing",
                "api-not-local",
                "context-names-a-cloud",
                "production-node",
                "production-namespace",
                "cloud-csi-driver",
            },
        )

    def test_issue_codes_never_echo_a_snapshot_value(self) -> None:
        snapshot = local_snapshot() | {"nodes": ["anylings-default-secret-host"]}
        self.assertNotIn("secret-host", " ".join(guard.inspect(snapshot, OPT_IN)))


if __name__ == "__main__":
    unittest.main()
