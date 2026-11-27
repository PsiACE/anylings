import os
from pathlib import Path

from duty import duty

from .common import ROOT, kubectl, tofu

CLOUDFLARE = ROOT / "infra" / "cloudflare"
CONNECTOR = ("-n", "cloudflared")


@duty
def configure_cloudflare(ctx):
    """Copy local configuration templates without overwriting edits."""
    for destination in (ROOT / ".env", CLOUDFLARE / "terraform.tfvars"):
        template = destination.with_name(f"{destination.name}.example")
        content = template.read_bytes()
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as file:
            file.write(content)


def initialize(ctx):
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    state_dir = state_home / "anylings"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tofu(ctx, CLOUDFLARE, "init", f"-backend-config=path={state_dir / 'cloudflare.tfstate'}")


@duty(pre=[initialize])
def plan_cloudflare(ctx):
    """Preview Cloudflare changes."""
    tofu(ctx, CLOUDFLARE, "plan", env_file=ROOT / ".env")


@duty(pre=[initialize])
def apply_cloudflare(ctx):
    """Review and apply Cloudflare changes."""
    tofu(ctx, CLOUDFLARE, "apply", env_file=ROOT / ".env")


@duty
def status_cloudflare(ctx):
    """Show the Cloudflare connector and tunnel token sync status."""
    kubectl(
        ctx, *CONNECTOR, "get", "deployment/cloudflared", "externalsecret/cloudflared-token",
        "--ignore-not-found", capture=False,
    )
