"""Collect component duties and define shared workflows."""

from duty import duty

# Duty discovers the tasks imported into this module.
from scripts.argocd import apply_argocd, status_argocd, ui_argocd
from scripts.cloudflare import (
    apply_cloudflare,
    configure_cloudflare,
    connect_cloudflare,
    plan_cloudflare,
    rotate_cloudflare,
    status_cloudflare,
)
from scripts.common import ROOT, authorize
from scripts.glance import ui_glance
from scripts.host import apply_host
from scripts.k0s import apply_k0s, backup_k0s, restore_k0s, status_k0s


@duty(pre=[apply_host, apply_k0s, apply_argocd])
def install(ctx):
    """Apply the local host, k0s, and Argo CD. Does not apply Cloudflare."""


@duty
def sign_in_1password(ctx):
    """Sign in to 1Password."""
    ctx.run(["op", "signin"], capture=False, silent=True, allow_overrides=False)


@duty
def test(ctx):
    """Run smoke tests against the local cluster."""
    authorize(ctx)
    ctx.run(["uv", "run", "--locked", "--managed-python", "pytest"], workdir=ROOT, capture=False)
