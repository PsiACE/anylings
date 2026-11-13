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

Every case below is sent with its *actual OTLP value type*: a positive integer
is `intValue`, and an adversarial string, map, array, bool or double keeps that
type. The earlier version of this fixture serialized every number as
`doubleValue`, which is exactly the shape a typed guard must reject, so it could
not have caught a missing type check.

A regression control runs the same hostile payload against the rendered
configuration with only the `filter/*_types` processors removed. That variant
must leak: if it ever stops leaking, this fixture is no longer testing the
guards, and the check fails.

Validated first, unmodified, by the pinned binaries: the rendered collector
configuration and the rendered store arguments.

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

# Markers that appear only in fields the slice must never persist.
HOSTILE = "HOSTILE-MARKER"

# Query syntax that is not a metric name.
JSON_FUNCTIONS = frozenset({"sum", "rate", "min", "max", "by", "clamp_min", "receiver", "job"})

# Keys the stored record may contain. Anything else means either a new field
# was added deliberately or a nested value was flattened into the record.
EVENT_KEYS = {
    "_msg", "_stream", "_stream_id", "_time", "severity_number", "severity_text",
    "scope.name", "scope.version", "k8s.namespace.name",
    "event_type", "event_severity", "event_reason", "event_count",
    "object_kind", "object_name", "event_namespace",
}
STRUCTURED_KEYS = {
    "_msg", "_stream", "_stream_id", "_time", "severity_number", "severity_text",
    "scope.name", "scope.version", "k8s.namespace.name", "service.name",
    "event_count", "reason", "level", "component",
}


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


# --------------------------------------------------------------- OTLP values --
# Each helper keeps the exact wire type the collector will decode, so a guard
# that only works on one type cannot pass by accident.

def int_value(number):
    # OTLP/JSON encodes int64 as a string, inside an `intValue` field.
    return Raw({"intValue": str(number)})


def double_value(number):
    return Raw({"doubleValue": number})


def bool_value(value):
    return Raw({"boolValue": value})


def string_list(values):
    return Raw({"arrayValue": {"values": [string(value) for value in values]}})


def string_map(pairs):
    return Raw({"kvlistValue": {"values": attributes(pairs)}})


class Raw:
    """A ready-made OTLP AnyValue, kept exactly as the fixture wrote it."""

    def __init__(self, value):
        self.value = value


def any_of(value):
    """Encode one Event body field, keeping nested types and ints exact."""
    if isinstance(value, Raw):
        return value.value
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [
            {"key": key, "value": any_of(item)} for key, item in value.items()
        ]}}
    if isinstance(value, int):
        return int_value(value).value
    return string(value)


def event_record(event_type, event, extra=None):
    """One `k8sobjects` log record: body is a kvlist of {type, object}."""
    record = {
        "timeUnixNano": str(time.time_ns()),
        "body": {"kvlistValue": {"values": [
            {"key": "type", "value": string(event_type)},
            {"key": "object", "value": any_of(event)},
        ]}},
    }
    if extra:
        record["attributes"] = [
            {"key": key, "value": any_of(value)} for key, value in extra.items()
        ]
    return record


def structured_record(reason, level, component=None, count=None, extra=None):
    """One controlled OTLP record. Its free-text body is never read."""
    fields = [(key, value) for key, value in (
        ("reason", reason),
        ("level", level),
        ("component", component),
        ("event_count", count),
    ) if value is not None]
    record = {
        "timeUnixNano": str(time.time_ns()),
        "body": string(f"{HOSTILE}-BODY"),
        "attributes": [{"key": key, "value": any_of(value)} for key, value in fields],
    }
    if extra:
        record["attributes"].extend(
            {"key": key, "value": any_of(value)} for key, value in extra.items()
        )
    return record


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


def store_query(base):
    with urlopen(
        f"{base}/select/logsql/query?{urlencode({'query': '*', 'limit': 200})}", timeout=10
    ) as response:
        return response.read().decode()


def metric_names(text):
    names = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        names.add(re.match(r"[a-zA-Z_:][a-zA-Z0-9_:]*", line).group())
    return names


def counter(metrics, name, **labels):
    """Sum one counters' samples, optionally only those carrying the labels."""
    total = 0.0
    for line in metrics.splitlines():
        if not line.startswith(f"{name}{{"):
            continue
        selector, _, value = line.rpartition(" ")
        if all(f'{key}="{expected}"' in selector for key, expected in labels.items()):
            total += float(value)
    return total


