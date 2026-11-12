"""Validate cluster.toml, apply defaults, and emit the config as JSON.

Standalone usage (check config, CI): uv run --locked --no-dev template/scripts/validate.py [cluster.toml]
Schema export (just schema config): uv run --locked --no-dev template/scripts/validate.py --schema
In-process usage (makejinja plugin): from validate import load

Exits non-zero with one human-readable error per line on stderr when the
config is invalid.
"""

from pathlib import Path
from typing import Any, Literal, Self

import json
import re
import sys
import tomllib

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# Git hosts whose SSH host keys are bundled with the template; ssh:// URLs
# pointing anywhere else must provide repository.known_hosts.
KNOWN_SSH_HOSTS = ["github.com", "gitlab.com", "codeberg.org"]

REPO_URL_PATTERN = r"^(https?://|git://|ssh://git@)[^/]+/.+$"
# Helm chart versions are pinned; allow an optional leading "v" like v1.2.3.
CHART_VERSION_PATTERN = r"^v?[0-9]+\.[0-9]+\.[0-9]+$"
# A namespace is a DNS label: dots are not allowed.
NAMESPACE_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
# A PersistentVolumeClaim name is a DNS subdomain (RFC 1123), like a Service.
# Magpie and the agent runtime both reference an existing claim, so both use
# this one rule rather than a second approximation of it.
PVC_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
# The default image tag. Both component versions are part of the identity of a
# combined image, so the tag names both rather than either alone.
DEFAULT_IMAGE_TAG = "bub0.6.1-t3web0.1.8"
# A container image repository without a tag or digest: the tag is separate so
# the rendered values never carry two sources for the same version.
IMAGE_REPOSITORY_PATTERN = r"^[a-z0-9]+([._-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+$"
IMAGE_TAG_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$"
# Editor hint only: the empty string means "use an emptyDir". Runtime
# validation stays in the validators, where empty is allowed.
EMPTY_OR_PVC_NAME_PATTERN = r"^$|" + PVC_NAME_RE.pattern
# Editor hint for an optional registry digest: empty, or sha256 and 64 hex.
EMPTY_OR_DIGEST_PATTERN = r"^$|sha256:[0-9a-f]{64}"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Repository(Model):
    url: str = Field(pattern=REPO_URL_PATTERN)
    branch: str = Field(default="main", min_length=1)
    # Optional relative directory prefix inside the repository. Every source
    # path is resolved through it, so a monorepo and a dedicated cluster
    # repository share the same rendered layout.
    path: str = ""
    known_hosts: str = ""

    @field_validator("path")
    @classmethod
    def check_path(cls, value: str) -> str:
        if not value:
            return value
        if value.startswith("/") or value.startswith("~"):
            raise ValueError(f"{value!r} must be relative to the repository root")
        if "\\" in value:
            raise ValueError(f"{value!r} must use forward slashes")
        parts = value.rstrip("/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"{value!r} must not contain empty, '.' or '..' segments")
        return "/".join(parts)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.url.startswith("ssh://"):
            host = self.url.removeprefix("ssh://git@").split("/", 1)[0].split(":", 1)[0]
            if host not in KNOWN_SSH_HOSTS and not self.known_hosts:
                raise ValueError(
                    f"known_hosts is required for ssh:// URLs to {host!r} "
                    f"(host keys are only bundled for {', '.join(KNOWN_SSH_HOSTS)})"
                )
        return self


class Argocd(Model):
    # The single source for the chart version: bootstrap.sh and the
    # self-managed Application both read it, so they cannot drift.
    chart_version: str = Field(default="10.14.0", pattern=CHART_VERSION_PATTERN)
    namespace: str = Field(default="argo-system", pattern=NAMESPACE_PATTERN)


class Ingress(Model):
    # "none" keeps the cluster credential-free and reachable only inside the
    # cluster; "cloudflare-tunnel" runs cloudflared with a token file.
    mode: Literal["none", "cloudflare-tunnel"] = "none"
    # Path to the tunnel token, resolved relative to the config file. The
    # token itself is never a TOML value.
    tunnel_token_file: str = ""

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.mode == "cloudflare-tunnel" and not self.tunnel_token_file:
            raise ValueError(
                "tunnel_token_file is required when ingress.mode is 'cloudflare-tunnel'"
            )
        if self.mode == "none" and self.tunnel_token_file:
            raise ValueError(
                "tunnel_token_file must be empty when ingress.mode is 'none'"
            )
        return self


class Agents(Model):
    # The Bub agent runtime image is built from this repository but published to
    # a registry this template must not guess: an operator names it, so no
    # private registry or vendor path is baked into the rendered tree.
    image_repository: str = Field(pattern=IMAGE_REPOSITORY_PATTERN)
    # The image carries two independently versioned components (Bub and the T3
    # Code web server), so the publisher tags it `bub<version>-t3web<version>`.
    # The default must therefore name both; a bare T3 release here would label
    # the runtime with the version of its browser shell.
    image_tag: str = Field(default=DEFAULT_IMAGE_TAG, pattern=IMAGE_TAG_PATTERN)
    # An immutable pin. A tag can be moved; the registry digest the publisher
    # records cannot, so a deployment that must not drift sets this. Empty
    # leaves the tag as the only reference.
    #
    # No Field `pattern` here, for the same reason as the claim below: it would
    # run before the validator and reject the documented empty default.
    image_digest: str = Field(
        default="",
        json_schema_extra={"pattern": EMPTY_OR_DIGEST_PATTERN},
    )

    @field_validator("image_digest")
    @classmethod
    def check_image_digest(cls, value: str) -> str:
        if not value:
            return value
        # A registry digest is `sha256:` and 64 lowercase hex characters. An
        # algorithm that is not sha256 is not something this template pins.
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError(
                f"{value!r} is not a registry digest "
                "(expected sha256: followed by 64 lowercase hex characters)"
            )
        return value

    # Empty keeps the default ephemeral emptyDir /home/t3; a name opts into an
    # existing claim the operator created. The platform never creates a claim
    # or picks a StorageClass.
    #
    # No Field `pattern` here: it runs before field validators and would reject
    # the documented empty value, while an omitted key defaulted to the same ""
    # and passed. The validator below owns both cases.
    storage_existing_claim: str = Field(
        default="",
        json_schema_extra={"pattern": EMPTY_OR_PVC_NAME_PATTERN},
    )
    # File holding the gateway key the runtime presents to Magpie. Naming it
    # makes the render emit the `bub-magpie-gateway` Secret, its KSOPS
    # generator and a `secretKeyRef`; leaving it empty renders none of them,
    # so an instance without a shared gateway keeps no dead secret.
    gateway_secret: str = ""

    @field_validator("storage_existing_claim")
    @classmethod
    def check_storage_claim(cls, value: str) -> str:
        if not value:
            return value
        # A claim name is a DNS subdomain: 253 characters overall and at most
        # 63 per dot-separated label. A bare regex match would accept a single
        # over-long label, which the API server then rejects at sync time.
        if (
            len(value) > 253
            or not PVC_NAME_RE.fullmatch(value)
            or any(len(label) > 63 for label in value.split("."))
        ):
            raise ValueError(
                f"{value!r} is not a valid PersistentVolumeClaim name "
                "(lowercase DNS subdomain, at most 253 characters, "
                "each label at most 63)"
            )
        return value


class Magpie(Model):
    # Empty keeps the default ephemeral emptyDir /config volume. A name opts
    # into an existing claim in the application's own namespace; the platform
    # never creates that claim, so removing the app cannot delete its data.
    config_claim: str = ""
    # Directory holding `providers.json` and `caller-keys.json`, the gateway's
    # own two stores. Naming it makes the render emit the `magpie-seed` Secret
    # and the init container that copies those files into `/config/magpie`
    # only when absent. Empty renders neither, so an instance that configures
    # its providers at runtime keeps the ephemeral-volume default.
    seed_dir: str = ""

    @field_validator("config_claim")
    @classmethod
    def check_config_claim(cls, value: str) -> str:
        if not value:
            return value
        if len(value) > 253 or not PVC_NAME_RE.fullmatch(value):
            raise ValueError(
                f"{value!r} is not a valid PersistentVolumeClaim name "
                "(lowercase DNS subdomain, at most 253 characters)"
            )
        return value

    @field_validator("seed_dir")
    @classmethod
    def check_seed_dir(cls, value: str) -> str:
        # The render reads exactly two files from here and encrypts their
        # contents; an empty path would make the resolution silently read the
        # config directory instead.
        if not value:
            return value
        if not value.strip():
            raise ValueError("seed_dir must be empty or a directory path")
        return value


class Observability(Model):
    # Off by default: the slice needs ClusterIP-only access and its own
    # monitoring decision, so it never appears in a cluster that did not ask
    # for it. Enabling it deploys VictoriaMetrics single-node and Gatus.
    enabled: bool = False


class Spinkube(Model):
    # Off by default: the slice installs a cluster-scoped operator, a
    # cert-manager instance and admission webhooks, so it never appears in a
    # cluster that did not ask for it. Enabling it deploys cert-manager, the
    # Spin operator with its CRDs, a Spintainer executor and one sample app,
    # all in the `spinkube` namespace, reachable by ClusterIP only.
    enabled: bool = False


class Config(Model):
    model_config = ConfigDict(extra="forbid", title="cluster.toml")

    repository: Repository
    argocd: Argocd = Argocd()
    ingress: Ingress = Ingress()
    magpie: Magpie = Magpie()
    observability: Observability = Observability()
    spinkube: Spinkube = Spinkube()
    # Optional: a cluster without an operator-supplied registry simply has no
    # agent runtime. Making it required would block every existing deployment
    # and the native rehearsal on a registry that does not exist there.
    agents: Agents | None = None

    @model_validator(mode="after")
    def check_gateway_wiring(self) -> Self:
        # A gateway key the gateway never learned only produces 401s, and the
        # agent runtime is then unusable in a way that looks like an outage.
        # Both halves are seeded from one snapshot, so require them together.
        if self.agents and self.agents.gateway_secret and not self.magpie.seed_dir:
            raise ValueError(
                "agents.gateway_secret requires magpie.seed_dir: the gateway key "
                "must be one Magpie's caller-keys.json already records"
            )
        return self


def format_errors(error: ValidationError) -> str:
    lines = []
    for err in error.errors():
        loc = ".".join(str(part) for part in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"{loc}: {msg}" if loc else msg)
    return "\n".join(lines)


class ConfigError(Exception):
    pass


def resolve_relative(config_file: str | Path, value: str) -> Path:
    """Resolve a config-file-relative path; absolute paths are kept as-is."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (Path(config_file).resolve().parent / path).resolve()


def parse(config_file: str | Path) -> tuple[Config, Path]:
    """Validate config_file and return the model plus its resolved path."""
    path = Path(config_file).expanduser()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None

    try:
        return Config.model_validate(raw), path.resolve()
    except ValidationError as e:
        raise ConfigError(format_errors(e)) from None


# Validate config_file and return the defaulted config as a plain dict.
# Raises ConfigError with a human-readable message.
def load(config_file: str = "cluster.toml") -> dict[str, Any]:
    config, _ = parse(config_file)
    # Unset optionals stay in the dump as None rather than being dropped:
    # makejinja renders with StrictUndefined, so a template testing an
    # optional key needs the key to exist.
    return config.model_dump(mode="json")


# JSON Schema for editor completion and validation of cluster.toml (taplo's
# #:schema directive). Cross-field rules and the tunnel-token file check only
# exist in the model validators, so the schema is an editing aid, not the gate.
def schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **Config.model_json_schema(),
    }


def main() -> int:
    if sys.argv[1:] == ["--schema"]:
        json.dump(schema(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    try:
        data = load(sys.argv[1] if len(sys.argv) > 1 else "cluster.toml")
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1
    json.dump(data, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
