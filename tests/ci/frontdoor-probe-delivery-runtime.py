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

import hashlib
import http.server
import tarfile
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
# The reviewed Gatus image reference. The tag and digest are both pinned: the
# digest is the multi-arch INDEX digest (the manifest list). A platform manifest
# inside that index has its own digest and is not what this pins, so an operator
# pulling on any architecture still resolves the reviewed index.
GATUS_PIN = {
    "tag": "v5.38.0",
    "digest": "sha256:085ed63da963728965d00a3c78aa6167e3bf135218485b893201758e079e8415",
}

# An exposition carrying the label names the repository pin actually emits,
# including userHostname / connection_id / edge_location / rpcName, plus the dead
# names an earlier ineffective pattern used and the status dimension a panel
# needs. Values are synthetic; the label census is measured from
# `cloudflared 2026.10.7`, not invented.
OWNED_IDENTITY_EXPOSITION = """\
# HELP cloudflared_tunnel_total_requests requests proxied
# TYPE cloudflared_tunnel_total_requests counter
cloudflared_tunnel_total_requests 4
# HELP cloudflared_tunnel_response_by_code responses by status
# TYPE cloudflared_tunnel_response_by_code counter
cloudflared_tunnel_response_by_code{status_code="200"} 2
cloudflared_tunnel_response_by_code{status_code="503"} 2
# HELP cloudflared_tunnel_request_errors proxy errors
# TYPE cloudflared_tunnel_request_errors counter
cloudflared_tunnel_request_errors 0
# HELP cloudflared_tunnel_ha_connections connections
# TYPE cloudflared_tunnel_ha_connections gauge
cloudflared_tunnel_ha_connections 1
# HELP cloudflared_tunnel_concurrent_requests_per_tunnel concurrent
# TYPE cloudflared_tunnel_concurrent_requests_per_tunnel gauge
cloudflared_tunnel_concurrent_requests_per_tunnel 0
# TYPE cloudflared_tunnel_server_locations gauge
cloudflared_tunnel_server_locations{connection_id="0",edge_location="nrt09"} 1
# TYPE cloudflared_tunnel_user_hostnames_counts gauge
cloudflared_tunnel_user_hostnames_counts{userHostname="https://owned.example.trycloudflare.com"} 1
# TYPE cloudflared_tunnel_register_success counter
cloudflared_tunnel_register_success{rpcName="RegisterTunnel"} 1
"""

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


class TunnelUnavailable(Exception):
    """The public quick-tunnel edge could not serve this sandbox."""


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
            # A public quick tunnel is an external service this project does not
            # control. When the edge cannot serve the sandbox at all, that is an
            # environment limitation, not a product result: report it as such
            # rather than failing the suite or, worse, quietly passing. The
            # tunnel-independent evidence (label policy, dashboard queries over
            # a loopback exposition, probe/delivery routing) still runs.
            raise TunnelUnavailable(f"{path} never answered: {last!r}")

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




