"""Disposable runtime fixture for the operational log slice.

Invoked only by check-observability.sh --runtime. It runs the *rendered*
collector configuration and the *rendered* store arguments through the actual
pinned release binaries, then queries the actual store.

Substitutions are explicit and limited to what a host cannot provide:

* The `k8sobjects` receiver needs a cluster API, so the events pipeline is fed
  by a local OTLP receiver that injects the same `{type, object:{...}}` body
  shape the receiver emits. The Kubernetes watch, its authentication and its
  RBAC are NOT exercised here.
* Cluster DNS names become loopback addresses; nothing else about the rendered
  processors, filters, exporter or store flags changes.

The unmodified rendered collector config and the unmodified rendered store
arguments are validated by the pinned binaries first, so a passing ingestion
fixture cannot hide an unsupported field.

Synthetic records deliberately contain hostile markers: prompts, a bearer
token, a session id and tool output. They must be absent from the stored
records and from the query response, not merely from the dashboard.
"""

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Query syntax that is not a metric name.
JSON_FUNCTIONS = frozenset({"sum", "rate", "min", "max", "by", "clamp_min", "receiver", "job"})

# Markers that appear only in fields the slice must never persist.
HOSTILE = (
    "HOSTILE-PROMPT-MARKER",
    "HOSTILE-TOKEN-MARKER",
    "HOSTILE-SESSION-MARKER",
    "HOSTILE-TOOL-OUTPUT-MARKER",
    "HOSTILE-MESSAGE-MARKER",
    "HOSTILE-ANNOTATION-MARKER",
)


def free_ports(count):
    """Distinct loopback ports, held open together so none can repeat."""
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


@contextmanager
def process(command, log_path, env=None):
    with log_path.open("w") as log:
        child = subprocess.Popen(command, stdout=log, stderr=log, env=env)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
            # The rendered store arguments include a 15s HTTP shutdown delay,
            # so a graceful stop takes longer than a bare kill would.
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)


def wait_http(url, children, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert all(child.poll() is None for child in children), "runtime service exited; see logs"
        try:
            with urlopen(url, timeout=2):
                return
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(0.3)
    raise AssertionError(f"{url} did not become available")


def string(value):
    return {"stringValue": value}


def attributes(pairs):
    return [{"key": key, "value": string(value)} for key, value in pairs.items()]


def structured_record(reason, level, component, count=None):
    """One controlled OTLP record. Its free-text body is never read."""
    fields = {"reason": reason}
    if level is not None:
        fields["level"] = level
    if component is not None:
        fields["component"] = component
    record = {
        "timeUnixNano": str(time.time_ns()),
        "body": string("HOSTILE-BODY-MARKER"),
        "attributes": attributes(fields),
    }
    if count is not None:
        record["attributes"].append({"key": "event_count", "value": {"doubleValue": count}})
    return record


def any_value(value):
    """Encode one Event field the way the receiver emits it."""
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [
            {"key": key, "value": any_value(item)} for key, item in value.items()
        ]}}
    if isinstance(value, int):
        # OTLP/JSON encodes int64 as a string.
        return {"intValue": str(value)}
    return string(value)


def event_record(event_type, event, extra=()):
    """One `k8sobjects` log record: body is a kvlist of {type, object}."""
    values = [{"key": "type", "value": string(event_type)}]
    object_values = []
    for key, value in event.items():
        object_values.append({"key": key, "value": any_value(value)})
    values.append({"key": "object", "value": {"kvlistValue": {"values": object_values}}})
    return {
        "timeUnixNano": str(time.time_ns()),
        "body": {"kvlistValue": {"values": values}},
        "attributes": attributes(dict(extra)),
    }


def envelope(records, resource):
    return {
        "resourceLogs": [{
            "resource": {"attributes": attributes(resource)},
            "scopeLogs": [{"scope": {"name": "runtime-fixture"}, "logRecords": records}],
        }],
    }


def post(url, payload):
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            assert response.status == 200, response.status
    except HTTPError as error:
        raise AssertionError(f"{url} rejected the fixture payload: {error.read()[:400]!r}") from error


def store_query(base, query="*"):
    with urlopen(
        f"{base}/select/logsql/query?{urlencode({'query': query, 'limit': 100})}", timeout=10
    ) as response:
        return response.read().decode()


