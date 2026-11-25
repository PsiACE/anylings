import os
from pathlib import Path

from duty import duty

from .common import ROOT, authorize, k0s, kubectl, tofu

CONFIG = ROOT / "infra" / "k0s"


@duty
def apply_k0s(ctx):
    """Provision k0s and wait for the node."""
    tofu(ctx, CONFIG, "init")
    authorize(ctx)
    tofu(ctx, CONFIG, "apply", "-auto-approve")
    for condition in ("create", "condition=Ready"):
        kubectl(ctx, "wait", f"--for={condition}", "nodes", "--all", "--timeout=360s")


@duty
def status_k0s(ctx):
    """Show Kubernetes nodes."""
    kubectl(ctx, "get", "nodes")



def caller_path(path):
    return Path(os.environ.get("ANYLINGS_CALLER_DIR", Path.cwd())) / path


@duty
def backup_k0s(ctx, directory):
    """Back up k0s state to an existing directory."""
    k0s(ctx, "backup", "--save-path", caller_path(directory))


@duty
def restore_k0s(ctx, archive):
    """Restore k0s state on a fresh host with k0s installed but stopped."""
    k0s(ctx, "restore", "--config-out=/etc/k0s/k0s.yaml", caller_path(archive))
