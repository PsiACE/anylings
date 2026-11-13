"""Tests for the read-only retained-PV operator preflight.

Every kubectl call is mocked: no cluster, no kubeconfig, no credential and no
network is touched. The fake records each invocation so the tests can assert
the transport contract itself -- explicit kubeconfig, context and request
timeout, ``get`` only, argv list without a shell -- while the readiness rules
stay covered by ``test_retained_pv.py`` against the same pure core.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SECRET_MARKER = "synthetic-marker-never-printed"
KUBECONFIG = "/tmp/synthetic-kubeconfig.conf"
CONTEXT = "synthetic-context"


def load_cli():
    """Import the entrypoint by path; its filename is a command, not a module."""
    spec = importlib.util.spec_from_file_location(
        "inspect_retained_pv", ROOT / "infra" / "platform" / "inspect-retained-pv.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()


def deployment(replicas: int = 0) -> dict:
    return {
        "metadata": {"namespace": "src", "name": "app", "generation": 1},
        "spec": {"replicas": replicas},
        "status": {"replicas": 0, "readyReplicas": 0, "availableReplicas": 0, "observedGeneration": 1},
    }


def objects() -> dict:
    """Cluster contents keyed by (kind, namespace, name); None means absent."""
    return {
        ("pv", "", "pv-data"): {
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
        ("pvc", "src", "data"): {
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
        ("namespace", "", "dst"): {"metadata": {"name": "dst"}},
        ("pvc", "dst", "data"): None,
        ("deployment", "src", "app"): deployment(),
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


def pod(namespace: str, claim: str, terminating: bool = False) -> dict:
    metadata = {"namespace": namespace, "name": f"{claim}-pod"}
    if terminating:
        metadata["deletionTimestamp"] = "2026-11-13T00:00:00Z"
    return {
        "metadata": metadata,
        "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]},
    }


class FakeKubectl:
    """Stand-in for the process seam: records argv, returns JSON or a failure."""

    def __init__(self, contents: dict | None = None, pods: dict | None = None) -> None:
        self.contents = objects() if contents is None else contents
        self.pods = {} if pods is None else pods
        self.calls: list[list[str]] = []
        self.failure: subprocess.CompletedProcess[str] | None = None

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        assert isinstance(arguments, list), "transport must pass an argv list, never a shell string"
        self.calls.append(list(arguments))
        if self.failure is not None:
            return self.failure
        command = arguments[arguments.index("get") + 1 :]
        kind, name = command[0], command[1]
        namespace = ""
        if "-n" in command:
            namespace = command[command.index("-n") + 1]
        if kind == "pods":
            items = self.pods.get(namespace, [])
            return self.done(json.dumps({"items": items}))
        found = self.contents.get((kind, namespace, name))
        return self.done("" if found is None else json.dumps(found))

    @staticmethod
    def done(stdout: str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["kubectl"], code, stdout, stderr)


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.expected_path = Path(self.directory.name) / "expected.json"
        self.write_expected(expected())

    def write_expected(self, payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.expected_path.write_text(text, encoding="utf-8")

    def run_cli(self, fake: FakeKubectl, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--kubeconfig", KUBECONFIG, "--context", CONTEXT, "--expected", str(self.expected_path)]
        with redirect_stdout(out), redirect_stderr(err):
            try:
                status = cli.main([*argv, *extra], invoke=fake)
            except SystemExit as exit_signal:  # argparse rejects an unknown flag
                status = int(exit_signal.code or 0)
        return status, out.getvalue(), err.getvalue()

    # ------------------------------------------------------------ transport --

    def test_every_call_is_an_explicit_read_only_get(self):
        fake = FakeKubectl()
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 0, out)
        self.assertTrue(fake.calls)
        mutating = {"apply", "create", "delete", "edit", "patch", "replace", "scale", "annotate", "label"}
        for arguments in fake.calls:
            self.assertIn("--kubeconfig", arguments)
            self.assertEqual(arguments[arguments.index("--kubeconfig") + 1], KUBECONFIG)
            self.assertEqual(arguments[arguments.index("--context") + 1], CONTEXT)
            self.assertIn("--request-timeout", arguments)
            self.assertIn("get", arguments)
            self.assertFalse(mutating & set(arguments), arguments)
            self.assertNotIn("exec", arguments)
            self.assertNotIn("proxy", arguments)
            self.assertIn("--ignore-not-found", arguments)

    def test_transport_bounds_its_own_process_timeout(self):
        self.assertIsInstance(cli.PROCESS_TIMEOUT, int)
        self.assertGreater(cli.PROCESS_TIMEOUT, 0)
        self.assertTrue(cli.REQUEST_TIMEOUT.endswith("s"))

    def test_positive_snapshot_is_ready(self):
        status, out, err = self.run_cli(FakeKubectl())
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out), {"ready": True, "issues": []})
        self.assertEqual(err, "")

    def test_collects_every_snapshot_key_without_duplicating_rules(self):
        fake = FakeKubectl(pods={"src": [pod("other", "unrelated")]})
        self.run_cli(fake)
        collected = cli.collect(cli.Kubectl(KUBECONFIG, CONTEXT, invoke=fake), expected())
        self.assertEqual(set(collected), {"pv", "source_pvc", "target_namespace", "target_pvc", "deployments", "pods"})
        self.assertEqual(len(collected["deployments"]), 1)
        self.assertEqual(collected["target_pvc"], None)

    # -------------------------------------------------------------- not ready --

    def test_missing_target_namespace_reports_not_ready(self):
        contents = objects()
        contents[("namespace", "", "dst")] = None
        status, out, _ = self.run_cli(FakeKubectl(contents))
        self.assertEqual(status, 1)
        self.assertIn("target-namespace-missing", json.loads(out)["issues"])

    def test_existing_foreign_target_claim_is_rejected(self):
        contents = objects()
        contents[("pvc", "dst", "data")] = {
            "metadata": {"namespace": "dst", "name": "data", "uid": "unowned"},
            "spec": {"volumeName": "pv-data"},
            "status": {"phase": "Bound"},
        }
        status, out, _ = self.run_cli(FakeKubectl(contents))
        self.assertEqual(status, 1)
        self.assertIn("target-claim-foreign", json.loads(out)["issues"])

    def test_live_pod_holder_is_rejected(self):
        status, out, _ = self.run_cli(FakeKubectl(pods={"src": [pod("src", "data", terminating=True)]}))
        self.assertEqual(status, 1)
        self.assertIn("pod-references-claim", json.loads(out)["issues"])

    def test_nonzero_writer_is_rejected(self):
        contents = objects()
        contents[("deployment", "src", "app")] = deployment(replicas=1)
        status, out, _ = self.run_cli(FakeKubectl(contents))
        self.assertEqual(status, 1)
        self.assertIn("writer-not-quiesced", json.loads(out)["issues"])

    def test_core_guards_still_decide_readiness(self):
        # A transport cannot make a wrong CSI handle ready: the core rejects it.
        wanted = expected()
        wanted["csi_handle"] = "different-handle"
        self.write_expected(wanted)
        status, out, _ = self.run_cli(FakeKubectl())
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out), {"ready": False, "issues": ["csi-handle-mismatch"]})

    # --------------------------------------------------------------- failures --

    def test_unknown_execute_flag_is_rejected_before_any_call(self):
        for flag in ("--execute", "--delete", "--apply"):
            fake = FakeKubectl()
            status, out, err = self.run_cli(fake, flag)
            self.assertEqual(status, 2, flag)
            self.assertEqual(out, "")
            self.assertEqual(fake.calls, [], flag)
            self.assertIn("arguments-invalid", err.lower())

    def test_forbidden_permission_error_is_sanitized(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done("", 1, "Error from server (Forbidden): pods is forbidden: " + SECRET_MARKER)
        status, out, err = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-forbidden"})
        for stream in (out, err):
            self.assertNotIn(SECRET_MARKER, stream)
            self.assertNotIn("Forbidden", stream)

    def test_timeout_is_sanitized(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done("", 1, "Unable to connect to the server: dial tcp: i/o timeout")
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-timeout"})

    def test_connection_refused_query_timeout_is_not_a_timeout_error(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done("", 1, 'Get "https://127.0.0.1:1/api?timeout=10s": dial tcp: connect: connection refused')
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-unavailable"})

    def test_unresolvable_host_query_timeout_is_not_a_timeout_error(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done("", 1, 'Get "https://api.invalid.example/api?timeout=10s": lookup api.invalid.example: no such host')
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-unavailable"})

    def test_actual_io_timeout_remains_a_timeout_error(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done("", 1, 'Get "https://api.invalid.example/api?timeout=10s": dial tcp: i/o timeout')
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-timeout"})

    def test_subprocess_timeout_is_a_transport_error(self):
        def explode(_arguments):
            raise subprocess.TimeoutExpired("kubectl", cli.PROCESS_TIMEOUT)

        out = io.StringIO()
        argv = ["--kubeconfig", KUBECONFIG, "--context", CONTEXT, "--expected", str(self.expected_path)]
        with redirect_stdout(out):
            status = cli.main(argv, invoke=explode)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out.getvalue()), {"error": "transport-timeout"})

    def test_bad_json_body_is_sanitized(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done("{not json " + SECRET_MARKER)
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-response-invalid"})

    def test_list_body_without_items_is_sanitized(self):
        fake = FakeKubectl()
        fake.failure = FakeKubectl.done(json.dumps({"kind": "PodList"}))
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "api-response-invalid"})

    def test_unreadable_expected_file_is_an_input_error(self):
        for payload in ("{not json", "[]"):
            self.write_expected(payload)
            fake = FakeKubectl()
            status, out, _ = self.run_cli(fake)
            self.assertEqual(status, 2, payload)
            self.assertEqual(json.loads(out), {"error": "expected-invalid"}, payload)
            self.assertEqual(fake.calls, [], payload)
        self.expected_path.unlink()
        status, out, _ = self.run_cli(FakeKubectl())
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "expected-invalid"})

    def test_expected_without_required_identity_stops_before_transport(self):
        wanted = expected()
        del wanted["source_uid"]
        self.write_expected(wanted)
        fake = FakeKubectl()
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "expected-invalid"})
        self.assertEqual(fake.calls, [])

    def test_malformed_writer_identity_stops_before_transport(self):
        wanted = expected()
        wanted["writers"] = [{"namespace": "src"}]
        self.write_expected(wanted)
        fake = FakeKubectl()
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "expected-invalid"})
        self.assertEqual(fake.calls, [])

    # ---------------------------------------------------------------- reports --

    def test_report_never_leaks_a_handle_or_raw_object(self):
        contents = objects()
        contents[("deployment", "src", "app")] = deployment(replicas=1)
        status, out, err = self.run_cli(FakeKubectl(contents, pods={"src": [pod("src", "data")]}))
        self.assertEqual(status, 1)
        report = json.loads(out)
        self.assertEqual(sorted(report), ["issues", "ready"])
        self.assertEqual(set(report["issues"]), {"pod-references-claim", "writer-not-quiesced"})
        for stream in (out, err):
            self.assertNotIn(SECRET_MARKER, stream)
            self.assertNotIn("pv-uid", stream)
            self.assertNotIn("claim-uid", stream)
            self.assertNotIn("persistentVolumeReclaimPolicy", stream)

    def test_collection_does_not_mutate_expected(self):
        wanted = expected()
        before = copy.deepcopy(wanted)
        cli.collect(cli.Kubectl(KUBECONFIG, CONTEXT, invoke=FakeKubectl()), wanted)
        self.assertEqual(wanted, before)



    def test_blank_explicit_scope_never_uses_a_default_cluster(self):
        for flag in ("--kubeconfig", "--context"):
            for value in ("", "  "):
                fake = FakeKubectl()
                out, err = io.StringIO(), io.StringIO()
                argv = ["--kubeconfig", KUBECONFIG, "--context", CONTEXT, "--expected", str(self.expected_path)]
                argv[argv.index(flag) + 1] = value
                with redirect_stdout(out), redirect_stderr(err):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(argv, invoke=fake)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(fake.calls, [])

    def test_expected_resource_names_cannot_inject_kubectl_flags(self):
        for key, value in (
            ("pv_name", "--context=unexpected"),
            ("source_name", "--raw=/api/v1/secrets"),
            ("target_name", "--kubeconfig=/synthetic/other"),
            ("source_namespace", "--all-namespaces"),
        ):
            wanted = expected()
            wanted[key] = value
            self.write_expected(wanted)
            fake = FakeKubectl()
            status, out, _ = self.run_cli(fake)
            self.assertEqual(status, 2, key)
            self.assertEqual(json.loads(out), {"error": "expected-invalid"})
            self.assertEqual(fake.calls, [], key)

    def test_expected_writer_name_cannot_inject_kubectl_flags(self):
        wanted = expected()
        wanted["writers"] = [{"namespace": "src", "name": "--raw=/api/v1/secrets"}]
        self.write_expected(wanted)
        fake = FakeKubectl()
        status, out, _ = self.run_cli(fake)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out), {"error": "expected-invalid"})
        self.assertEqual(fake.calls, [])

    def test_argument_errors_never_echo_unknown_secret_values(self):
        fake = FakeKubectl()
        status, out, err = self.run_cli(fake, "--token", SECRET_MARKER)
        self.assertEqual(status, 2)
        self.assertEqual(fake.calls, [])
        self.assertNotIn(SECRET_MARKER, out + err)

    def test_default_process_seam_is_bounded_and_never_uses_a_shell(self):
        fake = FakeKubectl()
        options = []
        def run(arguments, **kwargs):
            options.append(kwargs)
            return fake(arguments)
        argv = ["--kubeconfig", KUBECONFIG, "--context", CONTEXT, "--expected", str(self.expected_path)]
        with patch.object(cli.subprocess, "run", side_effect=run), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        self.assertTrue(options)
        for kwargs in options:
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertLessEqual(kwargs["timeout"], 30)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
