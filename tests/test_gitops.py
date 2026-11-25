import os
import select
import shlex
import subprocess
from contextlib import contextmanager
from urllib.request import Request, urlopen

import pytest


@pytest.fixture(scope="module")
def kubectl(tmp_path_factory, request):
    command = os.getenv("KUBECTL")
    if command:
        return shlex.split(command)

    config = tmp_path_factory.mktemp("kubeconfig") / "admin"
    request.addfinalizer(lambda: config.unlink(missing_ok=True))
    with config.open("x") as stream:
        config.chmod(0o600)
        subprocess.run(
            ["sudo", "-n", "k0s", "kubeconfig", "admin"], stdout=stream, check=True
        )
    return ["kubectl", "--kubeconfig", str(config)]


@pytest.fixture(scope="module", autouse=True)
def wait_for_gitops(kubectl):
    for app in ("argocd", "glance"):
        for condition in (
            "create",
            "jsonpath={.status.sync.status}=Synced",
            "jsonpath={.status.health.status}=Healthy",
        ):
            subprocess.run(
                [
                    *kubectl, "-n", "argocd", "wait", f"application/{app}",
                    f"--for={condition}", "--timeout=180s",
                ],
                check=True,
            )


@contextmanager
def forward(kubectl, namespace, service, port):
    command = [
        *kubectl, "-n", namespace, "port-forward", "--address", "127.0.0.1",
        f"svc/{service}", f":{port}",
    ]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        try:
            assert select.select([process.stdout], [], [], 30)[0], f"Port-forward timed out: {service}"
            line = process.stdout.readline()
            prefix = "Forwarding from 127.0.0.1:"
            assert line.startswith(prefix), f"Port-forward failed: {service}"
            local_port = line.removeprefix(prefix).split()[0]
            yield f"http://127.0.0.1:{local_port}"
        finally:
            process.terminate()


def test_glance_bookmarks(kubectl):
    with forward(kubectl, "glance", "glance", 8080) as origin:
        with urlopen(f"{origin}/api/pages/anylings/content/", timeout=10) as response:
            page = response.read().decode()
    assert 'href="/argocd"' in page
    assert 'href="https://github.com/PsiACE/anylings"' in page


def test_argocd_subpath(kubectl):
    with forward(kubectl, "argocd", "argocd-server", 80) as origin:
        request = Request(f"{origin}/argocd", headers={"Accept": "text/html"})
        with urlopen(request, timeout=10) as response:
            page = response.read().decode()
    assert '<base href="/argocd/">' in page
