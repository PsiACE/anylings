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

The collector keeps an explicit, closed set of fields. Two things follow from
that: keys outside the set are removed, and the values inside the set are
type- and range-checked. Key filtering alone is not enough — a caller can put a
credential into an _allowed_ key — so both are enforced.

**Span attributes kept**

| Attribute                   | Accepted value                                                                                    |
| --------------------------- | ------------------------------------------------------------------------------------------------- |
| `http.request.method`       | a standard method: `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `TRACE`, `CONNECT` |
| `http.response.status_code` | an integer `100`–`599`                                                                            |
| `http.route`                | a short route pattern                                                                             |
| `rpc.method`                | a short method name                                                                               |
| `error.type`                | a short classifier                                                                                |

**Resource attributes kept:** `service.name`, `k8s.namespace.name`,
`deployment.environment.name`.

**Everything else is dropped**, including authorization headers, cookies,
session identifiers, prompts, tool output and arbitrary host or workload labels.

The following caller-controlled surfaces cannot be allowlisted usefully, so they
are cleared rather than preserved:

| Surface                                           | Handling                                         |
| ------------------------------------------------- | ------------------------------------------------ |
| span **events** (name and fields)                 | fields emptied, name replaced with a fixed label |
| instrumentation **scope** name/version/attributes | replaced with fixed values, attributes emptied   |
| **status message**                                | cleared                                          |
| resource and scope **schema URL**                 | cleared                                          |

The span _name_ is retained but bounded. This matters because it is the field
most instrumentation sets automatically — see below.

### Span names: what is accepted

Accepted:

- an HTTP method followed by a route — `GET /checkout`
- a route template with a leading slash — `/checkout/{cartId}`
- an operation name starting with a letter — `SELECT cart`, `Cart.Get`

Rejected whole:

- free text, and names longer than 120 characters

Query delimiters (`?`, `=`) are **not** accepted in a name or route: a string
carrying `?token=...` is a query string, not an operation identifier.

This is a **syntactic** bound, and it is worth being precise about what it does
not do. Text that fits the accepted class — a legal-looking statement, a word
sequence, a dotted token — can still contain an arbitrary secret. The collector
cannot detect that semantically and does not claim to; keeping secrets out of span
names and attribute values is the sender's responsibility.

**The rejection is observable, not silent.** A rejected span is counted, not
returned to the sender:

```bash
# spans dropped by the name/value contract, per filter
curl -sS http://otel-events.observability.svc.cluster.local:8888/metrics \
  | grep otelcol_processor_filter_spans_filtered_total
```

The sender still receives HTTP 200 — the request was accepted, individual spans
were not. If a trace is missing, compare that counter against the store's query
result before assuming the exporter failed.

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

- A span's `status.message` is free text, so the pipeline **clears** it rather
  than storing it. Use `error.type` for classification; a message you set there
  will not appear in the trace.
- This page describes the feature, not a running installation. Nothing here is
  evidence that a particular deployment is receiving traces.
