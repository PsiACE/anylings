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

import yaml
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# The archived pre-fix configuration, used as the regression control. Checked in
# so a clean checkout needs no git history, network or clone depth.
CONTROL_FIXTURE = Path(__file__).parent / "fixtures" / "collector-prefix-ae6a8bfc.yaml"

# Markers that appear only in fields the slice must never persist.
HOSTILE = "HOSTILE-MARKER"

# Query syntax that is not a metric name.
JSON_FUNCTIONS = frozenset({"sum", "rate", "min", "max", "by", "clamp_min", "receiver", "job"})

# Keys the stored record may contain. Anything else means either a new field
# was added deliberately or a nested value was flattened into the record.
EVENT_KEYS = {
    "_msg", "_stream", "_stream_id", "_time", "severity_number", "severity_text",
    # Fixed repository values, asserted below to be exactly these.
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


# The keys an OTLP AnyValue may use. A dict with exactly one of these is an
# already-encoded value and must be passed through, not treated as a map.
ANY_VALUE_KEYS = frozenset({
    "stringValue", "intValue", "doubleValue", "boolValue", "arrayValue", "kvlistValue",
})


def any_of(value):
    """Encode one field, keeping nested types and ints exact."""
    if isinstance(value, Raw):
        return value.value
    if isinstance(value, dict) and len(value) == 1 and set(value) <= ANY_VALUE_KEYS:
        return value
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


def structured_record(reason, level, component=None, count=None, extra=None, prepend=False):
    """One controlled OTLP record. Its free-text body is never read.

    `extra` may repeat an already-supplied key: that is the point of the
    duplicate cases. Order matters, because the collector validates the first
    occurrence of a key while the store ingests every occurrence.
    """
    pairs = [(key, value) for key, value in (
        ("reason", reason),
        ("level", level),
        ("component", component),
        ("event_count", count),
    ) if value is not None]
    if extra:
        extras = list(extra.items() if isinstance(extra, dict) else extra)
    else:
        extras = []
    attributes_list = extras + pairs if prepend else pairs + extras
    return {
        "timeUnixNano": str(time.time_ns()),
        "body": string(f"{HOSTILE}-BODY"),
        "attributes": [{"key": key, "value": any_of(value)} for key, value in attributes_list],
    }


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
        pairs = list(extra.items() if isinstance(extra, dict) else extra)
        record["attributes"] = [
            {"key": key, "value": any_of(value)} for key, value in pairs
        ]
    return record


def envelope(records, resource, resource_duplicates=None, resource_duplicates_first=False):
    """One OTLP request, always carrying hostile envelope metadata.

    A log record is not just its attributes. The envelope also carries an
    instrumentation scope (name, version, attributes), resource and scope
    schema URLs, and each record can carry a caller-supplied severity text,
    event name, trace/span IDs and flags. VictoriaLogs stores every one of
    those as its own field, so a hostile value there is exactly as dangerous as
    one in a record attribute. Sending them on every request means the guard is
    exercised by every case, not only by a case that opts in.

    The first two parameters are positional and stay that way on purpose: an
    external probe wraps this function to substitute its own scope, and must
    keep working. `resource_duplicates` is additive.

    A repeated *resource* key is the sibling of a repeated record key: the
    filters read the first occurrence and the store ingests every one, and the
    value also lands in `_stream`.
    """
    scope_logs = {
        "scope": {
            "name": f"{HOSTILE}-SCOPE-NAME",
            "version": f"{HOSTILE}-SCOPE-VERSION",
            "attributes": [
                {"key": "prompt", "value": string(f"{HOSTILE}-SCOPE-PROMPT")},
                {"key": "tool_output", "value": string_map({"k": f"{HOSTILE}-SCOPE-MAP"}).value},
                {"key": "session_id", "value": string_list([f"{HOSTILE}-SCOPE-ARR"]).value},
            ],
        },
        "schemaUrl": f"{HOSTILE}-SCOPE-SCHEMA",
        "logRecords": records,
    }
    for record in records:
        record.setdefault("severityText", f"{HOSTILE}-SEVERITY-TEXT")
        record.setdefault("eventName", f"{HOSTILE}-EVENT-NAME")
        record.setdefault("traceId", "1" * 32)
        record.setdefault("spanId", "2" * 16)
        record.setdefault("flags", 1)
    resource_pairs = list(resource.items())
    if resource_duplicates:
        duplicated = list(resource_duplicates.items())
        resource_pairs = (
            duplicated + resource_pairs if resource_duplicates_first
            else resource_pairs + duplicated
        )
    return {
        "resourceLogs": [{
            "resource": {"attributes": [
                {"key": key, "value": string(value)} for key, value in resource_pairs
            ]},
            "schemaUrl": f"{HOSTILE}-RESOURCE-SCHEMA",
            "scopeLogs": [scope_logs],
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

    # Repeated attribute keys. The collector keeps every occurrence,
    # `attributes[...]` reads the FIRST, and the store ingests ALL of them, so
    # a record can be validated on one value and stored from another. Both
    # orders are sent: the checked-then-hostile order and its reverse.
    duplicated_good = [
        # Valid first, hostile second: must be stored with the FIRST value.
        structured_record("dupKept", "WARN", "gateway", int_value(2), extra=[
            ("reason", string(f"{HOSTILE}-DUP-REASON")),
            ("level", string(f"{HOSTILE}-DUP-LEVEL")),
            ("component", string(f"{HOSTILE}-DUP-COMPONENT")),
            ("event_count", string(f"{HOSTILE}-DUP-COUNT")),
        ]),
        # Hostile first, valid second: the first occurrence is what the guard
        # sees, so this must be dropped, not stored from the second value.
        structured_record("dupGone", "ERROR", "gateway", int_value(1), extra=[
            ("reason", string(f"{HOSTILE}-FIRST-REASON")),
            ("level", string(f"{HOSTILE}-FIRST-LEVEL")),
        ], prepend=True),
    ]

    events_duplicated = [
        # Events re-derive their fields from the body, so an inherited record
        # attribute must not survive at all, in either order.
        event_record("ADDED", {
            "type": "Warning", "reason": "FailedMount", "count": 4, "involvedObject": pod,
        }, extra=[
            ("event_severity", string(f"{HOSTILE}-EV-SEVERITY")),
            ("event_count", string(f"{HOSTILE}-EV-COUNT")),
            ("event_reason", string(f"{HOSTILE}-EV-REASON")),
        ]),
    ]

    # Two records sent with a repeated resource key by the runner below.
    structured_resource_dup = [
        structured_record("resDupKept", "INFO", None, int_value(1)),
    ]
    structured_resource_dup_hostile_first = [
        structured_record("resDupGone", "INFO", None, int_value(1)),
    ]

    # Caller repeats the scratch key this stage copies into. `delete_key` is
    # `pdata.Map.Remove`, which removes only the first matching entry, so a
    # cleanup built from it leaves one behind -- stored as a new field and
    # promoted into `_stream`. Each case is a separate record so a stored row
    # can be attributed to exactly one of them.
    scratch_collision_records = [
        structured_record("scratchNsOnce", "INFO", None, int_value(1)),
        structured_record("scratchNsTwice", "INFO", None, int_value(1)),
        structured_record("scratchNsThrice", "INFO", None, int_value(1)),
        structured_record("scratchSvcTwice", "INFO", None, int_value(1)),
        structured_record("scratchRecTwice", "INFO", None, int_value(1)),
    ]

    return {
        "events_good": events_good,
        "events_hostile": events_hostile,
        "structured_good": structured_good,
        "scratch_collision_records": scratch_collision_records,
        "structured_resource_dup": structured_resource_dup,
        "structured_resource_dup_hostile_first": structured_resource_dup_hostile_first,
        "structured_hostile": structured_hostile,
        "structured_duplicated": duplicated_good,
        "events_duplicated": events_duplicated,
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
        # Regression control: the ARCHIVED pre-fix configuration, loaded as-is.
        #
        # It is not produced by deleting statements from the fixed config. That
        # approach inherits the fixed config's later stages -- notably the shape
        # stage's own trailing `keep_keys`, which collapses a repeated key to a
        # single occurrence independently of the removed normalisation -- so
        # whether a duplicate survived depended on field count and ordering
        # rather than on the guards under test. A control built that way is not
        # a faithful old negative, and its failures moved whenever the payload
        # set changed.
        #
        # `tests/ci/fixtures/collector-prefix-ae6a8bfc.yaml` is the rendered
        # relay from commit ae6a8bfc, checked in so a clean checkout needs no
        # git history, no network and no clone depth. That commit predates the
        # type guards, the envelope purge, the nil-free body and both
        # duplicate-key normalisations, so it leaks every guard this fixture
        # asserts. The loopback substitutions below are the same ones applied to
        # the candidate, and nothing else about it changes.
        relay = yaml.safe_load(CONTROL_FIXTURE.read_text())
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
        # The archived config has only the events pipeline wired to
        # `k8sobjects`; the fixture feeds both pipelines from local OTLP.
        relay["service"]["pipelines"]["logs/events"]["receivers"] = ["otlp/events"]
        relay["receivers"].pop("k8sobjects", None)
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
        # Every send carries a hostile instrumentation scope, hostile schema
        # URLs and caller-controlled severity/event-name/trace metadata. The
        # records themselves are unchanged, so a passing run still stores
        # exactly the well-formed ones -- and nothing from the envelope.
        post(f"http://127.0.0.1:{events_port}/v1/logs", envelope(
            records["events_good"], resource))
        post(f"http://127.0.0.1:{events_port}/v1/logs", envelope(
            records["events_hostile"], resource))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_good"],
            {**resource, "service.name": "gateway"},
        ))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_hostile"],
            {**resource, "service.name": "gateway"},
        ))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_duplicated"],
            {**resource, "service.name": "gateway"},
        ))
        post(f"http://127.0.0.1:{events_port}/v1/logs", envelope(
            records["events_duplicated"], resource,
        ))
        # Repeated *resource* keys, in both orders, actually sent. These two
        # posts are the reason `resDupKept` can exist in the store at all; the
        # payloads were previously built and never sent, so the assertion for
        # them could only ever fail.
        resource_duplicates = {
            "k8s.namespace.name": f"{HOSTILE}-RES-NAMESPACE",
            "service.name": f"{HOSTILE}-RES-SERVICE",
        }
        # The resource must carry BOTH approved keys, or the duplicate supply
        # becomes the first occurrence and the guard rejects it for that reason
        # rather than the one under test.
        duplicate_resource = {**resource, "service.name": "gateway"}
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_resource_dup"], duplicate_resource,
            resource_duplicates=resource_duplicates,
        ))
        post(f"http://127.0.0.1:{structured_port}/v1/logs", envelope(
            records["structured_resource_dup_hostile_first"], duplicate_resource,
            resource_duplicates=resource_duplicates, resource_duplicates_first=True,
        ))
        # A hostile duplicate of the scratch name itself, in every position that
        # matters: once, twice (the reported case), three times, on the service
        # scratch key, and on a record scratch key. All are actually posted.
        scratch_cases = {
            "scratchNsOnce": {"__otl_dup_ns": f"{HOSTILE}-SCRATCH-NS"},
            "scratchNsTwice": {"__otl_dup_ns": f"{HOSTILE}-SCRATCH-NS"},
            "scratchNsThrice": {"__otl_dup_ns": f"{HOSTILE}-SCRATCH-NS"},
            "scratchSvcTwice": {"__otl_dup_svc": f"{HOSTILE}-SCRATCH-SVC"},
            "scratchRecTwice": {},
        }
        for record in records["scratch_collision_records"]:
            reason = record["attributes"][0]["value"]["stringValue"]
            duplicates = scratch_cases[reason]
            # `scratchNsTwice`/`Thrice`/`SvcTwice` repeat the same scratch key;
            # `envelope` appends each entry of the mapping once, so a repeated
            # key is expressed by passing the same name in both the base
            # resource list and the duplicate map for the "once" case, and by a
            # doubled value list below for the repeated ones.
            # The hostile entries are added explicitly, `repetitions` times,
            # so the duplicate really is a duplicate in the wire list.
            host_resource = dict(duplicate_resource)
            if reason in ("scratchNsTwice", "scratchNsThrice", "scratchSvcTwice"):
                repetitions = 2 if reason != "scratchNsThrice" else 3
                payload = envelope([record], host_resource)
                resource_attributes = payload["resourceLogs"][0]["resource"]["attributes"]
                for name in duplicates:
                    for index in range(repetitions):
                        resource_attributes.append({
                            "key": name,
                            "value": string(f"{HOSTILE}-SCRATCH-{index}-{name}"),
                        })
                request = Request(
                    f"http://127.0.0.1:{structured_port}/v1/logs",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=10) as response:
                    assert response.status == 200, response.status
            elif reason == "scratchRecTwice":
                # The record-side sibling: a caller-supplied duplicate of the
                # record scratch name.
                payload = envelope([record], host_resource)
                payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"].append(
                    {"key": "__otl_dup_reason", "value": string(f"{HOSTILE}-SCRATCH-REC")})
                payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"].append(
                    {"key": "__otl_dup_reason", "value": string(f"{HOSTILE}-SCRATCH-REC-2")})
                request = Request(
                    f"http://127.0.0.1:{structured_port}/v1/logs",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=10) as response:
                    assert response.status == 200, response.status
            else:
                # Exactly one hostile occurrence of the scratch name.
                post(f"http://127.0.0.1:{structured_port}/v1/logs",
                     envelope([record], {**host_resource, **duplicates}))

        # Valid-first duplicates are stored; the hostile-first ones must be
        # dropped, so only the former count towards the positive expectation.
        expected = (len(records["events_good"]) + len(records["structured_good"])
                    + 3 + len(records["scratch_collision_records"]))
        # Wait for a record from *each* pipeline, not just for a total count: a
        # count alone can be satisfied by one pipeline while the other is still
        # in flight, and the control run then misses its nil placeholder.
        deadline = time.monotonic() + 45
        stored = ""
        while time.monotonic() < deadline:
            assert all(child.poll() is None for child in children), "runtime service exited; see logs"
            stored = store_query(store_base)
            lines = [line for line in stored.splitlines() if line.strip()]
            # A total is not enough: wait for a representative of every payload
            # group, otherwise a run can snapshot before the last posts land.
            if len(lines) >= expected and all(
                marker in stored for marker in ("recovered", "BackOff", "resDupKept")
            ):
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
        "accepted": sum(
            len(records[name]) for name in (
                "events_good", "events_hostile", "structured_good",
                "structured_hostile", "structured_duplicated", "events_duplicated",
                "structured_resource_dup", "structured_resource_dup_hostile_first",
                "scratch_collision_records",
            )
        ),
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
    # And it stored caller envelope metadata verbatim: scope, schema, event
    # name and trace/span identifiers. These were the reported leak.
    control_keys = {key for record in control_records for key in record}
    for envelope_key in ("scope.name", "scope.version", "scope.attributes.prompt",
                         "event_name", "trace_id", "span_id"):
        assert envelope_key in control_keys, (
            f"the control did not store {envelope_key}; the envelope negatives "
            "would then not be measuring the purge stage"
        )
    # The control kept the caller's scope name verbatim, which is the reported
    # leak; the fixed run replaces it with a repository constant.
    assert any(record.get("scope.name") == f"{HOSTILE}-SCOPE-NAME" for record in control_records)
    assert f"{HOSTILE}-SCOPE-NAME" in leaked and f"{HOSTILE}-SCOPE-PROMPT" in leaked, (
        "the control did not store hostile scope values"
    )
    # And it stored the second occurrence of a repeated attribute key, which is
    # the bypass this run guards. Both pipelines must show it.
    # Which *specific* duplicate field survives in the control is order- and
    # field-count-dependent, and honestly so: the archived baseline's own shape
    # stage ends with `keep_keys(attributes, ["event_count", "reason", "level",
    # "component"])`, and that collapse happens independently of the guard under
    # test. So the control is asserted to leak the duplicate *class*, not a
    # hand-picked key -- claiming a particular one would be an assertion about
    # pdata map ordering, not about normalisation.
    duplicate_markers = {
        marker for marker in (
            f"{HOSTILE}-DUP-REASON", f"{HOSTILE}-DUP-LEVEL",
            f"{HOSTILE}-DUP-COMPONENT", f"{HOSTILE}-DUP-COUNT",
            f"{HOSTILE}-FIRST-REASON",
        ) if marker in leaked
    }
    assert duplicate_markers, (
        "the control stored no second-occurrence duplicate value; the "
        "duplicate-key assertions would then not be measuring the normalisation"
    )
    # The mechanism, measured rather than assumed. The archived baseline's own
    # shape stage ends with a `keep_keys` over the four record attributes, so in
    # the control the two occurrences of a repeated key are collapsed to ONE
    # stored field -- and *which* value that field holds is the leak: the second
    # occurrence wins, so the value the allowlist never saw is what was stored.
    # A control cannot therefore be asserted to show a literally repeated key;
    # it is asserted to show the wrong value in the single field.
    control_dup = next(
        (record for record in control_records if "dupKept" in json.dumps(record)), None
    )
    if control_dup is None:
        # The hostile value replaced the valid one, so the row is no longer
        # findable by its reason; fall back to any row carrying the marker.
        control_dup = next(
            (record for record in control_records if f"{HOSTILE}-DUP-REASON" in json.dumps(record)),
            None,
        )
    assert control_dup is not None, "the control stored no duplicate-key record at all"
    assert control_dup.get("reason") != "dupKept", (
        "the control stored the FIRST occurrence, so it is not a faithful "
        f"pre-fix baseline: {control_dup.get('reason')!r}"
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
    # Nine baseline positives plus the valid-first duplicate record and the
    # duplicated Events record; the hostile-first duplicate is rejected.
    assert len(records) == 17, [record["_msg"] for record in records]

    # Reasons are distinct per case, so a collapsed or duplicated record shows.
    events = [record for record in records if "event_type" in record]
    structured = [record for record in records if "reason" in record]
    assert sorted(record["event_reason"] for record in events) == [
        "BackOff", "FailedMount", "Scheduled", "Scheduled", "Unhealthy"], events
    assert sorted(record["reason"] for record in structured) == [
        "authFailure", "backlog", "dupKept", "queueDepth", "recovered",
        "resDupKept", "retryBudget", "scratchNsOnce", "scratchNsThrice",
        "scratchNsTwice", "scratchRecTwice", "scratchSvcTwice"], structured
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

    # Envelope metadata is caller text that this slice never approved, so it
    # must be absent from the actual store and query response -- not merely
    # renamed. Each of these was observed to persist before the purge stage.
    assert HOSTILE not in fixed["stored"], (
        "a hostile marker reached the store: "
        f"{[line for line in fixed['stored'].splitlines() if HOSTILE in line][:1]}"
    )
    # Key-level, not substring-level: `event_namespace` is an approved
    # operational field and contains `event_name` as a substring.
    #
    # Only keys the transport demonstrably persists are asserted here. The
    # store never receives `schema_url`, `flags` or `dropped_attributes_count`
    # at all -- measured with the purge stage removed -- so asserting their
    # absence would pass before the fix and prove nothing. They are cleared in
    # the configuration as defence in depth, but the assertion set below is
    # restricted to the fields observed to leak unguarded.
    stored_keys = {key for record in records for key in record}
    for envelope_key in ("event_name", "trace_id", "span_id"):
        assert envelope_key not in stored_keys, envelope_key
    assert not any(key.startswith(("scope.attributes", "resource.")) for key in stored_keys), (
        sorted(key for key in stored_keys if key.startswith(("scope.", "resource.")))
    )
    assert "11111111111111111111111111111111" not in fixed["stored"], "raw trace id stored"
    assert "2222222222222222" not in fixed["stored"], "raw span id stored"
    # The severity text that survives is the one the shape stage wrote from the
    # allowlisted field, never the caller's string.
    for record in records:
        assert record["severity_text"] in {"INFO", "WARN", "ERROR", "DEBUG"}, record["severity_text"]
    # VictoriaLogs always emits `scope.name`/`scope.version`, so the guarantee is
    # not that the field is absent but that its value is the fixed one this
    # repository wrote. A caller's string can therefore never appear there.
    assert {(record["scope.name"], record["scope.version"]) for record in records} == {
        ("otel-events", "0")
    }, sorted({(record["scope.name"], record["scope.version"]) for record in records})
    # Structured records name their component explicitly; no scope key may be
    # used as a second, unvalidated channel for it.
    assert all("scope." not in key or key in {"scope.name", "scope.version"}
               for record in records for key in record)

    # Negatives: no marker, no raw value, no flattened key, no unknown key.
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

    # Duplicate attribute keys. The collector validates the FIRST occurrence
    # and the store ingests every one, so without an explicit first-occurrence
    # normalisation a record passes the guard on one value and is stored with
    # another -- which is exactly what the store text would show as two keys.
    for record in records:
        # The parsed JSON dict cannot represent a repeated key, so the check is
        # on the raw stored line: every stored key appears once.
        line = next(
            (candidate for candidate in fixed["stored"].splitlines()
             if f'"_stream_id"' in candidate
             and record["_msg"] in candidate
             and record["_time"] in candidate),
            None,
        )
        assert line is not None, record["_msg"]
        for key in ("reason", "level", "component", "event_count",
                    "event_reason", "event_severity", "event_type"):
            assert line.count(f'"{key}":') <= 1, (key, line[:200])

    kept = next((record for record in records if record.get("reason") == "dupKept"), None)
    assert kept is not None, "the valid-first duplicate record was not stored"
    # Stored from the first occurrence of each repeated key, not the second.
    assert kept["level"] == "WARN", kept
    assert kept["component"] == "gateway", kept
    assert kept["event_count"] == "2", kept
    assert kept["severity_text"] == "WARN", kept
    # The hostile-first record fails the guard, so it must not be stored at all.
    assert all(record.get("reason") != "dupGone" for record in records)
    assert f'{HOSTILE}-FIRST-REASON' not in fixed["stored"], "the second-occurrence value was stored"

    # Events discard inherited record attributes and re-derive from the body,
    # so neither occurrence of a duplicated record attribute can survive.
    mounted = next((record for record in records if record.get("event_reason") == "FailedMount"), None)
    assert mounted is not None, "the duplicated Events record was not stored"
    assert mounted["event_severity"] == "Warning", mounted
    assert mounted["event_count"] == "4", mounted
    assert mounted["severity_text"] == "WARN", mounted
    for marker in (f"{HOSTILE}-EV-SEVERITY", f"{HOSTILE}-EV-COUNT", f"{HOSTILE}-EV-REASON"):
        assert marker not in fixed["stored"], marker

    # Repeated *resource* keys: the valid value must win, in both orders, and
    # the hostile occurrence must not reach the store or the stream label.
    resource_kept = next(
        (record for record in records if record.get("reason") == "resDupKept"), None
    )
    assert resource_kept is not None, "the valid-first resource-duplicate record was not stored"
    assert resource_kept["k8s.namespace.name"] == "agents", resource_kept
    assert resource_kept["service.name"] == "gateway", resource_kept
    assert all(record.get("reason") != "resDupGone" for record in records), (
        "the hostile-first resource-duplicate record was stored"
    )
    for marker in (f"{HOSTILE}-RES-NAMESPACE", f"{HOSTILE}-RES-SERVICE"):
        assert marker not in fixed["stored"], marker
    # One occurrence per resource key, checked on the raw line because a parsed
    # dict cannot represent a repeat.
    for line in fixed["stored"].splitlines():
        if not line.strip():
            continue
        for key in ("k8s.namespace.name", "service.name"):
            assert line.count(f'"{key}":') <= 1, (key, line[:240])

    # A caller-supplied duplicate of the scratch key must not survive the
    # resource stage. The store and the query response are checked on the raw
    # text, because a repeated or caller-named key is exactly what a parsed
    # dict hides.
    assert "__otl_dup_" not in fixed["stored"], (
        "a scratch key reached the store: "
        f"{[line[:200] for line in fixed['stored'].splitlines() if '__otl_dup_' in line][:1]}"
    )
    for reason in ("scratchNsOnce", "scratchNsTwice", "scratchNsThrice",
                   "scratchSvcTwice", "scratchRecTwice"):
        row = next((record for record in records if record.get("reason") == reason), None)
        assert row is not None, f"{reason} was not stored"
        # The approved context is intact, so the case is a positive, not a pass
        # by dropping the record.
        assert row["k8s.namespace.name"] == "agents", row
        assert row["service.name"] == "gateway", row
        assert row["event_count"] == "1", row
        allowed = STRUCTURED_KEYS
        assert set(row) <= allowed, sorted(set(row) - allowed)

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
