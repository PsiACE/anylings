import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from duty import duty

from .common import ROOT, authorize, kubectl, tofu

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
    """Show the local Cloudflare connector."""
    kubectl(ctx, *CONNECTOR, "get", "deployment/cloudflared", capture=False)


@duty
def connect_cloudflare(ctx, reference):
    """Save the Cloudflare connector token as a Kubernetes Secret."""
    authorize(ctx)
    token_value = ctx.run(
        ["op", "read", "--no-newline", reference],
        capture="stdout", silent=True, allow_overrides=False,
    )
    if not token_value:
        raise SystemExit("The 1Password connector token is empty.")
    with NamedTemporaryFile(mode="w", encoding="utf-8") as token:
        token.write(token_value)
        token.flush()
        manifest = kubectl(
            ctx, *CONNECTOR, "create", "secret", "generic", "cloudflared-token",
            f"--from-file=token={token.name}", "--dry-run=client", "-o", "yaml",
            capture="stdout", silent=True, allow_overrides=False,
        )
        kubectl(
            ctx, *CONNECTOR, "apply", "--server-side", "-f", "-",
            stdin=manifest, capture="stdout", allow_overrides=False,
        )


@duty
def rotate_cloudflare(ctx, reference):
    """Update the connector token and restart the connector."""
    connect_cloudflare(ctx, reference)
    kubectl(ctx, *CONNECTOR, "rollout", "restart", "deployment/cloudflared")
