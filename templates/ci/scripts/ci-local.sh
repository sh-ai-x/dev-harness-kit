#!/usr/bin/env bash
# scripts/ci-local.sh — Local CI runner. No nektos/act required.
#
# Runs the same checks GitHub Actions runs in `.github/workflows/ci.yml`:
#   1. validate.py — installation + marker + bash syntax
#   2. test.sh     — pytest suite (skips if no tests/)
#   3. act -l      — list discovered workflows (optional, WARN if missing)
#
# Also runs the CC ↔ Codex hook-manifest parity check (issue #715)
# as a dedicated step so consumer repos catch drift on `hooks/hooks.json`
# vs `.codex-plugin/hooks/hooks.json` before the full test suite.
#
# Exit non-zero on any failure. Idempotent.

set -eo pipefail

# REPO_ROOT = the directory containing scripts/ci-local.sh (i.e. the
# parent of $(dirname "$0")). Two strategies, both safe:
#   1. Inside a git repo: `git rev-parse --show-toplevel` (authoritative)
#   2. Outside: fall back to a subshell that cd's to the parent of $0
#      then pwd's. The subshell scopes the cd so the fallback path
#      doesn't leak its pwd into REPO_ROOT alongside the toplevel.
#
# NOTE: the previous form  `git || cd ... && pwd`  silently broke on
# many setups: bash parses `A || B && C` as `(A || B) && C`, so `pwd`
# runs unconditionally and its output (always newline-terminated)
# concatenates with the toplevel path. `cd "$REPO_ROOT"` then sees
# two args and errors with "No such file or directory" referencing a
# path split across two lines. Fix: scope the fallback in a subshell
# so the inner pwd stays inside it.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
cd "$REPO_ROOT"

echo "=== validate ==="
if ! python3 scripts/validate.py; then
  echo "ci-local.sh: validate FAILED" >&2
  exit 1
fi
echo ""

echo "=== hooks-json-parity (issue #715) ==="
if [ -f tests/test_hooks_json_parity.py ]; then
  if ! python3 -m pytest tests/test_hooks_json_parity.py -v --tb=short; then
    echo "ci-local.sh: hooks-json-parity FAILED — CC vs Codex hook manifests drifted (issue #715)" >&2
    exit 1
  fi
else
  echo "ci-local.sh: tests/test_hooks_json_parity.py not present; skipping parity check."
fi
echo ""

echo "=== test ==="
if ! bash scripts/test.sh; then
  echo "ci-local.sh: test FAILED" >&2
  exit 1
fi
echo ""

if command -v act >/dev/null 2>&1; then
  echo "=== act (optional) ==="
  act -l 2>/dev/null || echo "act -l returned non-zero (this is informational only)"
else
  echo "act: not installed; skipping workflow listing."
  echo "  Install from https://nektos.act.dev for full GitHub Actions parity."
fi
echo ""

echo "ci-local.sh OK"
