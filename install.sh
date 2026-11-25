#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

mise="$HOME/.local/bin/mise"
curl -fsSL https://mise.run | \
  MISE_VERSION=2026.11.6 MISE_INSTALL_PATH="$mise" MISE_INSTALL_SKIP_IF_EXISTS=1 sh

"$mise" trust mise.toml
"$mise" install --locked
mkdir -p "$HOME/.local/bin"
launcher="$HOME/.local/bin/anylings"
if [[ $(readlink -- "$launcher" || true) != "$PWD/bin/anylings" ]]; then
  ln -sT -- "$PWD/bin/anylings" "$launcher"
fi
printf '%s\n' 'Tools are ready. Run anylings install to set up the host and cluster.'
