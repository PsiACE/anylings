"""Explicit cluster access for the rehearsal driver.

Every call names one kubeconfig and one context: this module never reads a
global configuration, never mutates the current context, and never falls back
to an implicit cluster. Output is parsed JSON or a bounded text value; a failure
raises with a code instead of echoing kubectl stderr, which can contain object
content.
"""

from __future__ import annotations

import json
import os
import subprocess


class ClusterError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _verb(arguments: tuple[str, ...]) -> str:
    """The first non-flag argument, so an error code names the verb performed."""
    for argument in arguments:
        if not argument.startswith("-"):
            return argument
    return "call"


class Cluster:
    def __init__(self, kubeconfig: str, context: str) -> None:
        if not kubeconfig or not os.path.isfile(kubeconfig):
            raise ClusterError("kubeconfig-missing")
        if not context.strip():
            raise ClusterError("context-missing")
        self.kubeconfig = kubeconfig
        self.context = context

    def run(self, *arguments: str, timeout: int = 120) -> str:
        """One kubectl call, argv only, explicit kubeconfig and context."""
        command = [
            "kubectl",
            "--kubeconfig",
            self.kubeconfig,
            "--context",
            self.context,
            "--request-timeout=60s",
            *arguments,
        ]
        finished = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
        if finished.returncode != 0:
            raise ClusterError(f"kubectl-{_verb(arguments)}-{finished.returncode}")
        return finished.stdout

    def json(self, *arguments: str, optional: bool = False) -> dict | None:
        """A parsed object. `-o json` is added here so no caller can forget it."""
        try:
            output = self.run(*arguments, "-o", "json")
        except ClusterError as error:
            # Absence is a legitimate answer for an optional read, and only for
            # absence: any other failure stays an error.
            if optional and error.code.endswith("-1"):
                return None
            raise
        if not output.strip():
            raise ClusterError(f"kubectl-{arguments[0]}-empty")
        return json.loads(output)

    def object(self, kind: str, namespace: str | None, name: str) -> dict | None:
        scope = ("-n", namespace) if namespace else ()
        return self.json("get", kind, name, *scope, optional=True)

    def apply(self, path: str) -> str:
        return self.run("apply", "--server-side", "--force-conflicts", "-f", path)

    def scale(self, namespace: str, name: str, replicas: int) -> None:
        self.run("-n", namespace, "scale", f"deployment/{name}", f"--replicas={replicas}")

    def wait_ready(self, namespace: str, name: str, timeout: str = "180s") -> None:
        self.run("-n", namespace, "wait", "--for=condition=Ready", f"pod/{name}",
                 f"--timeout={timeout}")

    def exec(self, namespace: str, pod: str, *arguments: str, timeout: int = 300) -> bytes:
        """Exec returns raw bytes: a dataset stream must never be re-encoded."""
        command = [
            "kubectl", "--kubeconfig", self.kubeconfig, "--context", self.context,
            "--request-timeout=60s", "-n", namespace, "exec", pod, "--", *arguments,
        ]
        finished = subprocess.run(command, capture_output=True, timeout=timeout)
        if finished.returncode != 0:
            raise ClusterError(f"exec-{pod}-{finished.returncode}")
        return finished.stdout

    def pods_by_label(self, namespace: str, selector: str) -> list[dict]:
        listing = self.json("-n", namespace, "get", "pods", "-l", selector)
        return (listing or {}).get("items") or []

    def claims_holding(self, namespace: str, claim: str) -> list[dict]:
        """Pods, terminating included, whose volumes name this claim."""
        holders = []
        for pod in self.pods_by_label(namespace, "") or []:
            volumes = (pod.get("spec") or {}).get("volumes") or []
            if any((v.get("persistentVolumeClaim") or {}).get("claimName") == claim
                   for v in volumes):
                holders.append(pod)
        return holders
