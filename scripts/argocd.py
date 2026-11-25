from pathlib import Path
from tempfile import TemporaryDirectory

from duty import duty

from .common import ROOT, authorize, k0s, kubectl

ARGOCD = ROOT / "platform" / "argocd"
CHART = str(ARGOCD / "chart")
APP_CRD = "crd/applications.argoproj.io"
APP = "application/argocd"
NAMESPACE = ("-n", "argocd")


def exists(ctx, *args):
    return bool(kubectl(
        ctx, "get", *args, "--ignore-not-found", "-o", "name", capture="stdout",
    ).strip())


def install_chart(ctx):
    with TemporaryDirectory() as directory:
        kubeconfig = Path(directory) / "kubeconfig"
        kubeconfig.write_text(k0s(
            ctx, "kubeconfig", "admin", capture="stdout", silent=True, allow_overrides=False,
        ))
        kubeconfig.chmod(0o600)
        target = ("--kubeconfig", str(kubeconfig), *NAMESPACE)
        release = ctx.run(
            ["helm", "list", *target, "--deployed", "--filter", "^argocd$", "--short"],
            capture="stdout",
        )
        if not release.strip():
            ctx.run(["helm", "dependency", "build", CHART])
            ctx.run([
                "helm", "install", "argocd", CHART, *target, "--create-namespace",
                "--rollback-on-failure", "--timeout", "5m",
            ], capture=False)


@duty
def apply_argocd(ctx):
    """Install Argo CD if needed and wait for GitOps takeover."""
    authorize(ctx)
    if not (exists(ctx, APP_CRD) and exists(ctx, *NAMESPACE, APP)):
        install_chart(ctx)
        kubectl(ctx, "wait", "--for=condition=Established", APP_CRD, "--timeout=60s")
        for deployment in ("argocd-redis", "argocd-repo-server"):
            kubectl(ctx, *NAMESPACE, "rollout", "status", f"deployment/{deployment}", "--timeout=180s")
        kubectl(ctx, "apply", "-f", ARGOCD / "self.yaml")

    for field, value in (("sync", "Synced"), ("health", "Healthy")):
        kubectl(
            ctx, *NAMESPACE, "wait", APP,
            f"--for=jsonpath={{.status.{field}.status}}={value}", "--timeout=300s",
        )


@duty
def status_argocd(ctx):
    """Show Argo CD application status."""
    kubectl(ctx, *NAMESPACE, "get", "applications", capture=False)


@duty
def ui_argocd(ctx):
    """Open a local port forward to Argo CD."""
    kubectl(ctx, *NAMESPACE, "port-forward", "svc/argocd-server", "8081:80", capture=False)
