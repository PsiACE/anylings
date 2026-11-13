#!/usr/bin/env python3
"""Read-only operator preflight for a retained PersistentVolume.

This is the transport only: it collects a bounded snapshot with explicit
``kubectl get`` calls and hands it to :func:`retained_pv.inspect_snapshot`.
Every readiness rule lives in that pure core, so this file never decides
whether a volume is safe -- it only fetches and reports.

Usage::

    inspect-retained-pv.py --kubeconfig PATH --context NAME --expected FILE

``--expected`` names a private JSON file with the core's identity and writer
contract. The transport reads only that file; it never inspects, copies or
logs a kubeconfig or a credential, and it never changes the global or
environment context.

Each call passes the given ``--kubeconfig``, ``--context`` and
``--request-timeout``, runs from an argv list with no shell, and is bounded by
a subprocess timeout. Only ``kubectl get`` is used: no apply, patch, delete,
exec, proxy or client SDK, so this cannot mutate a cluster or a volume.

Output is the core report as JSON: exit 0 ready, 1 not ready, 2 input or
transport error. Errors are sanitized codes -- never raw stderr, an API
object, a UID, a volume handle or a provider message. A ``ready`` report is a
read-only precondition only: it is not permission to quiesce, rebind or resume
anything, and it is not CSI or data-migration proof.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retained_pv import inspect_snapshot  # noqa: E402

REQUEST_TIMEOUT = "10s"
PROCESS_TIMEOUT = 30
NAMESPACE_FLAG = "-n"
WRITER_FIELDS = ("namespace", "name")


class TransportError(Exception):
    """A sanitized transport failure: the code is safe to print, the cause is not."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _invoke(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """The single process seam; tests replace this, production never uses a shell."""
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _failure_code(stderr: str) -> str:
    """Map kubectl's failure text to a bounded code without echoing it."""
    lowered = stderr.lower()
    if "forbidden" in lowered or "unauthorized" in lowered:
        return "api-forbidden"
    if "timeout" in lowered or "timed out" in lowered or "deadline" in lowered:
        return "api-timeout"
    return "api-unavailable"


class Kubectl:
    """Bounded read-only client: one explicit kubeconfig, context and timeout."""

    def __init__(self, kubeconfig: str, context: str, invoke=_invoke) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        self.invoke = invoke

    def get(self, *arguments: str) -> dict | None:
        """Return the object, or None when --ignore-not-found reported absence."""
        command = [
            "kubectl",
            "--kubeconfig", self.kubeconfig,
            "--context", self.context,
            "--request-timeout", REQUEST_TIMEOUT,
            "get", *arguments,
            "-o", "json",
            "--ignore-not-found",
        ]
        try:
            process = self.invoke(command)
        except subprocess.TimeoutExpired as error:
            raise TransportError("transport-timeout") from error
        except OSError as error:
            raise TransportError("transport-unavailable") from error
        if process.returncode != 0:
            raise TransportError(_failure_code(process.stderr or ""))
        body = (process.stdout or "").strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise TransportError("api-response-invalid") from error
        if not isinstance(payload, dict):
            raise TransportError("api-response-invalid")
        return payload

    def list_items(self, *arguments: str) -> list:
        """Return a list body's items, or an empty list when nothing matched."""
        payload = self.get(*arguments)
        if payload is None:
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            raise TransportError("api-response-invalid")
        return items


def _text(value: object) -> str | None:
    """Argument-building reader only; readiness rules stay in the core."""
    return value if isinstance(value, str) and value.strip() else None


def _writer_names(writer: object) -> tuple[str, str]:
    if isinstance(writer, str) and writer.count("/") == 1:
        namespace, name = writer.split("/")
    elif isinstance(writer, dict):
        namespace, name = (writer.get(field) for field in WRITER_FIELDS)
    else:
        namespace = name = None
    if _text(namespace) is None or _text(name) is None:
        raise TransportError("expected-invalid")
    return _text(namespace), _text(name)


def _required(expected: dict, key: str) -> str:
    value = _text(expected.get(key))
    if value is None:
        raise TransportError("expected-invalid")
    return value


def collect(client: Kubectl, expected: dict) -> dict:
    """Gather exactly the six snapshot keys; absence stays None for the core."""
    names = {key: _required(expected, key) for key in ("pv_name", "source_namespace", "target_namespace")}
    source_name = _required(expected, "source_name")
    target_name = _required(expected, "target_name")
    writers = expected.get("writers")
    if not isinstance(writers, list):
        raise TransportError("expected-invalid")
    writer_names = [_writer_names(writer) for writer in writers]
    snapshot = {
        "pv": client.get("pv", names["pv_name"]),
        "source_pvc": client.get("pvc", source_name, NAMESPACE_FLAG, names["source_namespace"]),
        "target_namespace": client.get("namespace", names["target_namespace"]),
        "target_pvc": client.get("pvc", target_name, NAMESPACE_FLAG, names["target_namespace"]),
        "deployments": [],
        "pods": [],
    }
    # A missing writer Deployment is simply omitted: the core reports it.
    for namespace, name in writer_names:
        found = client.get("deployment", name, NAMESPACE_FLAG, namespace)
        if found is not None:
            snapshot["deployments"].append(found)
    namespaces = {names["source_namespace"], names["target_namespace"]}
    namespaces.update(namespace for namespace, _ in writer_names)
    for namespace in sorted(namespaces):
        snapshot["pods"].extend(client.list_items("pods", NAMESPACE_FLAG, namespace))
    return snapshot


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected", required=True)
    # No execution flag exists: an unknown one such as --execute is rejected
    # here and no cluster call is made.
    return parser.parse_args(argv)


def _load_expected(path: str) -> dict:
    try:
        expected = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransportError("expected-invalid") from error
    if not isinstance(expected, dict):
        raise TransportError("expected-invalid")
    return expected


def main(argv: list[str] | None = None, invoke=_invoke) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        expected = _load_expected(args.expected)
        client = Kubectl(args.kubeconfig, args.context, invoke=invoke)
        report = inspect_snapshot(collect(client, expected), expected)
    except TransportError as error:
        print(json.dumps({"error": error.code}, sort_keys=True))
        return 2
    if report["issues"] == ["expected-invalid"]:
        # Reporting only: the core owns the identity rules, so an expectation it
        # rejects is an input error rather than a cluster verdict.
        print(json.dumps({"error": "expected-invalid"}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
