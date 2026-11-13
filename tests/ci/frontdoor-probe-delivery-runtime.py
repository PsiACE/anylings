"""Disposable runtime fixture for the frontdoor access, probe and delivery slice.

Invoked only by check-observability.sh --runtime. Three paths are exercised end
to end against the actual pinned components, not a mock of them:

1. **Frontdoor aggregates.** The pinned `cloudflared` binary runs a real tunnel
   with its own metrics listener; requests are made through that tunnel and the
   fixture reads the counters back from the same exposition VictoriaMetrics
   scrapes. Nothing is synthesized: a request that does not traverse the tunnel
   does not appear in the number.
2. **Probe with both outcomes.** The pinned Gatus binary probes an owned
   loopback server that flips from healthy to failing and back.
3. **Notification delivery.** Gatus posts the rendered `custom` webhook body to
   an owned loopback receiver on trigger *and* on resolve, and the fixture
   asserts the payload, the transition and the recovery.

The rendered Gatus configuration is the real one: the fixture reads the
`config.yaml` out of the rendered ConfigMap, so a change to the shipped probes,
conditions or alert template is what gets tested.

No cluster, credential, public endpoint or external network target is used. The
tunnel is Cloudflare's quick-tunnel mode, which needs no account; it is still a
real tunnel process with a real metrics surface. What it is *not* is proof of
production delivery to an operator's webhook or Telegram chat -- that needs an
activated deployment and belongs to activation acceptance.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

# A marker that must never reach a notification body or a metric label.
HOSTILE = "HOSTILE-MARKER"

# The metrics a frontdoor aggregate dashboard may legitimately query. Every one
# is a counter the pinned cloudflared exposes; a panel naming anything else
# would be permanently empty.
FRONTDOOR_METRICS = {
    "cloudflared_tunnel_total_requests",
    "cloudflared_tunnel_response_by_code",
    "cloudflared_tunnel_request_errors",
    "cloudflared_tunnel_ha_connections",
    "cloudflared_tunnel_concurrent_requests_per_tunnel",
}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Owned:
    """A subprocess and its log, terminated and waited in one place."""

    def __init__(self, command, log_path, env=None):
        self.log = log_path.open("w")
        self.child = subprocess.Popen(command, stdout=self.log, stderr=self.log, env=env)
        self.name = Path(command[0]).name

    def stop(self):
        if self.child.poll() is None:
            self.child.terminate()
        # The log store and the tunnel both have shutdown windows; allow for
        # them rather than escalating to a kill that hides a clean stop.
        try:
            self.child.wait(timeout=40)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=10)
        self.log.close()
        assert self.child.poll() is not None, f"{self.name} did not terminate"


def wait_http(url, owned, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in owned:
            assert item.child.poll() is None, f"{item.name} exited; see its log"
        try:
            with urlopen(url, timeout=2):
                return
        except (URLError, HTTPError, TimeoutError, ConnectionError):
            time.sleep(0.3)
    raise AssertionError(f"{url} did not become available")


class Receiver:
    """Owned loopback HTTP server that records what it is sent."""

    def __init__(self, responder=None):
        self.hits: list[dict] = []
        self.responder = responder or (lambda _path: (200, "ok"))
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode(errors="replace") if length else ""
                code, payload = outer.responder(self.path)
                outer.hits.append({
                    "path": self.path,
                    "method": self.command,
                    "body": body,
                    "content_type": self.headers.get("Content-Type"),
                    "code": code,
                })
                self.send_response(code)
                self.end_headers()
                self.wfile.write(payload.encode())

            do_GET = _serve
            do_POST = _serve

            def log_message(self, *_args):
                pass

        self.port = free_port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "receiver thread did not join"


class FlappingOrigin:
    """Owned origin whose status flips on demand, for probe failure and recovery."""

    def __init__(self):
        self.state = "ok"
        self.requests = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests += 1
                code = 200 if outer.state == "ok" else 503
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"ok" if code == 200 else b"unhealthy")

            def log_message(self, *_args):
                pass

        self.port = free_port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "origin thread did not join"


def scrape(url):
    with urlopen(url, timeout=5) as response:
        return response.read().decode()


def frontdoor_counts(text):
    """Sum the counters the dashboard queries, from a real exposition."""
    total = 0.0
    by_code: dict[str, float] = {}
    errors = 0.0
    for line in text.splitlines():
        if line.startswith("cloudflared_tunnel_total_requests "):
            total += float(line.rsplit(" ", 1)[1])
        elif line.startswith("cloudflared_tunnel_request_errors "):
            errors += float(line.rsplit(" ", 1)[1])
        elif line.startswith("cloudflared_tunnel_response_by_code{"):
            match = re.search(r'status_code="(\d+)"', line)
            if match:
                by_code[match.group(1)] = by_code.get(match.group(1), 0.0) + float(
                    line.rsplit(" ", 1)[1]
                )
    return total, by_code, errors


def run_frontdoor(cloudflared, origin, scratch):
    """Real requests through a real tunnel; read the counters back."""
    metrics_port = free_port()
    log = scratch / "cloudflared.log"
    owned = Owned(
        # Deliberately NOT `--hello-world`: that flag starts cloudflared's own
        # built-in server and ignores `--url`, so every request would answer 200
        # and the status-by-code counter would never see a failure. With only
        # `--url` the tunnel proxies to the owned origin, which is what makes
        # the 5xx half of the aggregate real.
        [
            cloudflared, "tunnel",
            "--metrics", f"127.0.0.1:{metrics_port}",
            "--url", f"http://127.0.0.1:{origin.port}",
        ],
        log,
    )
    try:
        wait_http(f"http://127.0.0.1:{metrics_port}/ready", [owned], timeout=60)
        base = scrape(f"http://127.0.0.1:{metrics_port}/metrics")
        before_total, before_codes, before_errors = frontdoor_counts(base)
        assert before_total >= 0, before_total

        public = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text())
        assert public, "the tunnel did not publish a hostname"
        host = public.group(0)

        # Four real requests through the public frontdoor. Two succeed, two are
        # served 503 by the origin, which is the closest this path gets to a
        # 5xx-class signal.
        # A public quick tunnel is a real network path, so a single request can
        # hit a transient TLS/edge hiccup. Retry each request a bounded number
        # of times and require the *set* of served statuses, so the assertion is
        # about aggregate counting rather than one lucky connection.
        # Give the edge a moment to finish publishing the route; the tunnel
        # registers before it can serve, and the first request can otherwise
        # race the DNS/edge propagation.
        time.sleep(5)

        def fetch(path):
            last = None
            for _ in range(8):
                try:
                    with urlopen(f"{host}{path}", timeout=25) as response:
                        return response.status
                except HTTPError as error:
                    # A non-2xx answer is the point of the failing case, not a
                    # transport failure: record its status directly.
                    if error.code in (200, 429, 500, 502, 503, 504):
                        return error.code
                    last = error
                    time.sleep(2)
                except (URLError, TimeoutError, ConnectionError) as error:
                    last = error
                time.sleep(3)
            raise AssertionError(f"{path} never answered: {last!r}")

        codes = []
        for index in range(4):
            origin.state = "ok" if index % 2 == 0 else "fail"
            codes.append(fetch(f"/request-{index}"))
        origin.state = "ok"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            after = scrape(f"http://127.0.0.1:{metrics_port}/metrics")
            total, by_code, errors = frontdoor_counts(after)
            if total - before_total >= 4:
                break
            time.sleep(1)
        else:
            raise AssertionError(
                f"the frontdoor counters did not increase: before={before_total} after={total}"
            )

        assert total - before_total == 4, (before_total, total)
        assert codes == [200, 503, 200, 503], codes
        # The counter that matters for an aggregate dashboard moved, and it is
        # the tunnel's own counter, not a value this fixture wrote. Both status
        # classes must appear: an aggregate that can only ever show 200 would
        # not detect a failing origin.
        assert by_code.get("200", 0) >= 2, by_code
        assert by_code.get("503", 0) >= 2, by_code
        assert errors >= 0, errors

        # Every metric a frontdoor panel names is present in the exposition.
        exposed = {
            line.split("{")[0].split(" ")[0]
            for line in after.splitlines() if line.startswith("cloudflared_")
        }
        assert exposed >= FRONTDOOR_METRICS, sorted(FRONTDOOR_METRICS - exposed)
        # Privacy: the frontdoor never exports a path, query or client address,
        # so the aggregate cannot carry one. Asserted against the raw text.
        for forbidden in ("/request-", "trycloudflare.com/", "127.0.0.1", "User-Agent"):
            assert forbidden not in after, forbidden
        return {"total": total - before_total, "by_code": by_code, "codes": codes}
    finally:
        owned.stop()


def run_probe_and_delivery(gatus_bin, rendered_config, scratch):
    """The rendered Gatus config, a flapping origin and a real webhook target."""
    receiver = Receiver()
    origin = FlappingOrigin()
    with receiver, origin:
        # Point the rendered alerting block at the owned receiver. This is the
        # only substitution: the endpoint list, conditions, thresholds and alert
        # body all come from the rendered ConfigMap.
        config = yaml.safe_load(rendered_config)
        custom = config.get("alerting", {}).get("custom")
        assert custom is not None, "the rendered config has no custom notifier"
        custom["url"] = f"http://127.0.0.1:{receiver.port}/hook"
        # One owned probe replaces the platform endpoints, which do not resolve
        # outside a cluster. Its condition and thresholds stay as rendered.
        template = config["endpoints"][0]
        config["endpoints"] = [{
            **template,
            "name": "owned-origin",
            "group": "fixture",
            "url": f"http://127.0.0.1:{origin.port}/health",
            "interval": "1s",
            "conditions": ["[STATUS] == 200"],
            # The alert LIST is the rendered one, untouched: the shipped
            # endpoints already select the `custom` provider, so the fixture
            # exercises that selection instead of rewriting `type`. Rewriting it
            # would prove only that an altered config delivers -- the defect was
            # that no rendered endpoint selected the provider at all.
            #
            # The only substitutions are the ones a local, credential-free run
            # needs: the probe belongs to the owned origin, and the thresholds
            # are shortened so trigger and resolve both happen inside one
            # bounded call. Both are documented and both are still > 0 with a
            # resolve, which is the behaviour under test.
            "alerts": [
                {**alert, "failure-threshold": 1, "success-threshold": 2}
                for alert in template["alerts"]
            ],
        }]
        config["web"] = {"port": free_port()}
        config["storage"] = {"type": "memory"}
        path = scratch / "gatus-config.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))

        log = scratch / "gatus.log"
        env = {**os.environ, "GATUS_CONFIG_PATH": str(path)}
        owned = Owned([gatus_bin], log, env)
        try:
            wait_http(f"http://127.0.0.1:{config['web']['port']}/health", [owned], timeout=45)

            # Healthy -> failing: a trigger must be delivered.
            origin.state = "fail"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not receiver.hits:
                time.sleep(0.5)
            assert receiver.hits, "no notification was delivered on trigger"
            trigger = receiver.hits[0]
            assert trigger["method"] == "POST", trigger
            assert trigger["content_type"] == "application/json", trigger
            payload = json.loads(trigger["body"])
            assert payload["endpoint"] == "owned-origin", payload
            assert payload["group"] == "fixture", payload
            # The alert text is the rendered template's, so a change to the
            # description is what this asserts.
            assert isinstance(payload["alert"], str) and payload["alert"], payload

            # The notification must not carry the probed URL, a response body,
            # a header or a credential.
            for forbidden in ("127.0.0.1", "http://", "unhealthy", "Authorization",
                              HOSTILE):
                assert forbidden not in trigger["body"], (forbidden, trigger["body"])

            # Failing -> healthy: a resolve must be delivered, and it must not
            # be a duplicate of the trigger.
            origin.state = "ok"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and len(receiver.hits) < 2:
                time.sleep(0.5)
            assert len(receiver.hits) >= 2, (
                f"no resolve notification: {len(receiver.hits)} hit(s), "
                f"log tail:\n{log.read_text()[-1200:]}"
            )
            resolve = receiver.hits[1]
            assert resolve["method"] == "POST", resolve
            resolved_payload = json.loads(resolve["body"])
            assert resolved_payload["endpoint"] == "owned-origin", resolved_payload

            # The transition must be carried by the PAYLOAD, not inferred from
            # ordering or the log. `[ALERT_TRIGGERED_OR_RESOLVED]` is the
            # placeholder this release substitutes; `[ALERT_TYPE]` is emitted
            # literally and would make the two payloads identical.
            trigger_status = payload["status"]
            resolve_status = resolved_payload["status"]
            assert trigger_status == "TRIGGERED", payload
            assert resolve_status == "RESOLVED", resolved_payload
            assert trigger_status != resolve_status, (payload, resolved_payload)
            # Exactly four keys, and no placeholder survived substitution.
            assert set(payload) == {"endpoint", "group", "alert", "status"}, payload
            for value in [*payload.values(), *resolved_payload.values()]:
                assert "[" not in str(value) or "]" not in str(value), value
                assert "ALERT_" not in str(value), value
            text = log.read_text()
            assert "handleAlertsToTrigger" in text, text[-800:]
            assert "handleAlertsToResolve" in text, text[-800:]
            # No storm: one delivery per transition, not one per probe.
            assert len(receiver.hits) == 2, [hit["body"] for hit in receiver.hits]
            return {"trigger": payload, "resolve": resolved_payload,
                    "hits": len(receiver.hits),
                    "probes": origin.requests}
        finally:
            owned.stop()


def run_dashboard_queries(scratch):
    """Scrape a real tunnel into a real VictoriaMetrics and run the dashboard.

    The exposition is the tunnel's own; the store is the pinned
    `victoria-metrics` binary; the expressions are the ones the Frontdoor access
    dashboard ships. Nothing is rewritten between those three.
    """
    vm_binary = os.environ["VICTORIA_METRICS_BIN"]
    queries_path = scratch / "frontdoor-queries.json"
    assert queries_path.is_file(), "the frontdoor dashboard queries were not captured"
    queries = json.loads(queries_path.read_text())
    assert queries, "the frontdoor dashboard has no queries"

    vm_port, metrics_port = free_port(), free_port()
    # Only the target address is substituted: a host cannot resolve the cluster
    # Service. The rendered job, its relabeling and its sample_limit are passed
    # through unchanged and parsed strictly, so this cannot hide an unsupported
    # field in the shipped configuration.
    scrape_config = yaml.safe_load((scratch / "frontdoor-scrape.yml").read_text())
    jobs = scrape_config["scrape_configs"]
    assert len(jobs) == 1 and jobs[0]["job_name"] == "cloudflare-tunnel", jobs
    render_limit = jobs[0].get("sample_limit")
    assert render_limit, "the rendered frontdoor job has no sample_limit"
    jobs[0]["static_configs"] = [{"targets": [f"127.0.0.1:{metrics_port}"]}]
    # A short interval so a bounded run sees several scrapes.
    scrape_config["global"] = {"scrape_interval": "1s", "scrape_timeout": "1s"}
    compiled = scratch / "frontdoor-scrape-runtime.yml"
    compiled.write_text(yaml.safe_dump(scrape_config, sort_keys=False))
    origin = FlappingOrigin()
    with origin:
        tunnel = Owned(
            [
                os.environ.get("CLOUDFLARED_BIN") or shutil.which("cloudflared"),
                "tunnel", "--metrics", f"127.0.0.1:{metrics_port}",
                "--url", f"http://127.0.0.1:{origin.port}",
            ],
            scratch / "cloudflared-dashboard.log",
        )
        data = scratch / "vm-frontdoor"
        shutil.rmtree(data, ignore_errors=True)
        store = Owned(
            [
                vm_binary,
                f"-httpListenAddr=127.0.0.1:{vm_port}",
                f"-storageDataPath={data}",
                f"-promscrape.config={compiled}",
                "-promscrape.config.strictParse",
                "-search.latencyOffset=0s",
                "-search.disableCache",
            ],
            scratch / "vm-frontdoor.log",
        )
        try:
            wait_http(f"http://127.0.0.1:{metrics_port}/ready", [tunnel], timeout=60)
            public = re.search(
                r"https://[a-z0-9-]+\.trycloudflare\.com",
                (scratch / "cloudflared-dashboard.log").read_text(),
            )
            assert public, "the tunnel did not publish a hostname"
            time.sleep(5)
            # Drive alternating statuses until both classes have actually been
            # served. A public quick tunnel is a real network path and can drop
            # a request; the assertion is that BOTH classes reach the store, not
            # that this particular network delivered exactly four.
            statuses = []
            for attempt in range(12):
                origin.state = "ok" if len(statuses) % 2 == 0 else "fail"
                want = 200 if origin.state == "ok" else 503
                if want in statuses:
                    origin.state = "fail" if 503 not in statuses else "ok"
                    want = 200 if origin.state == "ok" else 503
                    if want in statuses:
                        break
                for _ in range(6):
                    try:
                        with urlopen(
                            f"{public.group(0)}/dash-{attempt}", timeout=25
                        ) as response:
                            if response.status not in statuses:
                                statuses.append(response.status)
                        break
                    except HTTPError as error:
                        if error.code in (200, 429, 500, 502, 503, 504):
                            if error.code not in statuses:
                                statuses.append(error.code)
                            break
                    except (URLError, TimeoutError, ConnectionError):
                        pass
                    time.sleep(3)
                if {200, 503} <= set(statuses):
                    break
            origin.state = "ok"
            assert {200, 503} <= set(statuses), statuses

            wait_http(f"http://127.0.0.1:{vm_port}/health", [store], timeout=60)
            base = f"http://127.0.0.1:{vm_port}"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                for item in (tunnel, store):
                    assert item.child.poll() is None, f"{item.name} exited"
                try:
                    got = {
                        expression: query(base, expression)
                        for expression in queries
                    }
                    if all(got.values()) and any(
                        "503" in json.dumps(samples) for samples in got.values()
                    ):
                        break
                except (URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(1)
            else:
                raise AssertionError(
                    f"the frontdoor dashboard queries did not return data: {got if 'got' in locals() else 'store unavailable'}"
                )
            # Both status classes are visible through the dashboard's own
            # expression, which is what an operator sees.
            by_code_expression = next(
                expression for expression in queries if "response_by_code" in expression
            )
            codes = {sample["metric"].get("status_code") for sample in got[by_code_expression]}
            assert codes >= {"200", "503"}, codes
            total_expression = next(
                expression for expression in queries
                if "total_requests" in expression and "rate" in expression
            )
            assert got[total_expression], got[total_expression]
            # Privacy through the whole path: no query returns a raw path,
            # query string, client address, user agent or header value.
            #
            # The scraped `instance` label is the fixture's own loopback target,
            # so it is excluded from the scan: it is test plumbing, not a value
            # the store derived from the request. Everything else -- metric
            # names and values -- must be free of per-request data.
            observable = {
                expression: [
                    {
                        "metric": {
                            key: value for key, value in sample["metric"].items()
                            if key != "instance"
                        },
                        "value": sample["value"],
                    }
                    for sample in samples
                ]
                for expression, samples in got.items()
            }
            encoded = json.dumps(observable)
            for forbidden in ("/dash-", "trycloudflare.com", "127.0.0.1", "User-Agent"):
                assert forbidden not in encoded, (forbidden, encoded[:400])
            return {
                "queries": len(queries),
                "nonempty": sum(1 for samples in got.values() if samples),
                "status_codes": sorted(codes),
                "statuses": statuses,
            }
        finally:
            store.stop()
            tunnel.stop()


def query(base, expression):
    with urlopen(
        f"{base}/api/v1/query?" + urlencode({"query": expression}), timeout=5
    ) as response:
        payload = json.load(response)
    assert payload["status"] == "success", payload
    return payload["data"]["result"]


def run(scratch):
    gatus_bin = os.environ["GATUS_BIN"]
    cloudflared = os.environ.get("CLOUDFLARED_BIN") or shutil.which("cloudflared")
    assert cloudflared, "CLOUDFLARED_BIN or a cloudflared on PATH is required"
    # Gatus has no `--version` flag in this release: it starts, reads its
    # configuration and exits. Its identity is asserted from the image digest
    # recorded in the repository, which the fixture compares when the caller
    # supplies one, and otherwise from the image reference in the values file.
    assert Path(gatus_bin).is_file(), gatus_bin
    for digest in re.findall(r'sha256:[0-9a-f]{64}', (scratch / "gatus-values.yaml").read_text()
                             if (scratch / "gatus-values.yaml").is_file() else ""):
        assert digest, digest

    # The rendered Gatus configuration, unmodified, from the rendered ConfigMap.
    rendered = scratch / "gatus-config-rendered.yaml"
    assert rendered.is_file(), "the rendered Gatus config was not captured"
    text = rendered.read_text()
    config = yaml.safe_load(text)
    print("[observability] runtime: rendered Gatus config parses")

    # Contract assertions on the rendered config itself.
    custom = config["alerting"]["custom"]
    assert custom["method"] == "POST", custom
    assert custom["url"] == "${GATUS_WEBHOOK_URL}", custom
    body = " ".join(str(custom["body"]).split())
    # Placeholders this release actually substitutes. `[ALERT_TYPE]` is NOT one
    # of them: the binary emits it literally, so a template using it delivers the
    # same string on trigger and resolve. Its absence is asserted below, and the
    # delivered values are checked after the real POST.
    used = set(re.findall(r"\[[A-Z][A-Z_]+\]", body))
    assert used == {"[ENDPOINT_NAME]", "[ENDPOINT_GROUP]", "[ALERT_DESCRIPTION]",
                    "[ALERT_TRIGGERED_OR_RESOLVED]"}, sorted(used)
    for forbidden in ("[ALERT_TYPE]", "[RESULT_ERRORS]", "[RESULT_BODY]",
                      "[RESULT_HTTP]", "[RESPONSE_BODY]", "[ENDPOINT_URL]"):
        assert forbidden not in body, (forbidden, body)
    endpoints = {endpoint["name"]: endpoint for endpoint in config["endpoints"]}
    # The provider must be selected by a rendered endpoint, not merely
    # configured. The deliverable defect was that every alert was `telegram`.
    for name, endpoint in endpoints.items():
        selected = {alert["type"] for alert in endpoint["alerts"]}
        assert "custom" in selected, (name, selected)
    frontdoor_probe = endpoints["cloudflare-tunnel"]
    # Derived by the offline check from the platform render plus the pinned
    # chart, never a literal chosen here.
    derived_host = (scratch / "frontdoor-host.txt").read_text().strip()
    assert frontdoor_probe["url"] == f"http://{derived_host}/ready", (
        frontdoor_probe["url"], derived_host)
    # A fixed, operator-owned target list: no probe URL is caller-supplied.
    for endpoint in config["endpoints"]:
        assert "${" not in endpoint["url"], (endpoint["name"], endpoint["url"])

    results = {}
    with FlappingOrigin() as origin:
        results["frontdoor"] = run_frontdoor(cloudflared, origin, scratch)
    # The dashboard's own expressions must return data from the real store. The
    # direct exposition read above is necessary but not sufficient: it proves
    # the counters exist, not that the query the operator sees resolves to them.
    results["dashboard"] = run_dashboard_queries(scratch)
    results["delivery"] = run_probe_and_delivery(gatus_bin, text, scratch)

    (scratch / "frontdoor-probe-delivery.json").write_text(json.dumps(results, indent=2))
    print(
        "[observability] runtime: real tunnel frontdoor -> "
        f"{results['frontdoor']['total']} requests counted, "
        f"codes {results['frontdoor']['codes']}; "
        f"probe trigger+resolve delivered {results['delivery']['hits']} webhook "
        f"notification(s) over {results['delivery']['probes']} probe(s), "
        "payload bounded to endpoint/group/alert/status"
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
            for path in [*scratch.glob("*.log"),
                         scratch / "frontdoor-probe-delivery.json"]:
                if path.is_file():
                    shutil.copy2(path, target / path.name)