# ------------------------------------------------------------------ payloads --

def payloads():
    """Operational records and typed adversarial records, in wire form."""
    pod = {"kind": "Pod", "name": "bub-7d9f", "namespace": "agents"}

    events_good = [
        # Boundary integers arrive as real intValue, not doubleValue.
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff", "count": 0, "involvedObject": pod,
        }),
        event_record("MODIFIED", {
            "type": "Normal", "reason": "Scheduled", "count": 100000, "involvedObject": pod,
        }),
        # No count at all: the extract step defaults to integer 1.
        event_record("DELETED", {
            "type": "Normal", "reason": "Scheduled", "involvedObject": pod,
        }),
        # Well-typed, and carrying hostile content only in fields that must
        # never be copied. This one MUST be stored, with the markers absent.
        event_record("ADDED", {
            "type": "Warning", "reason": "Unhealthy", "count": 2, "involvedObject": pod,
            "message": f"{HOSTILE}-MESSAGE back-off restarting",
            "note": f"{HOSTILE}-NOTE",
            "metadata": {"annotations": f"{HOSTILE}-ANNOTATION"},
        }, extra={"prompt": f"{HOSTILE}-PROMPT", "authorization": f"Bearer {HOSTILE}-TOKEN"}),
    ]

    events_hostile = [
        # `event_count` present with every wrong type.
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff", "count": string(f"{HOSTILE}-COUNT-STR"),
            "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff",
            "count": string_map({"note": f"{HOSTILE}-COUNT-MAP"}), "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff",
            "count": string_list([f"{HOSTILE}-COUNT-ARR"]), "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff", "count": bool_value(True),
            "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff", "count": double_value(7.5),
            "involvedObject": pod,
        }),
        # Integers, but outside the accepted range.
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff", "count": -1, "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": "Warning", "reason": "BackOff", "count": 100001, "involvedObject": pod,
        }),
        # Required strings with the wrong type.
        event_record("ADDED", {
            "type": string_map({"x": f"{HOSTILE}-TYPE-MAP"}), "reason": "BackOff",
            "count": 1, "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": "Warning", "reason": string_list([f"{HOSTILE}-REASON-ARR"]),
            "count": 1, "involvedObject": pod,
        }),
        event_record("ADDED", {
            "type": bool_value(False), "reason": "BackOff", "count": 1, "involvedObject": pod,
        }),
        # No workload identity at all, so no useful context could be stored.
        event_record("ADDED", {"type": "Warning", "reason": "BackOff", "count": 1}),
        # Free text pasted into `reason` (a message in the wrong field).
        event_record("ADDED", {
            "type": "Warning", "reason": f"{HOSTILE}-TOOL-OUTPUT tool said hello",
            "count": 1, "involvedObject": pod,
        }),
    ]

    structured_good = [
        structured_record("queueDepth", "WARN", "gateway", int_value(2)),
        structured_record("backlog", "ERROR", None, int_value(0)),
        # No count and no component: both optional parts are omitted entirely.
        structured_record("recovered", "INFO"),
        # A reason with letters and digits is a valid operational code.
        structured_record("retryBudget", "DEBUG", "scheduler", int_value(100000)),
        # Well-typed, with unknown application attributes alongside. The
        # controlled fields must be stored and the extras must not be.
        structured_record("authFailure", "ERROR", "gateway", int_value(1), extra={
            "prompt": f"{HOSTILE}-OTLP-PROMPT",
            "session_id": f"{HOSTILE}-SESSION",
            "tool_output": f"{HOSTILE}-TOOL",
        }),
    ]

    structured_hostile = [
        structured_record("BackOff", "WARN", None, string(f"{HOSTILE}-OTLP-COUNT")),
        structured_record("BackOff", "WARN", None, string_map({"k": f"{HOSTILE}-OTLP-MAP"})),
        structured_record("BackOff", "WARN", None, string_list([f"{HOSTILE}-OTLP-ARR"])),
        structured_record("BackOff", "WARN", None, bool_value(True)),
        structured_record("BackOff", "WARN", None, double_value(3.5)),
        structured_record("BackOff", "WARN", None, int_value(-4)),
        structured_record("BackOff", "WARN", None, int_value(100001)),
        structured_record(string_map({"k": f"{HOSTILE}-OTLP-REASON"}), "WARN"),
        structured_record("BackOff", string_list(["WARN"])),
        structured_record("BackOff", "WARN", string_map({"k": f"{HOSTILE}-OTLP-COMP"})),
        # A free-text body alone must not become an operational record: without
        # reason and level this is not a controlled input at all.
        {"timeUnixNano": str(time.time_ns()), "body": string(f"{HOSTILE}-ONLY-BODY")},
    ]

    return {
        "events_good": events_good,
        "events_hostile": events_hostile,
        "structured_good": structured_good,
        "structured_hostile": structured_hostile,
    }


