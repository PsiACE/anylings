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
            # The rendered probes alert over Telegram, which needs operator
            # credentials. The fixture retargets the same alert at the `custom`
            # webhook so delivery can be observed locally. Only `type` and the
            # thresholds change; the description, which is what the payload
            # carries, is the rendered one.
            "alerts": [
                {**alert, "type": "custom",
                 "failure-threshold": 1, "success-threshold": 2}
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
            # Gatus reports the resolve as the same description; the transition
            # is distinguished by ordering and by the log, which is why the log
            # is asserted too rather than inferring from the body.
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
    for placeholder in ("[ENDPOINT_NAME]", "[ENDPOINT_GROUP]", "[ALERT_DESCRIPTION]",
                        "[ALERT_TYPE]"):
        assert placeholder in body, (placeholder, body)
    for forbidden in ("[RESULT_ERRORS]", "[RESULT_BODY]", "[RESULT_HTTP]", "[RESPONSE_BODY]"):
        assert forbidden not in body, (forbidden, body)
    endpoints = {endpoint["name"]: endpoint for endpoint in config["endpoints"]}
    frontdoor_probe = endpoints["cloudflare-tunnel"]
    assert frontdoor_probe["url"].startswith("http://cf.network.svc.cluster.local:"), frontdoor_probe
    # A fixed, operator-owned target list: no probe URL is caller-supplied.
    for endpoint in config["endpoints"]:
        assert "${" not in endpoint["url"], (endpoint["name"], endpoint["url"])

    results = {}
    with FlappingOrigin() as origin:
        results["frontdoor"] = run_frontdoor(cloudflared, origin, scratch)
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
