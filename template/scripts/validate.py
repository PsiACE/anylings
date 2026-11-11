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
# A container image repository without a tag or digest: the tag is separate so
# the rendered values never carry two sources for the same version.
IMAGE_REPOSITORY_PATTERN = r"^[a-z0-9]+([._-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+$"
IMAGE_TAG_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$"
CLAIM_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"


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
    # The T3 Code runtime image is built from this repository but published to
    # a registry this template must not guess: an operator names it, so no
    # private registry or vendor path is baked into the rendered tree.
    image_repository: str = Field(pattern=IMAGE_REPOSITORY_PATTERN)
    image_tag: str = Field(default="0.1.8", pattern=IMAGE_TAG_PATTERN)
    # An existing claim to mount at /home/t3 instead of an emptyDir. Empty means
    # the pod gets ephemeral state and the operator is told so; this template
    # never creates a claim or picks a StorageClass on its own.
    storage_existing_claim: str = Field(default="", pattern=CLAIM_NAME_PATTERN)

    @field_validator("storage_existing_claim")
    @classmethod
    def check_claim(cls, value: str) -> str:
        if value == "":
            return value
        # A claim name is a DNS subdomain; keep it to the conservative subset so
        # an accidental path or Secret reference cannot be expressed here.
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value):
            raise ValueError(f"{value!r} is not a valid PersistentVolumeClaim name")
        return value


class Config(Model):
    model_config = ConfigDict(extra="forbid", title="cluster.toml")

    repository: Repository
    argocd: Argocd = Argocd()
    ingress: Ingress = Ingress()
    # Optional: a cluster without an operator-supplied registry simply has no
    # agent runtime. Making it required would block every existing deployment
    # and the native rehearsal on a registry that does not exist there.
    agents: Agents | None = None


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
