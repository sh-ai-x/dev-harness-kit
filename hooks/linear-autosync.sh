#!/usr/bin/env bash
# linear-autosync.sh — PostToolUse:Edit|Write|MultiEdit hook.
#
# Fires after every Edit|Write/MultiEdit to keep the per-worktree
# Linear handoff current. The owner-gate + enabled check lives in
# Python (tools/linear_sync.py::auto_sync); this hook is the
# thin wrapper that runs on every save.
#
# Path resolution: extract `cwd` from the JSON payload (more
# authoritative than $PWD). Fall back to CLAUDE_PROJECT_DIR or $PWD
# if the payload has no `cwd` field. linear-fast-path.sh was
# extracted in inspect-pass2 (2026-09-23) to dedupe the 4-copy
# activation-source guard across the linear-* hooks.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Source the shared linear fast-path (activation-source guard +
# python3 lookup).
# shellcheck source=lib/linear-fast-path.sh
source "${BASH_SOURCE[0]%/*}/lib/linear-fast-path.sh"

# Extract `cwd` from the payload. linear-autosync.sh is unique among
# the linear-* hooks in that it parses the payload itself (the others
# rely on hook-preamble + hook-cwd.sh for HOOK_CWD); the original
# implementation predates the shared helper.
PROJECT_DIR="$(printf '%s' "${INPUT:-}" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
if [ -z "${PROJECT_DIR:-}" ]; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
fi

# PROJECT_DIR is reachable but doesn't look like a dev-harness-kit
# checkout (no tools/linear_sync.py). Bail silently — other Claude Code
# projects may share this hook and would otherwise emit "No such file"
# when linear-fast-path tries to invoke the python entry point.
# Restored in 2026-09-23 babysit after inspect-pass2 dropped this
# guard during the 4-way dedup (CI test_linear_autosync_hook::
# test_bails_when_no_tools_dir surfaced the regression).
if [ ! -f "$PROJECT_DIR/tools/linear_sync.py" ]; then
  exit 0
fi

linear_fast_path "$PROJECT_DIR" "auto-sync"
exit 0