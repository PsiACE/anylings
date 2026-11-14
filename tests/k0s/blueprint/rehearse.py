"""Fixed-target volume rebind rehearsal on a task-owned local k0s cluster.

This is the executable proof the production cutover rests on. It makes the same
decisions against a real Kubernetes API and the real PV binder, with synthetic
data and node-local volumes:

    admit -> writers -> read-only co-mount -> quiesce -> consistent backups
          -> retain -> release -> reserve -> guarded delete -> direct claimRef
          -> bind -> reverse

Every irreversible write is guarded by a `DeleteOptions` precondition or a JSON
patch `test` operation, so a wrong identity fails closed instead of applying.
Each step appends identifiers and hashes to a private journal; nothing secret,
no payload and no volume handle is printed.

It is not a DOKS rehearsal. Node-local volumes have no attach/detach phase, so
nothing here is evidence about CSI behaviour, cloud volume lifecycle or a
provider data plane.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup as backup_mod  # noqa: E402
import guard as guard_mod  # noqa: E402
import identity as identity_mod  # noqa: E402
import manifests  # noqa: E402
from api import (  # noqa: E402
    Api,
    ApiError,
    claim_path,
    pod_path,
    volume_path,
)
from cluster import Cluster, ClusterError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE = "blueprint-source"
AGENTS = "blueprint-agents"
TARGET = "blueprint-target"
PROBE = "blueprint-probe"
GATEWAY = "magpie-config"
T3 = "t3-code-home"
PROXY_PORT = 8123


class Step(Exception):
    """A failed assertion, named by a code and never by object content."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise Step(code)