# ------------------------------------------------------------------- fixture --

def service_ports():
    return free_ports(5)


def collector_config(rendered, store_base, ports, with_guards):
    """Rendered collector config, with loopback endpoints only substituted."""
    relay = json.loads(json.dumps(rendered))
    store_port, events_port, structured_port, metrics_port, health_port = ports
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
    # The watch receiver is the one input a host cannot stand up; the pipeline
    # keeps its rendered processors, filters and exporter.
    relay["service"]["pipelines"]["logs/events"]["receivers"] = ["otlp/events"]
    relay["receivers"].pop("k8sobjects")
    if not with_guards:
        # Regression control: the previously reviewed configuration, i.e. the
        # same config without the type filters and with the old body that
        # concatenated the optional parts unconditionally. It must leak, or
        # this fixture no longer proves the guards do anything.
        for key in ("filter/events_types", "filter/otlp_types"):
            relay["processors"].pop(key, None)
        for pipeline in relay["service"]["pipelines"].values():
            pipeline["processors"] = [
                name for name in pipeline["processors"] if not name.endswith("_types")
            ]
        relay["processors"]["transform/otlp_shape"] = {
            "error_mode": "ignore",
            "log_statements": [{
                "context": "log",
                "statements": [
                    'set(log.body, Concat(["event", attributes["level"], attributes["component"], '
                    'resource.attributes["k8s.namespace.name"], attributes["reason"]], " "))'
                ] + [
                    statement for statement in
                    relay["processors"]["transform/otlp_shape"]["log_statements"][0]["statements"]
                    if not statement.startswith("set(log.body")
                ],
            }],
        }
    return relay


