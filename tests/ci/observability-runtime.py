"""Disposable runtime fixture, invoked only by check-observability.sh --runtime.

Exercises the rendered collector -> scrape -> VM -> dashboard queries path.
Local adaptations are explicit: mTLS replaces the fixed in-Pod SA token path,
and two loopback targets replace cluster DNS. KSM samples are synthetic, not
proof of KSM's API watches. No cluster, credentials, containers or downloads.
"""

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process(command, log_path, env=None):
    with log_path.open("w") as log:
        child = subprocess.Popen(command, stdout=log, stderr=log, env=env)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def summary(node):
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    usage = {
        "time": timestamp,
        "usageNanoCores": 250_000_000,
        "usageCoreNanoSeconds": time.monotonic_ns(),
        "availableBytes": 128 * 1024**2,
        "usageBytes": 64 * 1024**2,
        "workingSetBytes": 60 * 1024**2,
        "rssBytes": 48 * 1024**2,
        "pageFaults": 11,
        "majorPageFaults": 1,
    }
    filesystem = {
        "time": timestamp,
        "availableBytes": 20_000_000_000,
        "capacityBytes": 30_000_000_000,
        "usedBytes": 10_000_000_000,
        "inodesUsed": 10,
        "inodes": 100,
    }
    return {
        "node": {"nodeName": node, "cpu": usage, "memory": usage, "fs": filesystem},
        "pods": [{
            "podRef": {"name": f"bub-{node[-1]}", "namespace": "agents", "uid": f"synthetic-uid-{node}"},
            "startTime": timestamp,
            "cpu": usage,
            "memory": usage,
            "volume": [{"name": name, **filesystem} for name in ("workspace", "scratch")],
            "containers": [{"name": "app", "startTime": timestamp, "cpu": usage, "memory": usage, "rootfs": filesystem}],
            "annotations": {"session": "DO-NOT-CAPTURE-PROMPT"},
        }],
    }


def state_metrics():
    # A small representative KSM exposition, not a replacement KSM process.
    samples = []
    for number in (1, 2):
        labels = f'namespace="agents",pod="bub-{number}",uid="synthetic-uid-{number}"'
        samples.extend([
            f'kube_pod_owner{{{labels},owner_kind="ReplicaSet",owner_name="bub-abc",owner_is_controller="true"}} 1',
            f'kube_pod_container_resource_requests{{{labels},container="app",resource="cpu",unit="core"}} 0.5',
            f'kube_pod_container_resource_requests{{{labels},container="app",resource="memory",unit="byte"}} 134217728',
            f'kube_pod_container_status_restarts_total{{{labels},container="app"}} 2',
            f'kube_pod_container_status_last_terminated_reason{{{labels},container="app",reason="OOMKilled"}} 1',
            f'kube_pod_status_scheduled{{{labels},condition="false"}} 0',
            f'kube_pod_status_phase{{{labels},phase="Pending"}} 0',
            f'kube_pod_status_ready{{{labels},condition="true"}} 1',
        ])
    samples.extend([
        'kube_replicaset_owner{namespace="agents",replicaset="bub-abc",owner_kind="Deployment",owner_name="bub",owner_is_controller="true"} 1',
        'kube_deployment_spec_replicas{namespace="agents",deployment="gateway"} 1',
        'kube_deployment_status_replicas_available{namespace="agents",deployment="gateway"} 1',
        'kube_node_status_allocatable{node="synthetic-1",resource="cpu",unit="core"} 2',
        'kube_node_status_allocatable{node="synthetic-2",resource="cpu",unit="core"} 2',
    ])
    return ("\n".join(samples) + "\n").encode()


@contextmanager
def fixture_server(node, cert, key):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path == "/stats/summary":
                body = json.dumps(summary(node)).encode()
                content_type = "application/json"
            elif self.path == "/metrics":
                body = state_metrics()
                content_type = "text/plain; version=0.0.4"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.load_verify_locations(cert)
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def query(base_url, expression):
    with urlopen(f"{base_url}/api/v1/query?{urlencode({'query': expression})}", timeout=2) as response:
        payload = json.load(response)
    assert payload["status"] == "success", payload
    return payload["data"]["result"]