class Journal:
    """Append-only private record of what this run observed and did."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "journal.jsonl"

    def write(self, step: str, **fields: object) -> None:
        entry = {"step": step, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        shown = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"[{step}] {shown}".rstrip(), flush=True)


def holders(cluster: Cluster, namespace: str, claim: str) -> list[str]:
    """Sorted Pod UIDs, terminating included, that name this claim."""
    return sorted(
        pod["metadata"]["uid"] for pod in cluster.claims_holding(namespace, claim)
    )


def write_manifest(directory: Path, name: str, document: dict) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def start_proxy(cluster: Cluster, journal: Journal) -> tuple[object, Api]:
    """One owned `kubectl proxy` on loopback, whose PID this run records."""
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            "kubectl",
            "--kubeconfig",
            cluster.kubeconfig,
            "--context",
            cluster.context,
            "proxy",
            "--address=127.0.0.1",
            f"--port={PROXY_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    api = Api(f"http://127.0.0.1:{PROXY_PORT}")
    deadline = time.time() + 60
    while True:
        try:
            api.get("/version")
            break
        except ApiError:
            require(process.poll() is None, "proxy-exited")
            require(time.time() < deadline, "proxy-not-ready")
            time.sleep(1)
    journal.write("proxy", pid=process.pid, port=PROXY_PORT)
    return process, api


def admission(cluster: Cluster, journal: Journal) -> dict:
    """Refuse a non-local target before any fixture object is created."""
    nodes = [item["metadata"]["name"] for item in cluster.json("get", "nodes")["items"]]
    namespaces = [
        item["metadata"]["name"] for item in cluster.json("get", "namespaces")["items"]
    ]
    volumes = cluster.json("get", "pv")["items"]
    snapshot = {
        "server": cluster.run(
            "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
        ).strip(),
        "context": cluster.context,
        "nodes": nodes,
        "namespaces": namespaces,
        "pv_drivers": [(pv.get("spec") or {}).get("csi", {}).get("driver") for pv in volumes],
        "pv_local": [
            bool((pv.get("spec") or {}).get("local") or (pv.get("spec") or {}).get("hostPath"))
            for pv in volumes
        ],
    }
    issues = guard_mod.inspect(snapshot, os.environ)
    journal.write("admission", issues=issues, nodes=len(nodes), namespaces=len(namespaces),
                  volumes=len(volumes))
    require(not issues, f"admission-{issues[0]}" if issues else "admission")
    return snapshot


def reconcile_own_fixtures(cluster: Cluster, journal: Journal, work: Path) -> None:
    """Remove only this fixture's own leftover Pods from an interrupted run.

    Namespaces and the owner label are both exact, so a Pod created by anything
    else is untouchable here. A rehearsal must be repeatable without a manual
    cleanup step, and without deleting an object it does not own.
    """
    # Applying the four fixture namespaces is idempotent and creates only the
    # ones this run needs, so a partial earlier run cannot leave a gap.
    cluster.apply(write_manifest(work, "namespaces", manifests.namespaces()))
    for namespace in (SOURCE, AGENTS, TARGET, PROBE):
        pods = cluster.pods_by_label(namespace, "anylings.dev/owner=blueprint-fixture")
        for pod in pods:
            cluster.run("-n", namespace, "delete", "pod", pod["metadata"]["name"],
                        "--wait=true", timeout=180)
        journal.write("reconciled", namespace=namespace, removed=len(pods))


def stage_writers(cluster: Cluster, journal: Journal, work: Path) -> dict:
    """Create fresh fixture claims and writers; prove one live writer each."""
    # A claim's volume binding is immutable, so a repeat run must reuse the
    # claim it already owns instead of re-applying a fresh request over it.
    pending = []
    for namespace, name in ((SOURCE, GATEWAY), (AGENTS, T3)):
        existing = cluster.object("pvc", namespace, name)
        if existing is None:
            pending.append(manifests.claim(namespace, name))
            continue
        owner = (existing["metadata"].get("labels") or {}).get("anylings.dev/owner")
        require(owner == "blueprint-fixture", f"claim-not-owned-{namespace}-{name}")
        journal.write("claim-reused", namespace=namespace, name=name,
                      uid=existing["metadata"]["uid"])
    if pending:
        cluster.apply(write_manifest(work, "claims", {
            "apiVersion": "v1", "kind": "List", "items": pending,
        }))
    cluster.apply(str(FIXTURES / "writer-gateway.yaml"))
    cluster.apply(str(FIXTURES / "writer-t3.yaml"))
    facts: dict[str, dict] = {}
    for key, namespace, deployment, claim in (
        ("gateway", SOURCE, "writer-gateway", GATEWAY),
        ("t3", AGENTS, "writer-t3", T3),
    ):
        deadline = time.time() + 300
        while True:
            pods = cluster.pods_by_label(
                namespace, f"app.kubernetes.io/name={deployment}"
            )
            ready = [
                pod
                for pod in pods
                if any(
                    condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                    for condition in (pod.get("status") or {}).get("conditions") or []
                )
            ]
            if ready:
                break
            require(time.time() < deadline, f"writer-not-ready-{deployment}")
            time.sleep(3)
        pod_uid, pod_name = ready[0]["metadata"]["uid"], ready[0]["metadata"]["name"]
        # The dataset must exist on the volume before it can be backed up.
        cluster.exec(namespace, pod_name, "test", "-s", "/data/records/records.ndjson")
        claim_object = cluster.object("pvc", namespace, claim)
        require(claim_object, f"claim-missing-{claim}")
        require(claim_object["status"]["phase"] == "Bound", f"claim-unbound-{claim}")
        volume = claim_object["spec"]["volumeName"]
        pv = cluster.object("pv", None, volume)
        facts[key] = {
            "namespace": namespace,
            "claim": claim,
            "claim_uid": claim_object["metadata"]["uid"],
            "pod_uid": pod_uid,
            "pod_name": pod_name,
            "pv_name": volume,
            "pv_uid": pv["metadata"]["uid"],
            "backend": identity_mod.fingerprint(identity_mod.backend(pv)[1:]),
        }
    journal.write(
        "writers-live",
        gateway_claim_uid=facts["gateway"]["claim_uid"],
        t3_claim_uid=facts["t3"]["claim_uid"],
        gateway_pv=facts["gateway"]["pv_name"],
        gateway_pv_uid=facts["gateway"]["pv_uid"],
        backend=facts["gateway"]["backend"],
    )
    return facts


def data_hash(cluster: Cluster, fact: dict) -> str:
    """Hash the live dataset through its writer Pod (writer still exists)."""
    output = cluster.exec(
        fact["namespace"], fact["pod_name"], "sha256sum", "/data/records/records.ndjson"
    )
    return output.decode().split()[0]


def stage_readers(cluster: Cluster, journal: Journal) -> dict:
    """Co-mount both claims read-only while both writers still run.

    Proving the read-only access path before anything is stopped is the point:
    the backup later depends on it. The refusal is checked in the filesystem,
    not inferred from the `readOnly` flag.
    """
    cluster.apply(str(FIXTURES / "reader.yaml"))
    readers: dict[str, dict] = {}
    for key, namespace, name in (
        ("gateway", SOURCE, "reader-gateway"),
        ("t3", AGENTS, "reader-t3"),
    ):
        cluster.wait_ready(namespace, name)
        pod = cluster.object("pod", namespace, name)
        require(pod, f"reader-missing-{name}")
        mount = pod["spec"]["containers"][0]["volumeMounts"][0]
        volume = pod["spec"]["volumes"][0]["persistentVolumeClaim"]
        require(mount.get("readOnly") is True, f"reader-mount-writable-{name}")
        require(volume.get("readOnly") is True, f"reader-claim-writable-{name}")
        listing = cluster.exec(namespace, name, "ls", "/src/records").decode()
        require("records.ndjson" in listing, f"reader-cannot-read-{name}")
        refused = write_refused(cluster, namespace, name)
        readers[key] = {"namespace": namespace, "name": name,
                        "uid": pod["metadata"]["uid"], "write_refused": refused}
        journal.write("reader-live", reader=key, uid=pod["metadata"]["uid"],
                      write_refused=refused)
    return readers


def write_refused(cluster: Cluster, namespace: str, pod: str) -> bool:
    """True only when the kernel refused the write, not when exec failed."""
    try:
        cluster.exec(namespace, pod, "touch", "/src/.blueprint-write-probe")
    except ClusterError:
        return True
    return False


def stage_quiesce(cluster: Cluster, journal: Journal, facts: dict) -> None:
    """Stop BOTH writers, then accept only the two known reader UIDs."""
    for key, namespace, deployment, claim in (
        ("gateway", SOURCE, "writer-gateway", GATEWAY),
        ("t3", AGENTS, "writer-t3", T3),
    ):
        cluster.scale(namespace, deployment, 0)
        deadline = time.time() + 240
        while True:
            observed = cluster.object("deployment", namespace, deployment)
            require(observed, f"deployment-missing-{deployment}")
            status = observed.get("status") or {}
            remaining = holders(cluster, namespace, claim)
            # Both conditions matter: a Deployment at zero replicas can still
            # have a terminating Pod that holds the claim.
            if (status.get("replicas") or 0) == 0 and facts[key]["pod_uid"] not in remaining:
                break
            require(time.time() < deadline, f"writer-not-quiesced-{deployment}")
            time.sleep(3)
        journal.write("writer-quiesced", writer=key, claim=claim,
                      removed_uid=facts[key]["pod_uid"],
                      old_uid_absent=True)
    journal.write("quiesced", writers=0)


def assert_known_holders(journal: Journal, cluster: Cluster, readers: dict) -> None:
    """Only the recorded reader UIDs may hold either claim now."""
    observed = {
        "gateway": holders(cluster, SOURCE, GATEWAY),
        "t3": holders(cluster, AGENTS, T3),
    }
    expected = {key: [readers[key]["uid"]] for key in ("gateway", "t3")}
    journal.write("holders", gateway=len(observed["gateway"]), t3=len(observed["t3"]))
    require(observed == expected, "holders-not-exactly-known-readers")


def stage_backup(
    cluster: Cluster,
    journal: Journal,
    custody: Path,
    recipient: str,
    key_file: Path,
    readers: dict,
) -> list[backup_mod.Archive]:
    """Stream both quiesced datasets to private custody and verify a restore."""
    custody.mkdir(parents=True, exist_ok=True)
    archives = []
    print(backup_mod.head(), flush=True)
    for key in ("gateway", "t3"):
        reader = readers[key]
        tar_path = custody / f"{key}.tar"
        sealed = custody / f"{key}.tar.sops"
        restored = custody / f"{key}.restored.tar"
        with tar_path.open("wb") as sink:
            finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    "kubectl",
                    "--kubeconfig",
                    cluster.kubeconfig,
                    "--context",
                    cluster.context,
                    "-n",
                    reader["namespace"],
                    "exec",
                    reader["name"],
                    "--",
                    "tar",
                    "-cf",
                    "-",
                    "-C",
                    "/src",
                    ".",
                ],
                stdout=sink,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        require(finished.returncode == 0, f"tar-stream-failed-{key}")
        archive = backup_mod.verified(
            key, tar_path, sealed, restored, recipient, key_file
        )
        print(archive.line(), flush=True)
        journal.write("backup", dataset=key, plain_bytes=archive.plain_bytes,
                      plain_sha256=archive.plain_sha256[:16],
                      sealed_bytes=archive.sealed_bytes,
                      sealed_sha256=archive.sealed_sha256[:16],
                      members=archive.members, consistent=archive.consistent)
        tar_path.unlink()
        restored.unlink()
        archives.append(archive)
    return archives


def stage_probes(cluster: Cluster, journal: Journal, api: Api, work: Path, facts: dict) -> None:
    """Fail-closed proofs: wrong UID, stale RV, wrong backend identity.

    The negative cases come first, on a throwaway claim and on the real volume
    before any policy change, so a weakened guard is visible before a correct
    operation can mask it.
    """
    claim = cluster.object("pvc", SOURCE, GATEWAY)
    require(claim, "probe-source-missing")
    cluster.apply(write_manifest(work, "probe", manifests.probe_claim()))
    probe = cluster.object("pvc", PROBE, "probe-claim")
    require(probe, "probe-object-missing")
    probe_uid = probe["metadata"]["uid"]
    probe_rv = probe["metadata"]["resourceVersion"]

    wrong = api.delete_claim(
        claim_path(PROBE, "probe-claim"), "00000000-0000-0000-0000-000000000000", probe_rv
    )
    survived = cluster.object("pvc", PROBE, "probe-claim") is not None
    journal.write("delete-wrong-uid", status=wrong, object_survived=survived)
    require(wrong == 409, f"wrong-uid-status-{wrong}")
    require(survived, "wrong-uid-removed-object")

    stale = api.delete_claim(
        claim_path(PROBE, "probe-claim"), probe_uid, "1" if probe_rv != "1" else "2"
    )
    survived = cluster.object("pvc", PROBE, "probe-claim") is not None
    journal.write("delete-stale-rv", status=stale, object_survived=survived)
    require(stale == 409, f"stale-rv-status-{stale}")
    require(survived, "stale-rv-removed-object")

    exact = api.delete_claim(claim_path(PROBE, "probe-claim"), probe_uid, probe_rv)
    gone = cluster.object("pvc", PROBE, "probe-claim") is None
    journal.write("delete-exact", status=exact, object_gone=gone)
    require(exact == 200, f"exact-delete-status-{exact}")
    require(gone, "exact-delete-left-object")

    # Wrong backend identity: the patch must be refused and change nothing.
    pv_name = facts["gateway"]["pv_name"]
    pv = cluster.object("pv", None, pv_name)
    require(pv, "probe-pv-missing")
    before_rv = pv["metadata"]["resourceVersion"]
    source = (SOURCE, GATEWAY, facts["gateway"]["claim_uid"])
    tampered = json.loads(json.dumps(pv))
    tampered["spec"]["local"] = {"path": "/nonexistent/blueprint-rehearsal"}
    refused = api.patch(volume_path(pv_name), identity_mod.retained(tampered, before_rv, source))
    after = cluster.object("pv", None, pv_name)
    journal.write("retain-wrong-identity", status=refused,
                  policy=after["spec"]["persistentVolumeReclaimPolicy"],
                  rv_unchanged=after["metadata"]["resourceVersion"] == before_rv)
    require(refused == 422, f"wrong-identity-status-{refused}")
    require(after["spec"]["persistentVolumeReclaimPolicy"] == "Delete",
            "policy-changed-despite-refusal")

    # Wrong resource version on the same patch: also refused, also unchanged.
    stale_patch = api.patch(
        volume_path(pv_name),
        identity_mod.retained(pv, "1" if before_rv != "1" else "2", source),
    )
    journal.write("retain-stale-rv", status=stale_patch)
    require(stale_patch == 422, f"stale-rv-patch-status-{stale_patch}")


def stage_retain(cluster: Cluster, journal: Journal, api: Api, facts: dict) -> None:
    """Guard, then switch the gateway volume to `Retain`. T3 stays untouched."""
    pv_name = facts["gateway"]["pv_name"]
    source = (SOURCE, GATEWAY, facts["gateway"]["claim_uid"])
    pv = cluster.object("pv", None, pv_name)
    require(pv, "retain-pv-missing")
    require(pv["spec"]["persistentVolumeReclaimPolicy"] == "Delete", "retain-policy")
    require(pv["status"]["phase"] == "Bound", "retain-phase")
    operations = identity_mod.retained(pv, pv["metadata"]["resourceVersion"], source)
    status = api.patch(volume_path(pv_name), operations)
    after = cluster.object("pv", None, pv_name)
    journal.write("retained", status=status,
                  policy=after["spec"]["persistentVolumeReclaimPolicy"],
                  pv_uid=after["metadata"]["uid"],
                  pv_uid_unchanged=after["metadata"]["uid"] == facts["gateway"]["pv_uid"],
                  claimref_uid=after["spec"]["claimRef"]["uid"],
                  claimref_kept=after["spec"]["claimRef"]["uid"] == source[2],
                  backend=identity_mod.fingerprint(identity_mod.backend(after)[1:]))
    require(status == 200, f"retain-status-{status}")
    require(after["spec"]["persistentVolumeReclaimPolicy"] == "Retain", "retain-not-applied")
    require(after["spec"]["claimRef"]["uid"] == source[2], "retain-moved-claimref")
    require(after["metadata"]["uid"] == facts["gateway"]["pv_uid"], "retain-pv-uid-changed")
    t3_pv = cluster.object("pv", None, facts["t3"]["pv_name"])
    require(t3_pv["spec"]["persistentVolumeReclaimPolicy"] == "Delete", "t3-policy-changed")


def stage_release(cluster: Cluster, journal: Journal, api: Api, readers: dict) -> None:
    """Delete the exact reader UIDs; require zero holders on both claims.

    The UID is a precondition in the DELETE body, not a comparison before an
    unguarded delete: a reader replaced in between fails with 409 instead of
    removing a Pod this run does not own.
    """
    for key, namespace, claim in (
        ("gateway", SOURCE, GATEWAY),
        ("t3", AGENTS, T3),
    ):
        reader = readers[key]
        pod = cluster.object("pod", namespace, reader["name"])
        require(pod, f"reader-already-gone-{key}")
        require(pod["metadata"]["uid"] == reader["uid"], f"reader-uid-drift-{key}")
        status = api.delete_object(
            pod_path(namespace, reader["name"]),
            reader["uid"],
            pod["metadata"]["resourceVersion"],
        )
        journal.write("reader-delete", reader=key, uid=reader["uid"], status=status)
        require(status == 200, f"reader-delete-status-{status}")
        deadline = time.time() + 180
        while holders(cluster, namespace, claim):
            require(time.time() < deadline, f"holder-remains-{claim}")
            time.sleep(3)
        journal.write("reader-gone", reader=key, remaining_holders=0)


def stage_reserve(cluster: Cluster, journal: Journal, facts: dict, work: Path) -> dict:
    """Reserve the volume with a Pending explicit-`volumeName` claim.

    The old claim reference must still be in place here: the reservation exists
    so `claimRef` can be replaced in a single step later, never so the volume can
    be left unclaimed in between.
    """
    pv_name = facts["gateway"]["pv_name"]
    pv = cluster.object("pv", None, pv_name)
    require(pv, "reserve-pv-missing")
    require(pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain", "reserve-policy")
    require(pv["spec"]["claimRef"]["uid"] == facts["gateway"]["claim_uid"], "reserve-claimref")
    cluster.apply(write_manifest(work, "target", manifests.claim(TARGET, GATEWAY, pv_name)))
    target = cluster.object("pvc", TARGET, GATEWAY)
    require(target, "reserve-target-missing")
    require(target["spec"].get("volumeName") == pv_name, "reserve-no-volume-name")
    require(target["status"]["phase"] == "Pending", "reserve-target-not-pending")
    require(not holders(cluster, TARGET, GATEWAY), "reserve-target-consumed")
    journal.write("reserved", target_uid=target["metadata"]["uid"],
                  phase=target["status"]["phase"], volume_name=pv_name,
                  old_claimref_uid=pv["spec"]["claimRef"]["uid"],
                  pv_phase=pv["status"]["phase"])
    return {"namespace": TARGET, "name": GATEWAY, "uid": target["metadata"]["uid"]}


def stage_rebind(
    cluster: Cluster, journal: Journal, api: Api, facts: dict, target: dict,
    expected_data: str, work: Path,
) -> None:
    """Delete the old claim under `Retain`, then move `claimRef` in one patch."""
    pv_name = facts["gateway"]["pv_name"]
    source_uid = facts["gateway"]["claim_uid"]
    claim = cluster.object("pvc", SOURCE, GATEWAY)
    require(claim, "rebind-source-missing")
    require(claim["metadata"]["uid"] == source_uid, "rebind-source-uid-drift")
    require(claim["status"]["phase"] == "Bound", "rebind-source-not-bound")
    require(not holders(cluster, SOURCE, GATEWAY), "rebind-source-still-held")
    pv = cluster.object("pv", None, pv_name)
    require(pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain", "rebind-policy")

    status = api.delete_claim(
        claim_path(SOURCE, GATEWAY), source_uid, claim["metadata"]["resourceVersion"]
    )
    journal.write("source-delete", status=status, uid=source_uid)
    require(status == 200, f"source-delete-status-{status}")

    deadline = time.time() + 180
    while True:
        pv = cluster.object("pv", None, pv_name)
        require(pv, "rebind-pv-vanished")
        if pv["status"]["phase"] == "Released":
            break
        require(time.time() < deadline, f"rebind-phase-{pv['status']['phase']}")
        time.sleep(2)
    require(pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain", "rebind-policy-lost")
    require(pv["metadata"]["uid"] == facts["gateway"]["pv_uid"], "rebind-pv-uid-changed")
    require(pv["spec"]["claimRef"]["uid"] == source_uid, "rebind-old-claimref-lost")
    journal.write("released", pv_uid=pv["metadata"]["uid"],
                  claimref_uid=pv["spec"]["claimRef"]["uid"],
                  backend=identity_mod.fingerprint(identity_mod.backend(pv)[1:]))

    # Negative first: a replacement that declares a different source UID is not
    # the move this step approved. It must be refused before any request, and the
    # volume must stay Released with its original claim reference.
    refused_locally = False
    try:
        identity_mod.rebound(
            pv,
            pv["metadata"]["resourceVersion"],
            (SOURCE, GATEWAY, "00000000-0000-0000-0000-000000000000"),
            (target["namespace"], target["name"], target["uid"]),
        )
    except identity_mod.IdentityError:
        refused_locally = True
    stale = api.patch(
        volume_path(pv_name),
        identity_mod.rebound(
            pv,
            "1" if pv["metadata"]["resourceVersion"] != "1" else "2",
            (SOURCE, GATEWAY, source_uid),
            (target["namespace"], target["name"], target["uid"]),
        ),
    )
    unchanged = cluster.object("pv", None, pv_name)
    journal.write("claimref-refusals", local_refusal=refused_locally, stale_status=stale,
                  phase=unchanged["status"]["phase"],
                  claimref_uid=unchanged["spec"]["claimRef"]["uid"])
    require(refused_locally, "wrong-source-not-refused-locally")
    require(stale == 422, f"claimref-stale-status-{stale}")
    require(unchanged["spec"]["claimRef"]["uid"] == source_uid, "claimref-moved-despite-refusal")
    require(unchanged["status"]["phase"] == "Released", "phase-changed-despite-refusal")

    operations = identity_mod.rebound(
        pv,
        pv["metadata"]["resourceVersion"],
        (SOURCE, GATEWAY, source_uid),
        (target["namespace"], target["name"], target["uid"]),
    )
    status = api.patch(volume_path(pv_name), operations)
    journal.write("claimref-replace", status=status, target_uid=target["uid"])
    require(status == 200, f"claimref-status-{status}")

    # The storage class is WaitForFirstConsumer, so the reservation binds when a
    # consumer appears: the verifier Pod is that consumer, and it also reads the
    # bytes that survived, which is the property actually under test.
    verify_data(cluster, journal, TARGET, GATEWAY, "forward", expected_data, work)
    bound = cluster.object("pvc", TARGET, GATEWAY)
    require(bound and bound["status"]["phase"] == "Bound", "target-not-bound")
    volume = cluster.object("pv", None, pv_name)
    journal.write("bound", pv_uid=volume["metadata"]["uid"],
                  same_pv=volume["metadata"]["uid"] == facts["gateway"]["pv_uid"],
                  claimref_uid=volume["spec"]["claimRef"]["uid"],
                  target_uid=bound["metadata"]["uid"],
                  target_uid_unchanged=bound["metadata"]["uid"] == target["uid"],
                  backend=identity_mod.fingerprint(identity_mod.backend(volume)[1:]),
                  t3_unchanged=cluster.object("pvc", AGENTS, T3)["metadata"]["uid"]
                  == facts["t3"]["claim_uid"])
    require(volume["spec"]["claimRef"]["uid"] == target["uid"], "bound-wrong-claimref")
    require(volume["metadata"]["uid"] == facts["gateway"]["pv_uid"], "bound-pv-uid-changed")
    require(volume["spec"]["persistentVolumeReclaimPolicy"] == "Retain", "bound-policy")
    require(bound["metadata"]["uid"] != facts["gateway"]["claim_uid"], "bound-uid-reused")


def verify_data(
    cluster: Cluster, journal: Journal, namespace: str, claim: str, label: str,
    expected: str, work: Path,
) -> None:
    """Hash the dataset through a fresh read-only mount, not a deleted writer.

    Exec into the writer Pod after it has been removed is exactly the
    fabrication this rehearsal must avoid, so the check mounts the same claim in
    a new Pod and reads the bytes that actually survived the rebind.
    """
    name = f"verify-{label}"
    existing = cluster.object("pod", namespace, name)
    if existing:
        cluster.run("-n", namespace, "delete", "pod", name, "--wait=true")
    cluster.apply(write_manifest(work, name, manifests.verify_pod(namespace, name, claim)))
    cluster.wait_ready(namespace, name)
    observed = cluster.exec(namespace, name, "sha256sum", "/src/records/records.ndjson")
    digest = observed.decode().split()[0]
    journal.write("data", phase=label, namespace=namespace, matches=digest == expected,
                  sha256_prefix=digest[:16])
    require(digest == expected, f"data-mismatch-{label}")
    # The verifier is a reader this run created, so it is removed by name here.
    cluster.run("-n", namespace, "delete", "pod", name, "--wait=true")


def stage_reverse(
    cluster: Cluster, journal: Journal, api: Api, facts: dict, target: dict,
    expected_data: str, work: Path,
) -> None:
    """Reverse: stop the new claimant first, then restore the source identity.

    A Git revert is not a reverse of a rebind: the data lives on the same PV, the
    destination claim object is new, and its UID cannot be restored. The reverse
    recreates the original namespace/name with a NEW UID and repoints `claimRef`
    at it under the same guards.
    """
    pv_name = facts["gateway"]["pv_name"]
    target_object = cluster.object("pvc", TARGET, GATEWAY)
    require(target_object, "reverse-target-missing")
    require(not holders(cluster, TARGET, GATEWAY), "reverse-target-still-held")

    status = api.delete_claim(
        claim_path(TARGET, GATEWAY),
        target_object["metadata"]["uid"],
        target_object["metadata"]["resourceVersion"],
    )
    journal.write("reverse-source-delete", status=status,
                  uid=target_object["metadata"]["uid"])
    require(status == 200, f"reverse-delete-status-{status}")

    deadline = time.time() + 180
    while True:
        pv = cluster.object("pv", None, pv_name)
        require(pv, "reverse-pv-missing")
        if pv["status"]["phase"] == "Released":
            break
        require(time.time() < deadline, f"reverse-phase-{pv['status']['phase']}")
        time.sleep(2)
    require(pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain", "reverse-policy-lost")
    require(pv["metadata"]["uid"] == facts["gateway"]["pv_uid"], "reverse-pv-uid-changed")

    cluster.apply(write_manifest(work, "reverse", manifests.claim(SOURCE, GATEWAY, pv_name)))
    source = cluster.object("pvc", SOURCE, GATEWAY)
    require(source, "reverse-claim-missing")
    require(source["status"]["phase"] == "Pending", "reverse-claim-not-pending")
    new_uid = source["metadata"]["uid"]
    journal.write("reverse-reserved", uid=new_uid,
                  differs_from_original=new_uid != facts["gateway"]["claim_uid"])

    operations = identity_mod.rebound(
        pv,
        pv["metadata"]["resourceVersion"],
        (TARGET, GATEWAY, target["uid"]),
        (SOURCE, GATEWAY, new_uid),
    )
    status = api.patch(volume_path(pv_name), operations)
    journal.write("reverse-claimref", status=status, source_uid=new_uid)
    require(status == 200, f"reverse-claimref-status-{status}")

    deadline = time.time() + 240
    while True:
        bound = cluster.object("pvc", SOURCE, GATEWAY)
        if bound and bound["status"]["phase"] == "Bound":
            break
        require(time.time() < deadline, "reverse-not-bound")
        time.sleep(3)
    volume = cluster.object("pv", None, pv_name)
    journal.write("reversed", pv_uid=volume["metadata"]["uid"],
                  same_pv=volume["metadata"]["uid"] == facts["gateway"]["pv_uid"],
                  claimref_uid=volume["spec"]["claimRef"]["uid"],
                  reversed_uid=bound["metadata"]["uid"],
                  original_uid=facts["gateway"]["claim_uid"],
                  backend=identity_mod.fingerprint(identity_mod.backend(volume)[1:]),
                  t3_unchanged=cluster.object("pvc", AGENTS, T3)["metadata"]["uid"]
                  == facts["t3"]["claim_uid"])
    require(volume["metadata"]["uid"] == facts["gateway"]["pv_uid"], "reversed-pv-changed")
    require(volume["spec"]["claimRef"]["uid"] == bound["metadata"]["uid"], "reversed-claimref")
    verify_data(cluster, journal, SOURCE, GATEWAY, "reverse", expected_data, work)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--custody", required=True, help="private output directory")
    parser.add_argument("--recipient", required=True, help="synthetic local age recipient")
    parser.add_argument("--key", required=True, help="synthetic local age identity file")
    parser.add_argument("--journal", default=None)
    arguments = parser.parse_args(argv)

    custody = Path(arguments.custody)
    custody.mkdir(parents=True, exist_ok=True)
    journal = Journal(Path(arguments.journal) if arguments.journal else custody)
    work = custody / "manifests"
    cluster = Cluster(arguments.kubeconfig, arguments.context)

    proxy = None
    try:
        admission(cluster, journal)
        reconcile_own_fixtures(cluster, journal, work)
        proxy, api = start_proxy(cluster, journal)
        facts = stage_writers(cluster, journal, work)
        expected = data_hash(cluster, facts["gateway"])
        journal.write("dataset-hash", phase="live", sha256_prefix=expected[:16])
        readers = stage_readers(cluster, journal)
        stage_quiesce(cluster, journal, facts)
        assert_known_holders(journal, cluster, readers)
        # The writer Pod is gone from here on: every later comparison is a mount.
        stage_backup(
            cluster, journal, custody, arguments.recipient, Path(arguments.key), readers
        )
        stage_probes(cluster, journal, api, work, facts)
        stage_retain(cluster, journal, api, facts)
        stage_release(cluster, journal, api, readers)
        target = stage_reserve(cluster, journal, facts, work)
        stage_rebind(cluster, journal, api, facts, target, expected, work)
        stage_reverse(cluster, journal, api, facts, target, expected, work)
        journal.write("complete", result="rehearsal-passed")
        return 0
    except (Step, ClusterError, ApiError, backup_mod.BackupError,
            identity_mod.IdentityError) as error:
        code = error.code if hasattr(error, "code") else str(error)
        journal.write("failed", code=code)
        print(f"FAILED {code}", file=sys.stderr)
        return 1
    finally:
        if proxy is not None and proxy.poll() is None:
            proxy.terminate()
            proxy.wait(timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())












