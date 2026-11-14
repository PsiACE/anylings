#!/usr/bin/env bash
# Offline proofs for the fixed-target rebind rehearsal.
#
# These assert what the client sends and what the guards refuse, without a
# cluster, a kubeconfig or a credential. They are the half of the rehearsal that
# can run anywhere; the native half runs only in a disposable guest through
# `rehearse.py`.
set -euo pipefail
cd "$(dirname "$0")"

# A synthetic identity is created per run and removed with its directory, so the
# custody tests never touch an ambient key.
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
umask 077
export SOPS_AGE_KEY_FILE="$scratch/unused.age"

python3 -m unittest discover --pattern 'test_*.py' --top-level-directory . --verbose
