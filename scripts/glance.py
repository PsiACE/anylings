from duty import duty

from .common import kubectl


@duty
def ui_glance(ctx):
    """Open a local port forward to Glance."""
    kubectl(ctx, "-n", "glance", "port-forward", "svc/glance", "8088:8080", capture=False)