def run(scratch):
    collector_binary = os.environ["OTELCOL_BIN"]
    vm_binary = os.environ["VICTORIA_METRICS_BIN"]
    assert "0.140.1" in subprocess.check_output([collector_binary, "--version"], text=True, timeout=10)
    assert "v1.155.0" in subprocess.check_output([vm_binary, "--version"], text=True, timeout=10)
    config = json.loads((scratch / "collector.json").read_text())
    env = {**os.environ, "MY_POD_IP": "127.0.0.1", "K8S_NODE_NAME": "synthetic-1"}
    # Ephemeral local certificate only, never an existing key or trust store.
    cert, key = scratch / "fixture.crt", scratch / "fixture.key"
    with (scratch / "certificate.log").open("w") as log:
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], stdout=log, stderr=log, check=True, timeout=15)

    vm_port = free_port()
    scrape = json.loads((scratch / "scrape.json").read_text())
    scrape["global"].update(scrape_interval="1s", scrape_timeout="1s")
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    # Only workload metrics jobs are exercised; no unrelated Gatus emulator.
    scrape["scrape_configs"] = [jobs["kubelet"], jobs["kube-state-metrics"]]
    paths = []
    children = []
    with ExitStack() as stack:
        targets = []
        for number in (1, 2):
            fixture_port, requests = stack.enter_context(fixture_server(f"synthetic-{number}", cert, key))
            paths.append(requests)
            local = json.loads(json.dumps(config))
            receiver = local["receivers"]["kubeletstats"]
            receiver.update(auth_type="tls", endpoint=f"https://127.0.0.1:{fixture_port}", ca_file=str(cert), cert_file=str(cert), key_file=str(key), collection_interval="1s")
            metrics_port = free_port()
            local["exporters"]["prometheus"]["endpoint"] = f"127.0.0.1:{metrics_port}"
            local["extensions"]["health_check"]["endpoint"] = f"127.0.0.1:{free_port()}"
            path = scratch / f"collector-{number}.json"
            path.write_text(json.dumps(local))
            with (scratch / f"validate-{number}.log").open("w") as log:
                subprocess.run([collector_binary, "validate", f"--config={path}"], env=env, stdout=log, stderr=log, check=True, timeout=15)
            children.append(stack.enter_context(process([collector_binary, f"--config={path}"], scratch / f"collector-{number}.log", env)))
            targets.append(f"127.0.0.1:{metrics_port}")
        jobs["kubelet"].pop("dns_sd_configs")
        jobs["kubelet"]["static_configs"] = [{"targets": targets}]
        jobs["kube-state-metrics"]["static_configs"] = [{"targets": [f"127.0.0.1:{fixture_port}"]}]
        jobs["kube-state-metrics"].update(scheme="https", tls_config={"ca_file": str(cert), "cert_file": str(cert), "key_file": str(key)})
        scrape_path = scratch / "runtime-scrape.json"
        scrape_path.write_text(json.dumps(scrape))
        vm_documents = json.loads((scratch / "victoria-metrics.json").read_text())
        deployment = next(doc for doc in vm_documents if doc and doc["kind"] == "Deployment")
        args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        replacements = {
            "httpListenAddr": f"127.0.0.1:{vm_port}",
            "storageDataPath": str(scratch / "vm-data"),
            "promscrape.config": str(scrape_path),
            "vmui.customDashboardsPath": str(scratch / "dashboards"),
        }
        args = [arg for arg in args if arg.lstrip("-").split("=", 1)[0] not in replacements]
        args.extend(f"-{name}={value}" for name, value in replacements.items())
        args.extend(["-search.latencyOffset=0s", "-search.disableCache", "-promscrape.config.strictParse"])
        children.append(stack.enter_context(process([vm_binary, *args], scratch / "vm.log")))
        base = f"http://127.0.0.1:{vm_port}"
        deadline = time.monotonic() + 45
        expressions = json.loads((scratch / "workload-queries.json").read_text())
        while time.monotonic() < deadline:
            assert all(child.poll() is None for child in children), "runtime service exited; see logs"
            try:
                results = {expression: query(base, expression) for expression in expressions}
                nodes = query(base, "count by (node) (k8s_node_memory_working_set_bytes)")
                if len(nodes) == 2 and all(results.values()):
                    break
            except (URLError, TimeoutError):
                pass
            time.sleep(0.5)
        else:
            raise AssertionError(f"nonempty dashboard queries not ready: {results if 'results' in locals() else 'VM unavailable'}")
        assert {result["metric"]["node"] for result in nodes} == {"synthetic-1", "synthetic-2"}
        for expression, expected in (
            ("sum(k8s_node_cpu_usage)", 0.5),
            ("sum(k8s_pod_cpu_usage)", 0.5),
            ("sum(container_cpu_usage)", 0.5),
            ("sum(k8s_node_memory_working_set_bytes)", 120 * 1024**2),
            ("sum(k8s_pod_memory_working_set_bytes)", 120 * 1024**2),
            ("sum(container_memory_working_set_bytes)", 120 * 1024**2),
            ("sum(k8s_node_filesystem_usage_bytes)", 20_000_000_000),
            ("sum(kube_pod_container_status_restarts_total)", 4),
            ("count(k8s_volume_capacity_bytes)", 4),
        ):
            actual = query(base, expression)
            assert len(actual) == 1 and float(actual[0]["value"][1]) == expected, (expression, actual, expected)
        identities = query(base, '{job=~"kubelet|kube-state-metrics"}')
        encoded = json.dumps(identities)
        for forbidden in ("DO-NOT-CAPTURE", "synthetic-uid", "k8s_pod_uid", "session", "prompt"):
            assert forbidden not in encoded, forbidden
        assert all("/stats/summary" in requests and set(requests) <= {"/stats/summary", "/metrics"} for requests in paths), paths
        (scratch / "runtime-results.json").write_text(json.dumps(results, indent=2))
        print(f"[observability] runtime: 2 actual collectors -> VM v1.155.0; {len(results)} dashboard queries nonempty; numeric, TLS, filesystem and privacy assertions passed")
    assert all(child.poll() is not None for child in children)
    print("[observability] runtime: exact owned subprocesses terminated and waited; fixture servers joined")


if __name__ == "__main__":
    # A bounded foreground caller may terminate us; unwind owned services too.
    def terminate(_signum, _frame):
        raise SystemExit("runtime fixture terminated")

    signal.signal(signal.SIGTERM, terminate)
    scratch = Path(sys.argv[1])
    try:
        run(scratch)
    finally:
        if destination := os.environ.get("OBSERVABILITY_RUNTIME_LOG_DIR"):
            target = Path(destination)
            target.mkdir(parents=True, exist_ok=True)
            for path in [*scratch.glob("*.log"), scratch / "runtime-results.json"]:
                if path.is_file():
                    shutil.copy2(path, target / path.name)