def run_fixture(binary, rendered, store_args, scratch, ports, label, with_guards):
    """Start one store and one collector, send typed payloads, return results."""
    store_binary, store_port, events_port, structured_port, metrics_port, _ = (
        binary, *ports
    )
    data_path = scratch / f"store-{label}"
    shutil.rmtree(data_path, ignore_errors=True)
    # Only the two addresses a host must move; every other rendered flag,
    # including retention and the memory budget, is passed through unchanged.
    args = [
        arg for arg in store_args
        if arg.lstrip("-").split("=", 1)[0] not in ("storageDataPath", "httpListenAddr")
    ]
    args.extend([
        f"-storageDataPath={data_path}",
        f"-httpListenAddr=127.0.0.1:{store_port}",
    ])
    store_base = f"http://127.0.0.1:{store_port}"

    children = []
    with ExitStack() as stack:
        store = stack.enter_context(
            process([store_binary, *args], scratch / f"store-{label}.log")
        )
        children.append(store)
        wait_http(f"{store_base}/health", children)

        relay = collector_config(rendered, store_base, ports, with_guards)
        config_path = scratch / f"collector-{label}.json"
        config_path.write_text(json.dumps(relay, indent=2))
        env = {**os.environ, "MY_POD_IP": "127.0.0.1"}
        with (scratch / f"collector-{label}-validate.log").open("w") as log:
            subprocess.run(
                [os.environ["OTELCOL_BIN"], "validate", f"--config={config_path}"],
                env=env, stdout=log, stderr=log, check=True, timeout=30,
            )
        collector = stack.enter_context(process(
            [os.environ["OTELCOL_BIN"], f"--config={config_path}"],
            scratch / f"collector-{label}.log", env,
        ))
        children.append(collector)
        wait_http(f"http://127.0.0.1:{metrics_port}/metrics", children)

        records = payloads()
        resource = {"k8s.namespace.name": "agents"}
        post(f"http://127.0.0.1:{events_port}/v1/logs", envelope(records["events_good"], resource))
        post(f"http://127.0.0.1:{events_port}/v1/logs", envelope(records["events_hostile"], resource))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_good"],
            {**resource, "service.name": "gateway"},
        ))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_hostile"],
            {**resource, "service.name": "gateway"},
        ))

        expected = len(records["events_good"]) + len(records["structured_good"])
        # Wait for a record from *each* pipeline, not just for a total count: a
        # count alone can be satisfied by one pipeline while the other is still
        # in flight, and the control run then misses its nil placeholder.
        deadline = time.monotonic() + 45
        stored = ""
        while time.monotonic() < deadline:
            assert all(child.poll() is None for child in children), "runtime service exited; see logs"
            stored = store_query(store_base)
            lines = [line for line in stored.splitlines() if line.strip()]
            if len(lines) >= expected and "recovered" in stored and "BackOff" in stored:
                break
            time.sleep(0.5)

        metrics_url = f"http://127.0.0.1:{metrics_port}/metrics"
        metrics = urlopen(metrics_url, timeout=10).read().decode()
        store_metrics = urlopen(f"{store_base}/metrics", timeout=10).read().decode()
        sent = store_query(store_base)
    assert all(child.poll() is not None for child in children)
    return {
        "stored": sent,
        "metrics": metrics,
        "store_metrics": store_metrics,
        "accepted": len(records["events_good"]) + len(records["events_hostile"])
        + len(records["structured_good"]) + len(records["structured_hostile"]),
    }


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
    yaml_config = scratch / "rendered-events-collector.yaml"
    assert yaml_config.is_file(), "the rendered collector configuration was not captured"
    env = {**os.environ, "MY_POD_IP": "127.0.0.1"}
    with (scratch / "rendered-collector-validate.log").open("w") as log:
        subprocess.run(
            [collector_binary, "validate", f"--config={yaml_config}"],
            env=env, stdout=log, stderr=log, check=True, timeout=30,
        )
    print("[observability] runtime: unmodified rendered collector config passes the pinned 0.140.1 parser")

    rendered = json.loads((scratch / "rendered-events-collector.json").read_text())

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

    # 3. The regression control: identical configuration, guards removed. It
    #    must reproduce the rejected leak, or the negative cases below prove
    #    nothing about the guards.
    control_ports = service_ports()
    control = run_fixture(
        store_binary, rendered, store_args, scratch, control_ports,
        "without-type-guards", with_guards=False,
    )
    control_records = [json.loads(line) for line in control["stored"].splitlines() if line.strip()]
    leaked = control["stored"]
    assert HOSTILE in leaked, (
        "the guard-less control no longer leaks, so the typed negatives below "
        "would not be measuring the guards"
    )
    leaked_keys = {
        key for record in control_records for key in record
        if key not in EVENT_KEYS and key not in STRUCTURED_KEYS
    }
    assert leaked_keys, "the guard-less control did not flatten a nested value"
    leaked_counts = {
        record["event_count"] for record in control_records if "event_count" in record
    }
    assert any(not re.fullmatch(r"-?\d+", value) for value in leaked_counts), (
        f"the guard-less control stored no non-integer count: {sorted(leaked_counts)}"
    )
    # The old shape also concatenated absent optional parts unconditionally.
    assert any("<nil>" in record["_msg"] for record in control_records), (
        "the guard-less control rendered no nil placeholder; the omission check "
        "below would then not be measuring the shape fix"
    )
    print(
        "[observability] runtime: regression control without type guards leaks "
        f"{len(control_records)} records and flattened keys {sorted(leaked_keys)}"
    )

    # 4. The rendered configuration, with the guards, must reject all of it.
    ports = service_ports()
    fixed = run_fixture(
        store_binary, rendered, store_args, scratch, ports, "rendered", with_guards=True,
    )
    records = [json.loads(line) for line in fixed["stored"].splitlines() if line.strip()]

    # Exactly the well-formed records, and no more.
    assert len(records) == 9, [record["_msg"] for record in records]

    # Reasons are distinct per case, so a collapsed or duplicated record shows.
    events = [record for record in records if "event_type" in record]
    structured = [record for record in records if "reason" in record]
    assert sorted(record["event_reason"] for record in events) == [
        "BackOff", "Scheduled", "Scheduled", "Unhealthy"], events
    assert sorted(record["reason"] for record in structured) == [
        "authFailure", "backlog", "queueDepth", "recovered", "retryBudget"], structured
    events = {record["event_reason"] + "/" + record["event_type"]: record for record in events}
    structured = {record["reason"]: record for record in structured}

    # Integer positives came in as intValue and stayed integers.
    assert events["BackOff/ADDED"]["event_count"] == "0", events["BackOff/ADDED"]
    assert events["BackOff/ADDED"]["severity_text"] == "WARN"
    assert events["Scheduled/MODIFIED"]["event_count"] == "100000", events["Scheduled/MODIFIED"]
    # The record with hostile content in uncopied fields is stored, with the
    # operational identity intact and its count still an integer.
    assert events["Unhealthy/ADDED"]["event_count"] == "2", events["Unhealthy/ADDED"]
    assert structured["authFailure"]["event_count"] == "1", structured["authFailure"]
    assert structured["authFailure"]["severity_text"] == "ERROR"
    assert structured["queueDepth"]["event_count"] == "2", structured["queueDepth"]
    assert structured["retryBudget"]["event_count"] == "100000", structured["retryBudget"]
    # The countless event defaulted to integer 1 in the store, not to a nil.
    assert events["Scheduled/DELETED"]["event_count"] == "1", events["Scheduled/DELETED"]

    # Optional parts are omitted, not rendered as `<nil>` or empty padding.
    assert structured["queueDepth"]["_msg"] == "event WARN gateway agents queueDepth", structured["queueDepth"]["_msg"]
    # A record with neither component nor namespace must not keep placeholders.
    assert structured["recovered"]["_msg"] == "event INFO agents recovered", structured["recovered"]["_msg"]
    assert "<nil>" not in fixed["stored"], "a nil placeholder reached the store"
    # And no composed body carries a doubled or trailing separator.
    for record in records:
        assert "  " not in record["_msg"], record["_msg"]
        assert record["_msg"] == record["_msg"].strip(), record["_msg"]

    # Negatives: no marker, no raw value, no flattened key, no unknown key.
    assert HOSTILE not in fixed["stored"], "a hostile marker reached the store"
    for forbidden in ("prompt", "authorization", "session_id", "tool_output",
                      "message", "note", "annotations", "metadata"):
        assert all(forbidden not in record for record in records), forbidden
    for record in records:
        allowed = EVENT_KEYS if "event_reason" in record else STRUCTURED_KEYS
        assert set(record) <= allowed, sorted(set(record) - allowed)
        # A type that leaked through the guard would appear as one of these.
        assert all("." not in key or key in allowed for key in record), record
        for key in ("event_count",):
            if key in record:
                assert re.fullmatch(r"-?\d+", record[key]), (key, record[key])

    # Rejections are counted per filter, so the drop is observable and the
    # guard is demonstrably the component that did the work.
    metrics = fixed["metrics"]
    typed = (
        counter(metrics, "otelcol_processor_filter_logs_filtered_total",
                filter="filter/events_types")
        + counter(metrics, "otelcol_processor_filter_logs_filtered_total",
                  filter="filter/otlp_types")
    )
    assert typed >= 18, typed
    accepted = counter(metrics, "otelcol_receiver_accepted_log_records_total")
    assert accepted == fixed["accepted"], (accepted, fixed["accepted"])
    refused = counter(metrics, "otelcol_receiver_refused_log_records_total")
    failed = counter(metrics, "otelcol_exporter_send_failed_log_records_total")
    assert refused == 0 and failed == 0, (refused, failed)

    # The store recorded the ingest path the rendered exporter uses.
    ingest = re.search(
        r'^vl_http_requests_total\{path="/insert/opentelemetry/v1/logs".*\} (\d+)$',
        fixed["store_metrics"], re.MULTILINE,
    )
    assert ingest and int(ingest.group(1)) >= 2, ingest

    # Every expression in the published dashboard names a metric one of these
    # two binaries actually exports.
    expressions = json.loads((scratch / "log-queries.json").read_text())
    exposed = metric_names(metrics) | metric_names(fixed["store_metrics"])
    for expression in expressions:
        bare = re.sub(r"\[[^]]*\]|\{[^}]*\}", "", expression)
        for name in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", bare):
            if name in JSON_FUNCTIONS:
                continue
            assert name in exposed, (name, expression)

    (scratch / "operational-records.json").write_text(json.dumps(records, indent=2))
    print(
        "[observability] runtime: rendered collector -> VictoriaLogs v1.53.0; "
        f"{len(records)} well-formed records stored as integers, "
        f"{int(typed)} typed hostile records dropped by the type filters, "
        "no marker, flattened key or nil placeholder in stored data or query results"
    )


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