def metric_names(text):
    names = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        names.add(re.match(r"[a-zA-Z_:][a-zA-Z0-9_:]*", line).group())
    return names


def run(scratch):
    collector_binary = os.environ["OTELCOL_BIN"]
    store_binary = os.environ["VICTORIA_LOGS_BIN"]
    assert "0.140.1" in subprocess.check_output(
        [collector_binary, "--version"], text=True, timeout=15
    ), "collector binary is not the pinned 0.140.1 release"
    assert "v1.53.0" in subprocess.check_output(
        [store_binary, "--version"], text=True, timeout=15
    ), "log store binary is not the pinned v1.53.0 release"

    # 1. The rendered configuration, unmodified, through the actual parser.
    rendered = scratch / "rendered-events-collector.json"
    assert rendered.is_file(), "the rendered collector configuration was not captured"
    env = {**os.environ, "MY_POD_IP": "127.0.0.1"}
    yaml_config = scratch / "rendered-events-collector.yaml"
    assert yaml_config.is_file(), "the rendered collector configuration was not captured"
    with (scratch / "rendered-collector-validate.log").open("w") as log:
        subprocess.run(
            [collector_binary, "validate", f"--config={yaml_config}"],
            env=env, stdout=log, stderr=log, check=True, timeout=30,
        )
    print("[observability] runtime: unmodified rendered collector config passes the pinned 0.140.1 parser")

    # 2. The rendered store arguments, unmodified apart from path and address.
    store_documents = [
        document
        for document in json.loads((scratch / "victoria-logs.json").read_text())
        if document
    ]
    deployment = next(doc for doc in store_documents if doc["kind"] == "Deployment")
    store_args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--retentionPeriod=3d" in store_args, store_args
    assert "--retention.maxDiskSpaceUsageBytes=1GiB" in store_args, store_args

    children = []
    with ExitStack() as stack:
        store_port, events_port, structured_port, metrics_port, health_port = free_ports(5)
        data_path = scratch / "victoria-logs-data"
        # A previous run under the same scratch path must not be queried.
        shutil.rmtree(data_path, ignore_errors=True)
        args = [
            arg for arg in store_args
            if arg.lstrip("-").split("=", 1)[0] not in (
                "storageDataPath", "httpListenAddr", # replaced below
            )
        ]
        args.extend([
            f"-storageDataPath={data_path}",
            f"-httpListenAddr=127.0.0.1:{store_port}",
        ])
        store = stack.enter_context(
            process([store_binary, *args], scratch / "victoria-logs.log")
        )
        children.append(store)
        store_base = f"http://127.0.0.1:{store_port}"
        wait_http(f"{store_base}/health", children)
        print("[observability] runtime: rendered store arguments accepted by VictoriaLogs v1.53.0")

        # 3. The rendered collector config, with loopback endpoints only.
        relay = json.loads(rendered.read_text())
        relay["extensions"]["health_check"]["endpoint"] = f"127.0.0.1:{health_port}"
        relay["receivers"]["otlp"]["protocols"]["http"]["endpoint"] = f"127.0.0.1:{structured_port}"
        relay["receivers"]["otlp/events"] = {
            "protocols": {"http": {"endpoint": f"127.0.0.1:{events_port}"}}
        }
        relay["exporters"]["otlphttp/victoria-logs"]["logs_endpoint"] = (
            f"{store_base}/insert/opentelemetry/v1/logs"
        )
        relay["service"]["telemetry"]["metrics"]["readers"] = [{
            "pull": {"exporter": {"prometheus": {"host": "127.0.0.1", "port": metrics_port}}}
        }]
        # The watch receiver is the one input a host cannot stand up; the
        # pipeline keeps its rendered processors, filters and exporter.
        relay["service"]["pipelines"]["logs/events"]["receivers"] = ["otlp/events"]
        relay["receivers"].pop("k8sobjects")
        config_path = scratch / "operational-logs-collector.json"
        config_path.write_text(json.dumps(relay, indent=2))
        with (scratch / "operational-collector-validate.log").open("w") as log:
            subprocess.run(
                [collector_binary, "validate", f"--config={config_path}"],
                env=env, stdout=log, stderr=log, check=True, timeout=30,
            )
        collector = stack.enter_context(
            process([collector_binary, f"--config={config_path}"], scratch / "operational-collector.log", env)
        )
        children.append(collector)
        wait_http(f"http://127.0.0.1:{metrics_port}/metrics", children)

        # 4. Synthetic operational records: three that must be stored, five
        #    that must be dropped whole.
        event_records = [
            # Stored: a warning with an allowlisted reason and workload context.
            event_record("ADDED", {
                "type": "Warning", "reason": "BackOff", "count": 7,
                "involvedObject": {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"},
                "message": "HOSTILE-MESSAGE-MARKER back-off restarting failed container",
                "metadata": {"annotations": "HOSTILE-ANNOTATION-MARKER"},
                "note": "HOSTILE-TOOL-OUTPUT-MARKER",
            }, extra={"prompt": "HOSTILE-PROMPT-MARKER", "authorization": "Bearer HOSTILE-TOKEN-MARKER"}),
            # Stored: the events.k8s.io shape, with the count in series.
            event_record("MODIFIED", {
                "type": "Normal", "reason": "Scheduled", "series": {"count": 3},
                "regarding": {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"},
            }),
            # Dropped: free text pasted into `reason` (not an operational code).
            event_record("ADDED", {
                "type": "Warning", "reason": "HOSTILE-TOOL-OUTPUT-MARKER tool said hello",
                "involvedObject": {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"},
            }),
            # Dropped: a session id in a place that looks like a reason.
            event_record("ADDED", {
                "type": "Warning", "reason": "session-HOSTILE-SESSION-MARKER",
                "involvedObject": {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"},
            }),
            # Dropped: a reason outside the allowlisted alphabet entirely.
            event_record("ADDED", {
                "type": "Warning", "reason": "OOMKilled;token=HOSTILE-TOKEN-MARKER",
                "involvedObject": {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"},
            }),
            # Dropped: a severity the allowlist does not name.
            event_record("ADDED", {
                "type": "HOSTILE-SESSION-MARKER", "reason": "BackOff",
                "involvedObject": {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"},
            }),
        ]
        structured_records = [
            # Stored: a structured operational record from a workload.
            structured_record("queueDepth", "WARN", "gateway", count=2),
            # Dropped: no level, so this is not a controlled operational record.
            structured_record("queueDepth", None, "gateway"),
            # Dropped: an unallowlisted application attribute rides along.
            structured_record("backlog", "ERROR", "Gateway With Spaces", count=1),
        ]

        post(f"http://127.0.0.1:{events_port}/v1/logs", envelope(
            event_records, {"k8s.namespace.name": "agents"}
        ))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            structured_records, {"k8s.namespace.name": "agents", "service.name": "gateway"}
        ))

        # 5. Query the actual store. Three records pass the allowlist.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert all(child.poll() is None for child in children), "runtime service exited; see logs"
            stored = store_query(store_base)
            if len(stored.splitlines()) >= 3:
                break
            time.sleep(0.5)
        else:
            metrics = urlopen(
                f"http://127.0.0.1:{metrics_port}/metrics", timeout=10
            ).read().decode()
            counters = [
                line for line in metrics.splitlines()
                if line.startswith(("otelcol_receiver_accepted_log_records_total{",
                                    "otelcol_processor_filter_logs_filtered_total{",
                                    "otelcol_exporter_sent_log_records_total{",
                                    "otelcol_exporter_send_failed_log_records_total{"))
            ]
            raise AssertionError(
                f"the store received no operational record: {' | '.join(counters)}"
            )

        records = [json.loads(line) for line in stored.splitlines() if line.strip()]
        assert len(records) == 3, records

        # Positives: the operational fields survived with useful context.
        # Events carry `event_reason`; structured records carry `reason`.
        by_reason = {
            record.get("event_reason") or record["reason"]: record for record in records
        }
        assert set(by_reason) == {"BackOff", "Scheduled", "queueDepth"}, by_reason
        backoff = by_reason["BackOff"]
        assert backoff["event_type"] == "ADDED"
        assert backoff["event_severity"] == "Warning"
        assert backoff["object_kind"] == "Pod"
        assert backoff["object_name"] == "bub-7d9f"
        assert backoff["event_namespace"] == "agents"
        assert backoff["event_count"] == "7"
        assert backoff["severity_text"] == "WARN"
        # The composed body is the allowlisted identity, not the original text.
        assert backoff["_msg"] == "event ADDED Pod agents bub-7d9f BackOff", backoff["_msg"]
        scheduled = by_reason["Scheduled"]
        assert scheduled["severity_text"] == "INFO"
        # The events.k8s.io count moved out of `series` into a plain field.
        assert scheduled["event_count"] == "3", scheduled
        structured = by_reason["queueDepth"]
        assert structured["level"] == "WARN"
        assert structured["component"] == "gateway"
        assert structured["event_count"] == "2"
        assert structured["severity_text"] == "WARN"
        assert structured["_msg"] == "event WARN gateway agents queueDepth", structured["_msg"]

        # Negatives: no hostile marker, no raw body, no unknown attribute key.
        allowed_keys = {
            "_msg", "_stream", "_stream_id", "_time", "severity_number", "severity_text",
            "scope.name", "scope.version", "k8s.namespace.name", "service.name",
            "event_type", "event_severity", "event_reason", "event_count",
            "object_kind", "object_name", "event_namespace", "level", "component",
            "reason",
        }
        for record in records:
            assert set(record) <= allowed_keys, set(record) - allowed_keys
        for marker in HOSTILE:
            assert marker not in stored, marker
        for forbidden_key in ("prompt", "authorization", "session_id", "tool_output", "message", "note"):
            assert all(forbidden_key not in record for record in records), forbidden_key
        # A rejected free-text reason must not survive as a substring anywhere,
        # including in a dashboard-visible stream label.
        assert "tool said hello" not in stored

        # The dropped records are counted, not echoed: this is the privacy
        # decision made observable, and any record whose reason is free text is
        # included in that count.
        metrics = urlopen(f"http://127.0.0.1:{metrics_port}/metrics", timeout=10).read().decode()
        filtered = sum(
            float(line.rsplit(" ", 1)[1])
            for line in metrics.splitlines()
            if line.startswith("otelcol_processor_filter_logs_filtered_total{")
        )
        assert filtered >= 6, filtered
        accepted = sum(
            float(line.rsplit(" ", 1)[1])
            for line in metrics.splitlines()
            if line.startswith("otelcol_receiver_accepted_log_records_total{")
        )
        assert accepted == 9, accepted  # six Event records plus three structured

        # The store's own request counter recorded the ingest path the rendered
        # exporter uses, so the dashboard's store panel is not invented.
        store_metrics = urlopen(f"{store_base}/metrics", timeout=10).read().decode()
        ingest = re.search(
            r'^vl_http_requests_total\{path="/insert/opentelemetry/v1/logs".*\} (\d+)$',
            store_metrics, re.MULTILINE,
        )
        assert ingest and int(ingest.group(1)) >= 2, ingest

        # Every expression in the published operational-log dashboard names a
        # metric one of these two binaries actually exports.
        expressions = json.loads((scratch / "log-queries.json").read_text())
        exposed = metric_names(metrics) | metric_names(store_metrics)
        for expression in expressions:
            # Drop selectors first: neither `[5m]` nor `{path="..."}` is a
            # metric name, and a label value must not be mistaken for one.
            bare = re.sub(r"\[[^]]*\]|\{[^}]*\}", "", expression)
            for name in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", bare):
                if name in JSON_FUNCTIONS:
                    continue
                assert name in exposed, (name, expression)

        (scratch / "operational-records.json").write_text(json.dumps(records, indent=2))
        print(
            "[observability] runtime: rendered collector -> VictoriaLogs v1.53.0; "
            f"{len(records)} allowlisted records stored, {int(filtered)} hostile records dropped, "
            "no marker or free text in stored data or query results"
        )
    assert all(child.poll() is not None for child in children)
    print("[observability] runtime: exact owned subprocesses terminated and waited")


if __name__ == "__main__":
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
            for path in [*scratch.glob("*.log"), scratch / "operational-records.json"]:
                if path.is_file():
                    shutil.copy2(path, target / path.name)
