#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."

mise="$HOME/.local/bin/mise"
curl -fsSL https://mise.run | \
  MISE_VERSION=2026.11.6 MISE_INSTALL_PATH="$mise" MISE_INSTALL_SKIP_IF_EXISTS=1 sh

"$mise" trust mise.toml
"$mise" install --locked
"$mise" exec -- uv sync --locked --managed-python --no-dev