def verify_gatus_provenance(scratch, gatus_bin, declared_image):
    """Prove the tested binary came from the declared, pinned OCI image.

    Steps, all local and bounded:
      1. the declared image reference is resolved in the local container store;
      2. its RepoDigests must contain the reviewed multi-arch INDEX digest, so a
         locally re-tagged image cannot masquerade as the pin;
      3. the binary is extracted from that image, and its sha256 must equal the
         binary the fixture actually executes.

    If no container tool can do this, the fixture fails closed with an explicit
    ENV_BLOCKED message instead of accepting an unidentified binary.
    """
    tool = shutil.which("podman") or shutil.which("docker")
    assert tool, (
        "ENV_BLOCKED: no container tool to bind GATUS_BIN to the declared OCI "
        "image; refusing to accept an unidentified binary")
    reference = f"{declared_image['repository']}:{declared_image['tag']}"
    pinned_digest = GATUS_PIN["digest"]

    def run(*args, **kwargs):
        return subprocess.run([tool, *args], capture_output=True, text=True,
                              timeout=180, **kwargs)

    digests = run("image", "inspect", reference, "--format", "{{json .RepoDigests}}")
    assert digests.returncode == 0, (
        f"ENV_BLOCKED: {reference} is not present locally: {digests.stderr.strip()[:200]}")
    repo_digests = json.loads(digests.stdout.strip() or "[]")
    assert any(entry.endswith("@" + pinned_digest) for entry in repo_digests), (
        f"the local {reference} is not the reviewed image: {repo_digests}")

    name = f"gatus-provenance-{os.getpid()}"
    run("rm", "-f", name)
    created = run("create", "--name", name, reference)
    assert created.returncode == 0, created.stderr[:300]
    archive = scratch / "gatus-provenance.tar"
    try:
        exported = run("export", name, "-o", str(archive))
        assert exported.returncode == 0, exported.stderr[:300]
        with tarfile.open(archive) as tar:
            member = next(
                (m for m in tar.getmembers()
                 if m.isfile() and m.name.lstrip("./") in ("gatus", "app/gatus")),
                None,
            )
            assert member is not None, "the image contains no gatus binary"
            extracted = tar.extractfile(member)
            assert extracted is not None
            image_binary_sha = hashlib.sha256(extracted.read()).hexdigest()
    finally:
        run("rm", "-f", name)
        archive.unlink(missing_ok=True)

    tested_sha = hashlib.sha256(Path(gatus_bin).read_bytes()).hexdigest()
    assert tested_sha == image_binary_sha, (
        f"the tested Gatus binary is {tested_sha}, but the pinned image "
        f"{reference}@{pinned_digest} contains {image_binary_sha}")
    return {
        "reference": reference,
        "index_digest": pinned_digest,
        "binary_sha256": tested_sha,
    }



