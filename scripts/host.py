from duty import duty

from .common import ROOT, authorize


@duty
def apply_host(ctx):
    """Configure the host with Ansible (Fedora)."""
    authorize(ctx)
    ctx.run(
        ["ansible-playbook", "-i", "localhost,", str(ROOT / "infra/host/fedora.yml")],
        workdir=ROOT,
        capture=False,
    )
