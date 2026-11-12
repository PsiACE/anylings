#!/usr/bin/env bash
# Prepare a synthetic platform input in a private directory.
#
# The Cloudflare Tunnel connector is part of the platform baseline, so every
# cluster.toml names a tunnel token file. The repository fixtures use
# `.local/tunnel.token`, resolved relative to the config file, so a check that
# copies a fixture must also place that file beside the copy. The value is a
# synthetic marker, never a credential, and the fixtures stay in the checkout
# unmodified: a check writes only into its own scratch directory.
#
# Sourced by the offline checks; not an executable entry point.
# Usage: prepare_platform_input <fixture.toml> <destination-dir>
# Prints the prepared config path on stdout.

prepare_platform_input() {
  local fixture="$1" destination="$2"
  mkdir -p "$destination/.local" || return 1
  cp "$fixture" "$destination/cluster.toml" || return 1
  printf 'synthetic-ci-tunnel-token\n' >"$destination/.local/tunnel.token" || return 1
  printf '%s\n' "$destination/cluster.toml"
}
