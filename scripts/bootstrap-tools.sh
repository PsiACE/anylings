#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."

version=2026.11.6
checksum=5fb74f80aab81e0a5ca20eb6414cc69b78ae0cab1deaeb521572ed26d8d4a6e3
mise=.tools/mise
if [[ ! -x "$mise" ]] || ! printf '%s  %s\n' "$checksum" "$mise" | sha256sum --check --status; then
  mkdir -p .tools
  download=$(mktemp .tools/mise.XXXXXX)
  trap 'rm -f -- "$download"' EXIT
  curl -fsSL --retry 3 -o "$download" \
    "https://github.com/jdx/mise/releases/download/v${version}/mise-v${version}-linux-x64"
  printf '%s  %s\n' "$checksum" "$download" | sha256sum --check
  chmod +x "$download"
  mv -f -- "$download" "$mise"
fi

"$mise" trust mise.toml
"$mise" install --locked
"$mise" exec -- uv sync --locked --managed-python