def run_label_policy(scratch):
    """Prove the rendered labeldrop removes the labels the pinned tunnel emits.

    The exposition here is synthetic but its LABEL SET is the census measured
    from `cloudflared 2026.10.7` (the repository pin), including both the real
    identity names and the four names a previous, ineffective pattern used. The
    store, the rendered job and `strictParse` are the real ones; only the target
    address is substituted, because a host cannot resolve a cluster Service.

    This is a loopback replay, not a tunnel: it proves the *label policy*, not
    tunnel behaviour, and it opens no outbound connection.
    """
    vm_binary = os.environ["VICTORIA_METRICS_BIN"]
    scrape_config = yaml.safe_load((scratch / "frontdoor-scrape.yml").read_text())
    job = next(j for j in scrape_config["scrape_configs"]
               if j["job_name"] == "cloudflare-tunnel")
    drops = [e for e in job.get("metric_relabel_configs", [])
             if e.get("action") == "labeldrop"]
    assert len(drops) == 1, drops

    vm_port, expo_port = free_port(), free_port()
    exposition = OWNED_IDENTITY_EXPOSITION
    data = scratch / "vm-labelpolicy"
    shutil.rmtree(data, ignore_errors=True)
    scrape_config["scrape_configs"] = [job]
    job["static_configs"] = [{"targets": [f"127.0.0.1:{expo_port}"]}]
    scrape_config["global"] = {"scrape_interval": "1s", "scrape_timeout": "1s"}
    compiled = scratch / "labelpolicy-scrape.yml"
    compiled.write_text(yaml.safe_dump(scrape_config, sort_keys=False))

    receiver = Receiver(responder=lambda path: (
        (200, exposition) if path.startswith("/metrics") else (404, "no")
    ))
    with receiver:
        # The scrape target is the receiver's own bound port, taken after it is
        # listening. A separately chosen free port would be a different socket.
        expo_port = receiver.port
        job["static_configs"] = [{"targets": [f"127.0.0.1:{expo_port}"]}]
        compiled.write_text(yaml.safe_dump(scrape_config, sort_keys=False))
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
            scratch / "vm-labelpolicy.log",
        )
        try:
            wait_http(f"http://127.0.0.1:{vm_port}/health", [store], timeout=45)
            base = f"http://127.0.0.1:{vm_port}"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                assert store.child.poll() is None, "the store exited"
                try:
                    series = query(base, "cloudflared_tunnel_server_locations")
                    users = query(base, "cloudflared_tunnel_user_hostnames_counts")
                    if series and users:
                        break
                except (URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(1)
            else:
                raise AssertionError("the labelpolicy store returned no series")

            labels = {name for sample in series for name in sample["metric"]}
            user_labels = {name for sample in users for name in sample["metric"]}
            every = labels | user_labels
            # Real identity labels never reach the store.
            for gone in ("userHostname", "connection_id", "edge_location", "rpcName"):
                assert gone not in every, (gone, sorted(every))
            # The status dimension a panel needs survives.
            by_code = query(base, "cloudflared_tunnel_response_by_code")
            assert by_code, "response_by_code returned nothing"
            assert any("status_code" in sample["metric"] for sample in by_code), by_code
            # The counters the seven panels query are still present and correct:
            # dropping labels must not merge or double-count them.
            totals = query(base, "cloudflared_tunnel_total_requests")
            assert len(totals) == 1, totals
            assert float(totals[0]["value"][1]) == 4.0, totals[0]["value"]
            locations = query(base, "cloudflared_tunnel_server_locations")
            assert len(locations) == 1, locations
            assert float(locations[0]["value"][1]) == 1.0, locations[0]["value"]

            # Control: with the rendered relabel removed, the same exposition
            # keeps the identity labels. Without this the assertion above could
            # pass on a store that simply never received them.
            controls = run_label_control(vm_binary, job, exposition, scratch, expo_port)
            assert controls["leaked"], controls
            (scratch / "label-policy.json").write_text(json.dumps({
                "regex": drops[0]["regex"],
                "identities_absent": True,
                "status_code_present": True,
                "total_requests": 4.0,
                "control_leaked": sorted(controls["leaked"]),
            }, indent=2, sort_keys=True))
            return {"regex": drops[0]["regex"], "control_leaked": sorted(controls["leaked"])}
        finally:
            store.stop()


def run_label_control(vm_binary, rendered_job, exposition, scratch, expo_port):
    """The same exposition, scraped with the rendered `labeldrop` removed."""
    control_port = free_port()
    job = json.loads(json.dumps(rendered_job))
    job.pop("metric_relabel_configs", None)
    job["static_configs"] = [{"targets": [f"127.0.0.1:{expo_port}"]}]
    config = {"global": {"scrape_interval": "1s", "scrape_timeout": "1s"},
              "scrape_configs": [job]}
    path = scratch / "labelcontrol-scrape.yml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    data = scratch / "vm-labelcontrol"
    shutil.rmtree(data, ignore_errors=True)
    store = Owned(
        [
            vm_binary,
            f"-httpListenAddr=127.0.0.1:{control_port}",
            f"-storageDataPath={data}",
            f"-promscrape.config={path}",
            "-promscrape.config.strictParse",
            "-search.latencyOffset=0s",
            "-search.disableCache",
        ],
        scratch / "vm-labelcontrol.log",
    )
    try:
        wait_http(f"http://127.0.0.1:{control_port}/health", [store], timeout=45)
        base = f"http://127.0.0.1:{control_port}"
        deadline = time.monotonic() + 40
        leaked: set[str] = set()
        while time.monotonic() < deadline:
            assert store.child.poll() is None, "the control store exited"
            try:
                for name in ("cloudflared_tunnel_server_locations",
                             "cloudflared_tunnel_user_hostnames_counts"):
                    for sample in query(base, name):
                        leaked.update(
                            label for label in sample["metric"]
                            if label in {"userHostname", "connection_id",
                                         "edge_location", "rpcName"}
                        )
                if leaked:
                    break
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(1)
        return {"leaked": leaked}
    finally:
        store.stop()


def run_dashboard_queries(scratch):
    """Run the shipped dashboard expressions against a real pinned store.

    The exposition carries the label census and the 200/503 split measured from
    the pinned tunnel; the store is the pinned `victoria-metrics` binary; the
    expressions are the ones the Frontdoor access dashboard ships. Nothing is
    rewritten between the three.

    This is a LOOPBACK replay, not a live tunnel: it proves that the queries an
    operator sees resolve to real data over the rendered scrape job, which is a
    different claim from "a public tunnel served these requests". The live-tunnel
    variant is recorded separately and may be ENV_BLOCKED.
    """
    vm_binary = os.environ["VICTORIA_METRICS_BIN"]
    queries_path = scratch / "frontdoor-queries.json"
    assert queries_path.is_file(), "the frontdoor dashboard queries were not captured"
    queries = json.loads(queries_path.read_text())
    assert queries, "the frontdoor dashboard has no queries"

    vm_port = free_port()
    scrape_config = yaml.safe_load((scratch / "frontdoor-scrape.yml").read_text())
    job = next(j for j in scrape_config["scrape_configs"]
               if j["job_name"] == "cloudflare-tunnel")
    scrape_config["global"] = {"scrape_interval": "1s", "scrape_timeout": "1s"}
    compiled = scratch / "dashboard-scrape.yml"

    receiver = Receiver(responder=lambda path: (
        (200, OWNED_IDENTITY_EXPOSITION) if path.startswith("/metrics") else (404, "no")
    ))
    data = scratch / "vm-dashboard"
    shutil.rmtree(data, ignore_errors=True)
    with receiver:
        expo_port = receiver.port
        job["static_configs"] = [{"targets": [f"127.0.0.1:{expo_port}"]}]
        compiled.write_text(yaml.safe_dump(scrape_config, sort_keys=False))
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
            scratch / "vm-dashboard.log",
        )
        try:
            wait_http(f"http://127.0.0.1:{vm_port}/health", [store], timeout=45)
            base = f"http://127.0.0.1:{vm_port}"
            deadline = time.monotonic() + 60
            got = {}
            while time.monotonic() < deadline:
                assert store.child.poll() is None, "the store exited"
                try:
                    got = {expression: query(base, expression) for expression in queries}
                    if all(got.values()):
                        break
                except (URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(1)
            else:
                raise AssertionError(
                    f"the frontdoor dashboard queries returned nothing: {got}"
                )
            # Every shipped expression resolves to data, including `up`.
            assert len(got) == len(queries), (len(got), len(queries))
            by_code_expression = next(
                expression for expression in queries if "response_by_code" in expression
            )
            codes = {sample["metric"].get("status_code")
                     for sample in got[by_code_expression]}
            assert codes >= {"200", "503"}, codes
            up_expression = next(expression for expression in queries if "up{" in expression)
            assert got[up_expression], got[up_expression]
            # Dropping identity labels must not merge or double-count the
            # counters. The exposition carries exactly one series of each.
            # `rate` needs two samples inside its window, which a bounded run
            # may not produce; these assertions read the counter/gauge directly
            # so they measure the value rather than the scrape cadence.
            for expression, expected in (
                ("max(cloudflared_tunnel_total_requests)", 4.0),
                ("max(cloudflared_tunnel_ha_connections)", 1.0),
                ("max(cloudflared_tunnel_concurrent_requests_per_tunnel)", 0.0),
            ):
                samples = query(base, expression)
                assert len(samples) == 1, (expression, samples)
                assert abs(float(samples[0]["value"][1]) - expected) < 1e-9, (
                    expression, samples)
            # No per-request data through the whole path.
            observable = {
                expression: [
                    {"metric": {k: v for k, v in sample["metric"].items()
                                if k != "instance"},
                     "value": sample["value"]}
                    for sample in samples
                ]
                for expression, samples in got.items()
            }
            encoded = json.dumps(observable)
            for forbidden in ("/dash-", "trycloudflare.com", "127.0.0.1", "User-Agent",
                              "userHostname", "connection_id", "edge_location",
                              "rpcName"):
                assert forbidden not in encoded, (forbidden, encoded[:400])
            return {
                "queries": len(queries),
                "nonempty": sum(1 for samples in got.values() if samples),
                "status_codes": sorted(codes),
                "mode": "loopback-replay",
            }
        finally:
            store.stop()


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
    # Gatus has no `--version` flag: it starts, reads its configuration and
    # exits, so the binary cannot identify itself. Identity is therefore pinned
    # by OCI reference and checked exactly -- a moved tag, a missing digest or a
    # digest naming something else must fail here.
    #
    # This guard used to iterate over digests *found* in the values file and
    # assert each was truthy. With no digest present the loop ran zero times and
    # passed, so `tag: v5.37.0` with the digest deleted satisfied the whole
    # check. The values are now read, required, and compared to the reviewed pin.
    assert Path(gatus_bin).is_file(), gatus_bin
    gatus_values = scratch / "gatus-values.yaml"
    assert gatus_values.is_file(), "the rendered Gatus values were not captured"
    pinned = GATUS_PIN
    text_values = gatus_values.read_text()
    # The declared image reference, read structurally rather than by regex over
    # whatever digests happen to appear.
    image = None
    for line_no, line in enumerate(text_values.splitlines()):
        match_repo = re.match(r"^\s*repository:\s*(\S*twin/gatus)\s*$", line)
        if match_repo:
            image = {"repository": match_repo.group(1)}
            for follow in text_values.splitlines()[line_no + 1:line_no + 6]:
                match = re.match(r"\s*(tag|digest):\s*\"?([^\"\s]+)\"?\s*$", follow)
                if match:
                    image[match.group(1)] = match.group(2)
            break
    assert image, "the rendered values declare no twin/gatus image"
    assert image.get("tag") == pinned["tag"], (
        f"the rendered Gatus tag is {image.get('tag')!r}, the reviewed pin is "
        f"{pinned['tag']!r}")
    assert "digest" in image, (
        "the rendered Gatus image carries no digest, so a moved tag cannot be "
        "detected")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"]), image["digest"]
    assert image["digest"] == pinned["digest"], (
        f"the rendered Gatus digest is {image['digest']}, the reviewed pin is "
        f"{pinned['digest']}")

    # Tie the binary under test to that declared artifact -- by extracting it
    # from the OCI image itself, not by trusting a checksum the caller supplies.
    # A caller-provided sha256 only proves the caller can compute a hash of the
    # file it just chose; it does not connect that file to the pinned image.
    provenance = verify_gatus_provenance(scratch, gatus_bin, image)

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
    try:
        with FlappingOrigin() as origin:
            results["frontdoor"] = run_frontdoor(cloudflared, origin, scratch)
    except TunnelUnavailable as error:
        # Recorded, not hidden: the tunnel path could not be exercised here.
        results["frontdoor"] = {"status": "ENV_BLOCKED", "reason": str(error)}
        print(f"[observability] runtime: ENV_BLOCKED frontdoor tunnel path: {error}")
    # The dashboard's own expressions must return data from the real store. The
    # direct exposition read above is necessary but not sufficient: it proves
    # the counters exist, not that the query the operator sees resolves to them.
    try:
        results["dashboard"] = run_dashboard_queries(scratch)
    except TunnelUnavailable as error:
        results["dashboard"] = {"status": "ENV_BLOCKED", "reason": str(error)}
        print(f"[observability] runtime: ENV_BLOCKED dashboard-via-tunnel path: {error}")
    results["label_policy"] = run_label_policy(scratch)
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
