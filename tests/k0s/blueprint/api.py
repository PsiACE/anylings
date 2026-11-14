"""Authenticated Kubernetes API transport with real request preconditions.

`kubectl delete` cannot express a `DeleteOptions` precondition and silently
drops a body given to `--raw`; a read-then-delete is not an atomic guard. This
module therefore issues the request itself, against an already authenticated
local endpoint (`kubectl proxy`), and builds the delete body explicitly.

Only three verbs are used, and each maps to one rehearsal decision:

``delete_claim``   DELETE with ``preconditions.uid`` + ``resourceVersion``
``patch_test``     JSON patch whose ``test`` operations guard every identity
``get``            a read, including post-failure survival checks

The endpoint is an injected base URL, so the offline tests point the same code
at a capturing server instead of a cluster. Nothing here reads a kubeconfig or
a credential: authentication stays in the proxy process the caller owns.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

JSON_PATCH = "application/json-patch+json"
JSON_TYPE = "application/json"
TIMEOUT = 30


class ApiError(Exception):
    """A transport failure with a code safe to log: never a body or a header."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def delete_options(uid: str, resource_version: str) -> dict:
    """The exact `DeleteOptions` body: both preconditions, nothing else.

    A blank value is rejected rather than sent, because an empty precondition is
    the unguarded delete this module exists to avoid.
    """
    for name, value in (("uid", uid), ("resourceVersion", resource_version)):
        if not isinstance(value, str) or not value.strip():
            raise ApiError(f"precondition-{name}-blank")
    return {
        "kind": "DeleteOptions",
        "apiVersion": "v1",
        "preconditions": {"uid": uid, "resourceVersion": resource_version},
    }


def test_op(path: str, value: object) -> dict:
    return {"op": "test", "path": path, "value": value}


def replace_op(path: str, value: object) -> dict:
    return {"op": "replace", "path": path, "value": value}


class Api:
    """A bounded JSON client for one explicitly supplied endpoint."""

    def __init__(self, base_url: str) -> None:
        if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
            raise ApiError("endpoint-not-loopback")
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body: dict | list | None = None,
        content_type: str = JSON_TYPE,
    ) -> tuple[int, object]:
        """One request. Returns the status even for 4xx: those are assertions."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method
        )
        if data is not None:
            request.add_header("Content-Type", content_type)
        request.add_header("Accept", JSON_TYPE)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            # A rejection is an assertion result, not an exception: return it.
            try:
                with error:
                    payload = error.read()
            except http.client.HTTPException:
                payload = b""
            return error.code, _decode(payload)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ApiError(f"transport-{type(error).__name__.lower()}") from error

    def get(self, path: str) -> dict:
        status, body = self.request("GET", path)
        if status != 200 or not isinstance(body, dict):
            raise ApiError(f"get-{status}")
        return body

    def delete_claim(self, path: str, uid: str, resource_version: str) -> int:
        """DELETE with the guarded body; the status is the assertion result."""
        status, _ = self.request("DELETE", path, delete_options(uid, resource_version))
        return status

    # The same precondition body applies to every kind: a reader is a Pod, and
    # `kubectl delete` cannot express a uid guard for Pods either.
    delete_object = delete_claim

    def patch(self, path: str, operations: list[dict], content_type: str = JSON_PATCH) -> int:
        status, _ = self.request("PATCH", path, operations, content_type)
        return status


def _decode(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def claim_path(namespace: str, name: str) -> str:
    return f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}"


def volume_path(name: str) -> str:
    return f"/api/v1/persistentvolumes/{name}"


def pod_path(namespace: str, name: str) -> str:
    return f"/api/v1/namespaces/{namespace}/pods/{name}"
