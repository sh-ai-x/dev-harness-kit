#!/usr/bin/env bash
# linear-autosync.sh — PostToolUse:Edit|Write|MultiEdit hook.
#
# Fires after every Edit|Write/MultiEdit to keep the per-worktree
# Linear handoff current. The owner-gate + enabled check lives in
# Python (tools/linear_sync.py::auto_sync); this hook is the
# thin wrapper that runs on every save.
#
# Path resolution: prefer $PROJECT_DIR (set by the dev-kit runtime
# to the project root); fall back to $PWD. The auto-sync's scope
# is the project, not the user's cwd, so we explicitly cd to
# PROJECT_DIR before invoking Python.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Source the shared linear fast-path (activation-source guard +
# python3 lookup). Extracted in inspect-pass2 (2026-09-23) to
# eliminate the 4-copy duplication across the linear-* hooks.
# shellcheck source=lib/linear-fast-path.sh
source "${BASH_SOURCE[0]%/*}/lib/linear-fast-path.sh"

# Project dir defaults to the parent of .claude/ (the dev-kit
# runtime exports $CLAUDE_PROJECT_DIR; fall back to $PWD).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

linear_fast_path "$PROJECT_DIR" "auto-sync"
exit 0
