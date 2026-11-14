"""Transport proofs: the exact bytes the client sends, captured locally.

The rehearsal's safety rests on two facts that must not be assumed: a guarded
delete really carries `preconditions`, and a guarded patch really tests every
identity before replacing anything. Both are asserted here by pointing the same
client at a capturing loopback server, so the assertion is about the request,
not about the outcome.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api  # noqa: E402
import identity  # noqa: E402

CREDENTIAL_MARKER = "synthetic-marker-never-in-a-report"


class _Capture(BaseHTTPRequestHandler):
    """Records one request and answers with a status the test chooses."""

    recorded: list[dict] = []
    status = 200

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).recorded.append(
            {
                "method": self.command,
                "path": self.path,
                "type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        payload = b'{"ok":true}' if self.command != "DELETE" else b""
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_DELETE = _handle
    do_PATCH = _handle

    def log_message(self, *arguments: object) -> None:
        """Silence: the server's own log is not the test's output."""


class Captured(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _Capture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = api.Api(f"http://127.0.0.1:{cls.server.server_port}")
        cls.path = api.claim_path("blueprint-probe", "probe-claim")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Capture.recorded.clear()
        _Capture.status = 200

    def test_delete_preserves_both_preconditions(self) -> None:
        status = self.api.delete_claim(self.path, "uid-a", "4711")
        body = json.loads(_Capture.recorded[0]["body"])
        self.assertEqual(status, 200)
        self.assertEqual(_Capture.recorded[0]["method"], "DELETE")
        self.assertEqual(body["kind"], "DeleteOptions")
        self.assertEqual(body["preconditions"], {"uid": "uid-a", "resourceVersion": "4711"})

    def test_rejection_status_is_returned_not_raised(self) -> None:
        # A 409 is an assertion result: the caller must see it, not an exception.
        for status in (409, 422):
            _Capture.status = status
            self.assertEqual(self.api.delete_claim(self.path, "uid-a", "4711"), status)

    def test_blank_precondition_is_refused_before_any_request(self) -> None:
        for arguments in (("", "4711"), ("uid-a", "  ")):
            with self.subTest(arguments=arguments):
                with self.assertRaises(api.ApiError) as caught:
                    self.api.delete_claim(self.path, *arguments)
                self.assertIn("blank", caught.exception.code)
        self.assertEqual(_Capture.recorded, [])

    def test_endpoint_must_be_loopback(self) -> None:
        for endpoint in ("https://api.example.com:6443", "http://10.0.0.5:8001", ""):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(api.ApiError):
                    api.Api(endpoint)

    def test_patch_is_sent_as_json_patch(self) -> None:
        operations = [api.test_op("/metadata/uid", "uid-a"),
                      api.replace_op("/spec/persistentVolumeReclaimPolicy", "Retain")]
        self.api.patch(api.volume_path("pv-a"), operations)
        recorded = _Capture.recorded[0]
        self.assertEqual(recorded["method"], "PATCH")
        self.assertEqual(recorded["type"], api.JSON_PATCH)
        self.assertEqual(json.loads(recorded["body"]), operations)

    def test_patch_path_and_body_carry_no_secret(self) -> None:
        self.api.patch(api.volume_path("pv-a"), [api.test_op("/metadata/uid", "uid-a")])
        self.assertNotIn(CREDENTIAL_MARKER, _Capture.recorded[0]["body"].decode())


def local_volume() -> dict:
    return {
        "metadata": {"name": "pv-a", "uid": "pv-uid", "resourceVersion": "17"},
        "spec": {
            "local": {"path": "/var/local/blueprint/pv-a"},
            "claimRef": {"namespace": "src", "name": "data", "uid": "claim-uid"},
            "persistentVolumeReclaimPolicy": "Delete",
        },
        "status": {"phase": "Bound"},
    }


def hostpath_volume() -> dict:
    """What the local-path provisioner actually creates: a hostPath volume."""
    volume = local_volume()
    spec = dict(volume["spec"])
    spec.pop("local")
    spec["hostPath"] = {"path": "/var/local/blueprint/pv-a", "type": "DirectoryOrCreate"}
    return volume | {"spec": spec}


def csi_volume() -> dict:
    return {
        "metadata": {"name": "pv-b", "uid": "pv-uid", "resourceVersion": "17"},
        "spec": {
            "csi": {"driver": "dobs.csi.digitalocean.com", "volumeHandle": CREDENTIAL_MARKER},
            "claimRef": {"namespace": "src", "name": "data", "uid": "claim-uid"},
            "persistentVolumeReclaimPolicy": "Delete",
        },
        "status": {"phase": "Bound"},
    }


class GuardedPatches(unittest.TestCase):
    """Identity guards: the tests come before the replace, always."""

    def test_retain_tests_identity_then_replaces_policy(self) -> None:
        source = ("src", "data", "claim-uid")
        operations = identity.retained(local_volume(), "17", source)
        tests = [operation for operation in operations if operation["op"] == "test"]
        self.assertEqual(operations[-1]["op"], "replace")
        self.assertEqual(operations[-1]["path"], "/spec/persistentVolumeReclaimPolicy")
        self.assertEqual(operations[-1]["value"], "Retain")
        paths = {operation["path"] for operation in tests}
        self.assertTrue({"/metadata/uid", "/metadata/resourceVersion", "/status/phase",
                         "/spec/persistentVolumeReclaimPolicy", "/spec/claimRef/uid",
                         "/spec/local/path"} <= paths)

    def test_hostpath_backend_is_guarded_by_its_path(self) -> None:
        operations = identity.retained(hostpath_volume(), "17", ("src", "data", "claim-uid"))
        paths = {operation["path"] for operation in operations}
        self.assertIn("/spec/hostPath/path", paths)
        self.assertNotIn("/spec/local/path", paths)
        self.assertEqual(identity.backend(hostpath_volume())[0], "hostpath")

    def test_retain_tests_the_csi_handle_instead_of_a_local_path(self) -> None:
        operations = identity.retained(csi_volume(), "17", ("src", "data", "claim-uid"))
        paths = {operation["path"] for operation in operations}
        self.assertIn("/spec/csi/volumeHandle", paths)
        self.assertIn("/spec/csi/driver", paths)
        self.assertNotIn("/spec/local/path", paths)

    def test_retain_refuses_a_claimref_other_than_the_declared_source(self) -> None:
        with self.assertRaises(identity.IdentityError):
            identity.retained(local_volume(), "17", ("src", "other", "claim-uid"))

    def test_rebound_requires_released_and_moves_to_the_target_uid(self) -> None:
        volume = local_volume() | {
            "status": {"phase": "Released"},
            "spec": local_volume()["spec"] | {"persistentVolumeReclaimPolicy": "Retain"},
        }
        operations = identity.rebound(volume, "17", ("src", "data", "claim-uid"),
                                      ("dst", "data", "target-uid"))
        replacements = [operation for operation in operations if operation["op"] == "replace"]
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0]["path"], "/spec/claimRef")
        self.assertEqual(replacements[0]["value"]["uid"], "target-uid")
        self.assertEqual(replacements[0]["value"]["namespace"], "dst")
        # No clear-then-set: the claimRef is never unset, so no gap can be lost.
        self.assertFalse([operation for operation in operations
                          if operation["op"] == "remove" or operation["value"] is None])

    def test_rebound_refuses_a_bound_volume(self) -> None:
        with self.assertRaises(identity.IdentityError):
            identity.rebound(local_volume(), "17", ("src", "data", "claim-uid"),
                             ("dst", "data", "target-uid"))

    def test_backend_fingerprint_is_stable_and_hides_the_handle(self) -> None:
        first = identity.fingerprint(identity.backend(csi_volume())[1:])
        second = identity.fingerprint(identity.backend(csi_volume())[1:])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertNotIn(CREDENTIAL_MARKER, first)


if __name__ == "__main__":
    unittest.main()
