#!/usr/bin/env bash
# Explicit, idempotent promotion of Ralph's ephemeral phase artifacts.
#
# The Python module owns validation and publication. This wrapper keeps a
# stable operator-facing entry point and works from any caller cwd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found in PATH" >&2
  exit 3
fi

cd "$REPO_ROOT"
exec python3 -m lib.ralph_promote "$@"
