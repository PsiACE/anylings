import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def authorize(ctx):
    if subprocess.run(["sudo", "-n", "true"], stderr=subprocess.DEVNULL).returncode:
        ctx.run(["sudo", "-v"], capture=False)


def k0s(ctx, *args, **options):
    return ctx.run(["sudo", "k0s", *map(str, args)], **options)


def kubectl(ctx, *args, **options):
    return k0s(ctx, "kubectl", *args, **options)


def tofu(ctx, directory, *args, env_file=None):
    command = ["tofu", f"-chdir={directory}", *args]
    if env_file is not None:
        command = ["op", "run", f"--env-file={env_file}", "--", *command]
    ctx.run(command, capture=False)
