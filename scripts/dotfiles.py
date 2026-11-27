from duty import duty

from .common import ROOT

SOURCE = str(ROOT / "dotfiles")


@duty
def apply_dotfiles(ctx, dry_run: bool = False):
    """Apply the managed dotfiles to your home directory."""
    command = ["chezmoi", "apply", "--source", SOURCE]
    if dry_run:
        command.extend(["--dry-run", "--verbose"])
    ctx.run(command, capture=False)


@duty
def diff_dotfiles(ctx):
    """Show changes to the managed dotfiles."""
    ctx.run(["chezmoi", "diff", "--source", SOURCE], capture=False)
