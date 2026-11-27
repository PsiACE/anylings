from tempfile import NamedTemporaryFile

from duty import DutyFailure, duty

from .common import authorize, kubectl

NAMESPACE = ("-n", "external-secrets")
TOKEN_SECRET = "onepassword-service-account-token"


@duty
def connect_1password(ctx, reference):
    """Save the 1Password service account token as a Kubernetes Secret."""
    authorize(ctx)
    token_value = ctx.run(
        ["op", "read", "--no-newline", reference],
        capture="stdout", silent=True, allow_overrides=False,
    )
    if not token_value:
        raise SystemExit("The 1Password service account token is empty.")

    with NamedTemporaryFile(mode="w", encoding="utf-8") as token:
        token.write(token_value)
        token.flush()
        manifest = kubectl(
            ctx, *NAMESPACE, "create", "secret", "generic", TOKEN_SECRET,
            f"--from-file=token={token.name}", "--dry-run=client", "-o", "yaml",
            capture="stdout", silent=True, allow_overrides=False,
        )

    try:
        kubectl(
            ctx, *NAMESPACE, "apply", "--server-side", "-f", "-",
            stdin=manifest, capture="both", silent=True, allow_overrides=False,
        )
    except DutyFailure as error:
        raise SystemExit(f"Could not apply Secret/{TOKEN_SECRET} in external-secrets.") from error


@duty
def status_secrets(ctx):
    """Show ClusterSecretStore and ExternalSecret status across namespaces."""
    kubectl(ctx, "get", "clustersecretstore,externalsecret", "-A", capture=False)
