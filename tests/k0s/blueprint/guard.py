"""Refuse to rehearse anywhere but a task-owned local cluster.

The rehearsal deletes claims, patches claim references and rebinds volumes. It
must therefore never run against a production or cloud cluster. This module is
the single admission decision: a caller supplies a small snapshot and gets a
closed list of issue codes back, never an exception with object content.

The checks are deliberately narrow and local. They do not attempt to prove that
a cluster is "safe"; they reject the specific identities this repository's
production and cloud backends have, and require an explicit opt-in flag so an
ordinary developer shell cannot run the driver by accident.
"""

from __future__ import annotations

import ipaddress

LOCAL_ENV = "ANYLINGS_BLUEPRINT_LOCAL"

# CSI drivers of the supported cloud backends. A local rehearsal has none of
# them: local-path volumes carry `spec.local` instead of `spec.csi`.
CLOUD_CSI_DRIVERS = (
    "dobs.csi.digitalocean.com",
    "pd.csi.storage.gke.io",
    "ebs.csi.aws.com",
    "disk.csi.azure.com",
    "file.csi.azure.com",
    "csi.vsphere.vmware.com",
)

# Names that only the deployed platform has. Their presence means this is not a
# disposable fixture cluster.
PRODUCTION_NAMESPACES = ("gateways", "argo-system")
PRODUCTION_NODE_PREFIXES = ("anylings-default-",)


def _host(server: str | None) -> str | None:
    """The host part of an API server URL, without a port or path."""
    if not isinstance(server, str) or not server.strip():
        return None
    rest = server.split("://", 1)[-1].split("/", 1)[0]
    if rest.startswith("["):  # bracketed IPv6 literal
        return rest[1 : rest.find("]")] if "]" in rest else None
    return rest.rsplit(":", 1)[0] if ":" in rest else rest


def _is_local_host(host: str | None) -> bool:
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def inspect(snapshot: dict, env: dict) -> list[str]:
    """Issue codes for a snapshot; an empty list is the only accepted answer.

    ``snapshot`` keys: ``server``, ``context``, ``nodes`` (names),
    ``namespaces`` (names), ``pv_drivers`` (CSI driver strings, blanks
    allowed), ``pv_local`` (booleans, true when a PV is node-local). Only
    ``env`` is read for the opt-in flag, so a misconfigured cluster cannot
    enable itself.
    """
    issues: list[str] = []
    if env.get(LOCAL_ENV) != "1":
        issues.append("local-opt-in-missing")

    server = snapshot.get("server")
    if not _is_local_host(_host(server)):
        issues.append("api-not-local")

    context = snapshot.get("context")
    if not isinstance(context, str) or not context.strip():
        issues.append("context-missing")
    elif any(marker in context for marker in ("do-sgp", "doks", "gke", "eks")):
        issues.append("context-names-a-cloud")

    nodes = snapshot.get("nodes") or []
    if not nodes:
        issues.append("no-nodes")
    if any(
        isinstance(name, str) and name.startswith(PRODUCTION_NODE_PREFIXES)
        for name in nodes
    ):
        issues.append("production-node")

    namespaces = set(snapshot.get("namespaces") or [])
    if namespaces.intersection(PRODUCTION_NAMESPACES):
        issues.append("production-namespace")

    drivers = [driver for driver in snapshot.get("pv_drivers") or [] if driver]
    if any(driver in CLOUD_CSI_DRIVERS for driver in drivers):
        issues.append("cloud-csi-driver")

    # A local cluster has local volumes; a cloud one has CSI volumes. A mixed
    # set is not this rehearsal's target either.
    local = snapshot.get("pv_local") or []
    if any(driver and driver not in CLOUD_CSI_DRIVERS for driver in drivers) and not any(local):
        issues.append("driver-not-attributable")
    return issues
