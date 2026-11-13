# Operational logs

How to read, send and verify the platform's operational log slice. This is the
detail behind the summary in the [README](../../README.md#observability).

## What this covers, and what it does not

The slice stores two kinds of **operational records**:

1. Kubernetes **Events**, watched read-only from the API server.
2. A **structured OTLP** record that a workload sends deliberately.

It does **not** collect application container logs, node logs, journald output or
any file on a host. There is no log-tailing agent, no `hostPath`, no
`pods/log` permission and no privileged daemon. Broader file-based coverage needs
a separate source integration and is not part of this slice.

## Where a record goes

```text
Events API ──watch──▶ otel-events ──OTLP/HTTP──▶ victoria-logs ──LogsQL──▶ vmui
workload   ──OTLP───▶   (extract → type filter → allowlist → shape)
```

- Collector Service: `otel-events.observability.svc.cluster.local:4318`
  (OTLP/HTTP only; gRPC, Zipkin and Jaeger ports are disabled).
- Store Service: `victoria-logs.observability.svc.cluster.local:9428`.
- Both are `ClusterIP`. Nothing here renders an Ingress, route, LoadBalancer or
  NodePort.

## Sending a structured operational record

`POST` OTLP/HTTP to `http://otel-events.observability.svc.cluster.local:4318/v1/logs`.
The record **body is never read**; only these attributes are:

| Attribute     | Required | Accepted value                                             | Rejected                                                    |
| ------------- | -------- | ---------------------------------------------------------- | ----------------------------------------------------------- |
| `reason`      | yes      | string, `[A-Za-z][A-Za-z0-9]{0,63}`                        | missing, wrong type, free text, punctuation                 |
| `level`       | yes      | one of `DEBUG`, `INFO`, `WARN`, `ERROR` (string)           | missing, wrong type, other value                            |
| `component`   | no       | string, a DNS label (`[a-z0-9]([-a-z0-9]{0,62}[a-z0-9])?`) | present but wrong type or malformed                         |
| `event_count` | no       | integer `0`–`100000`; defaults to `1`                      | `bool`, double, string, map, array, negative, above the cap |

Resource attributes `k8s.namespace.name` and `service.name` are kept when they
are strings and match their DNS-label or namespace pattern; anything else is
dropped with the record.

## The type contract, and why it comes first

Every accepted field is type-checked **before** any regex or range comparison.
This matters because the collector runs with `error_mode: ignore`, and a filter
expression that raises on a type mismatch does not remove the record — it keeps
it. A range check alone (`event_count < 0 or event_count > 100000`) therefore
accepts a string, or flattens a map into `event_count.note`, instead of
rejecting it.

So both pipelines run a `filter/*_types` stage first:

- `event_count` must be absent or an integer (`not IsInt` drops it).
- Every required string must be a string (`not IsString` drops it).
- Optional present-but-wrong-type fields drop the record; absence is allowed
  only where the table above says optional.

Only after that stage does the allowlist apply its regex and range rules, and
only after the allowlist does the shape stage compose the stored body. A record
that fails at any stage is dropped whole, so no substring, nested map or raw
value can reach the store.

`event_count` is stored as an integer. Numeric positives in the runtime fixture
are sent as real OTLP `intValue`, not `doubleValue`, because a double is exactly
what the guard exists to reject.

## The envelope is not an input

A log record is more than its attributes. An OTLP request also carries an
instrumentation scope (name, version, attributes), resource and scope schema
URLs, and each record can carry a caller-supplied severity text, event name,
trace and span IDs and flags. VictoriaLogs stores every one of those as its own
field — `scope.name`, `scope.attributes.<key>`, `event_name`, `trace_id`,
`span_id` — so an attributes allowlist alone does not bound what reaches the
store.

`transform/purge_envelope` therefore runs **first** in both pipelines and clears
them:

| Envelope part                        | Handling                                                            |
| ------------------------------------ | ------------------------------------------------------------------- |
| resource schema URL                  | cleared                                                             |
| scope name, version                  | replaced with the fixed `otel-events` / `0`                         |
| scope attributes                     | removed (`keep_keys(..., [])`)                                      |
| scope schema URL                     | cleared                                                             |
| caller severity text                 | cleared, then written from the allowlisted `level`/`event_severity` |
| event name, trace ID, span ID, flags | cleared                                                             |

The scope name stays present because VictoriaLogs always emits it; its value is
a repository constant, so it can never echo a sender's string. Trace
correlation is a separate future requirement — the IDs are cleared rather than
assumed to be secret-free.

This is why the runtime fixture sends hostile scope metadata, schema URLs and
per-record severity/event-name/trace values on **every** request: the guard is
exercised by every case, not by a case that opts in. The fixture also asserts a
regression control reproduces the original leak, so the check cannot pass
vacuously.

## A repeated attribute key is normalised, not trusted

An OTLP log record may list the same attribute key more than once. The collector
keeps every occurrence; `attributes[...]` reads the **first**; the store ingests
**all** of them. A record could therefore pass the type and allowlist checks on
one value and be stored with another — a caller-selectable way to persist
something the contract never saw.

Both pipelines normalise before anything reads a field:

- **structured** — each approved value is copied aside, the attribute map is
  rebuilt from those copies only, and the copies are written back. Exactly one
  occurrence survives, and it is the one the filters inspect.
- **events** — inherited record attributes are discarded entirely and the
  operational fields are re-derived from the API object in the record body, so
  no record attribute can influence the stored event.

The runtime fixture sends both orders (valid-then-hostile and hostile-then-valid)
for `reason`, `level`, `component` and `event_count`, plus duplicated Events
record attributes, and asserts: only the first occurrence is stored, the
hostile-first record is rejected, no stored line contains a repeated key, and the
guard-less control still stores the second occurrence.

## The stored body

Shape appends only the parts that exist, so an absent optional `component` or
namespace leaves no `<nil>` placeholder and no doubled separator:

```text
event <event_type> <object_kind> <event_namespace> <object_name> <event_reason>
event <level> [<component>] [<namespace>] <reason>
```

Persisted keys are `event_type`, `event_severity`, `event_reason`,
`event_count`, `object_kind`, `object_name`, `event_namespace` for Events and
`reason`, `level`, `component`, `event_count` for structured records, plus the
namespace and service name. The original event message, note, annotations,
managed fields, resource UIDs, authorization headers, cookies, query strings and
every unlisted attribute are discarded with the record.

## Retention and its limits

- `-retentionPeriod=3d` and `-retention.maxDiskSpaceUsageBytes=1GiB`, whichever
  is reached first.
- Both are **query targets, not hard erasure**. VictoriaLogs drops whole per-day
  partitions; a partition that stops receiving data keeps its bytes until a drop
  runs.
- The store is an `emptyDir` with `sizeLimit: 2Gi`, so a rescheduled Pod loses
  every record regardless of the retention window.
- This is not an audit trail. Audit retention belongs in a separate store with
  its own policy.

## Inspecting and verifying

Rejected records are counted per filter, so a privacy decision is observable
without repeating its content:

Query the counter through VictoriaMetrics, which already scrapes the collector.
The pinned collector image has no shell or `wget`; do not try to exec them.
In one terminal, keep this loopback-only port-forward in the foreground using
an explicit operator kubeconfig and context:

```bash
kubectl --kubeconfig /path/to/operator.conf --context operator@cluster \
  --namespace observability port-forward --address 127.0.0.1 \
  service/victoria-metrics 8428:8428
```

In another terminal, read the per-filter counters, then stop the port-forward
with Ctrl-C when finished:

```bash
curl --fail --silent --show-error --get \
  --data-urlencode 'query=otelcol_processor_filter_logs_filtered_total{job="otel-events"}' \
  http://127.0.0.1:8428/api/v1/query
```

The vmui **Operational logs** dashboard shows the ingest path (accepted, stored,
exporter failures), the dropped count and share, and store size and free disk.

Offline, the rendered configuration is checked without a cluster:

```bash
bash tests/ci/check-observability.sh
```

The opt-in runtime check runs the same rendered configuration into the actual
pinned `v1.53.0` store and asserts typed negatives, integer positives and the
absence of hostile markers:

```bash
OTELCOL_BIN=/path/to/otelcol-contrib-0.140.1 \
VICTORIA_METRICS_BIN=/path/to/victoria-metrics-v1.155.0 \
VICTORIA_LOGS_BIN=/path/to/victoria-logs-v1.53.0 \
  bash tests/ci/check-observability.sh --runtime
```

That fixture also runs a guard-less control of the same configuration, which
must leak. If it ever stops leaking, the fixture no longer proves the guards do
anything and the check fails.

## Frontdoor access aggregates and notification delivery

A sibling slice covers the public frontdoor and the alerting path. It is the same
store and the same pinned components, no extra backend.

**Frontdoor aggregates** come from the tunnel's own counters, scraped from
`cloudflare-tunnel.network.svc.cluster.local:8080` as job `cloudflare-tunnel`:

| Metric                                                                    | Meaning                                    |
| ------------------------------------------------------------------------- | ------------------------------------------ |
| `cloudflared_tunnel_total_requests`                                       | requests the tunnel proxied                |
| `cloudflared_tunnel_response_by_code{status_code}`                        | responses by status class, from the origin |
| `cloudflared_tunnel_request_errors`                                       | proxy-to-origin failures                   |
| `cloudflared_tunnel_ha_connections`, `..._concurrent_requests_per_tunnel` | tunnel health                              |

These are bounded by construction: cloudflared exports no path, query, client
address, user agent or body, so an aggregate cannot carry one and there is no
field to leak them into. The identity labels the pinned tunnel _does_ emit —
`userHostname`, `connection_id`, `edge_location`, `rpcName` — are dropped by the
scrape job; they are deployment identity rather than aggregate dimensions, and
the counters and histograms every panel queries carry no labels of their own, so
removing them cannot change a panel or merge two series.

The scrape target and the probe URL name the Service the pinned chart actually
renders for this app: `cloudflare-tunnel.network.svc.cluster.local:8080`, which
is the release name from the application's `config.json`. The offline check
derives that host by rendering the chart under that release name and comparing,
so a wrong host fails instead of matching itself. The **Frontdoor access** dashboard queries only the
metrics above, and the offline check rejects any panel that names another.

One measured caveat: cosmetically the `--hello-world` flag makes cloudflared run
its own built-in server and ignore `--url`, so every request answers 200 and the
status counter never shows a failure. The deployment does not use that flag, and
the runtime fixture drives a real origin so both `status_code="200"` and
`status_code="503"` are observed.

**Probes** are Gatus endpoints with fixed, operator-owned URLs. Each declares a
failure threshold and `send-on-resolved`, so a fault is reported and then
cleared. The `cloudflare-tunnel` probe targets the tunnel's `/ready` and requires
`readyConnections > 0`, not merely HTTP 200 — the endpoint answers 200 with a JSON
body, so a status-only check would pass without a registered connector.

**Notification delivery** is a real HTTP POST, not just a contact-point entry. The
rendered `alerting.custom` block posts a closed JSON template:

```json
{
    "endpoint": "[ENDPOINT_NAME]",
    "group": "[ENDPOINT_GROUP]",
    "alert": "[ALERT_DESCRIPTION]",
    "status": "[ALERT_TRIGGERED_OR_RESOLVED]"
}
```

`[ALERT_TRIGGERED_OR_RESOLVED]` is the placeholder this Gatus release actually
substitutes — it renders `TRIGGERED` or `RESOLVED`. `[ALERT_TYPE]` is **not**
supported and is emitted literally, which would make the two payloads identical;
it is rejected by the check. The custom provider must also be _selected_ by each
endpoint's alert list: configuring `alerting.custom` alone delivers nothing,
because Gatus routes by the alert's own `type`.

It carries only those four allowlisted substitutions. `[RESULT_ERRORS]`,
`[RESULT_BODY]` and response headers are deliberately excluded: error strings can
embed the probed URL, so they stay out of the notification. The URL comes from
an optional `gatus-webhook` Secret (`GATUS_WEBHOOK_URL`); without it the provider
is inert and nothing is sent.

The runtime fixture proves trigger **and** resolve delivery against an owned
loopback receiver, asserts the payload fields, rejects a URL/body/credential in
the notification, and asserts exactly one delivery per transition (no storm).
That is local delivery evidence. **Production delivery** to an operator's webhook
or Telegram chat needs an activated deployment with real credentials, and is not
claimed here.

## Known gaps

- Synthetic Event payloads in the fixture are not proof of a live Kubernetes
  watch, API authorization, cluster DNS or node capacity.
- Application container logs, node logs, traces, web/API aggregate analytics and
  notification proof are not delivered by this slice.
