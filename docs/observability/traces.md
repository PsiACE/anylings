# Request traces

How to send and query request traces. The slice stores real spans: a timed
operation with a trace ID, a span ID, an optional parent span ID, a duration and
a status. A log line that happens to carry a correlation ID is **not** a trace
and is not accepted here.

## What this covers, and what it does not

It stores traces your workloads emit over OTLP. It does **not** instrument your
applications for you, generate spans server-side, or derive traces from request
logs. Sampling, propagation and span creation belong to the sender.

Application and node **source logs** are a separate concern with their own
collector and are not covered by this page.

## Where a span goes

```text
workload --OTLP/HTTP--> otel-events --filter--> rebuild --> VictoriaTraces --Jaeger API
```

Two endpoints, both `ClusterIP` only:

| Purpose                   | Address                                                                                 |
| ------------------------- | --------------------------------------------------------------------------------------- |
| Send spans (OTLP/HTTP)    | `http://otel-events.observability.svc.cluster.local:4318/v1/traces`                     |
| Query (Jaeger-compatible) | `http://victoria-traces.observability.svc.cluster.local:10428/select/jaeger/api/traces` |

Traces are written to their own store through their own exporter. A span is
never routed into the log store, and a log record is never turned into a span.

## Sending spans

Use any OpenTelemetry SDK with an OTLP/HTTP trace exporter pointed at the address
above. Set `service.name` as a resource attribute — it is how a trace is later
found.

## What is stored

The collector keeps an explicit, closed set of fields and rebuilds the attribute
maps from it, so an unlisted attribute is dropped rather than sanitized.

**Span attributes kept**

| Attribute                   | Meaning                        |
| --------------------------- | ------------------------------ |
| `http.request.method`       | HTTP method                    |
| `http.response.status_code` | HTTP status                    |
| `http.route`                | route template, not a raw path |
| `rpc.method`                | RPC method                     |
| `error.type`                | error classification           |

**Resource attributes kept:** `service.name`, `k8s.namespace.name`,
`deployment.environment.name`.

**Everything else is dropped**, including authorization headers, cookies,
session identifiers, prompts, tool output and arbitrary host or workload labels.
The span _name_ is the one caller-controlled string that reaches the store, and
it must match a bounded identifier pattern or the span is rejected whole.

## Retention and limits

- `-retentionPeriod=7d` and a 1 GiB disk cap, whichever is reached first, in a
  bounded `emptyDir` with a 2 GiB size limit.
- The window is a **query bound, not physical erasure**: the store prunes whole
  per-day partitions, so a partition that stops receiving data keeps its bytes
  until a drop runs. A reschedule loses everything regardless.
- Requests are `25m`/`96Mi`, limits `400m`/`384Mi`. Ingest and query concurrency
  are bounded, and the store runs non-root with a read-only root filesystem and
  no cluster RBAC.

## Querying

The Jaeger-compatible API accepts a service name and optional time window:

```bash
curl -sS "http://victoria-traces.observability.svc.cluster.local:10428/select/jaeger/api/traces?service=checkout&limit=20"
```

The `start`/`end` parameters are in **microseconds** — not milliseconds and not
nanoseconds. Omitting them widens the window; passing milliseconds returns
`request time out of retention`.

A response carries each span's operation name, duration, status and its
`references`, which is where the parent span ID appears. That is what makes a
trace a trace: a child span can be attributed to its parent and to the same
trace.

## Known limits

- A span's `status.message` is free text and is **not** filtered by the
  configured pipeline. It is carried by the store as its own status description
  field. Treat it as untrusted text and do not put secrets there; use
  `error.type` for classification.
- This page describes the feature, not a running installation. Nothing here is
  evidence that a particular deployment is receiving traces.
