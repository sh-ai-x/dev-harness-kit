#!/usr/bin/env bash
# linear-task-change.sh — UserPromptSubmit hook.
#
# Detects plan / task changes mid-session and triggers one
# auto-sync round. The detection is delegated to
# tools/linear_sync.py::task_change_sync, which compares the
# current scope (branch + latest commit subject) against the
# last-recorded handoff scope. When the scopes match, the
# function bails without a Linear round-trip; when they differ,
# it delegates to auto_sync (which is itself owner-gated).
#
# This hook closes the gap where a session sits idle after a
# branch change or a fresh commit and the next Edit|Write is
# many minutes away — the user can now expect a Linear update
# immediately after stating the new intent, not after the next
# accidental save.
#
# The hook is intentionally narrow: it does NOT inject any
# additionalContext. The handoff is the source of truth, the
# Linear issue is just the user's view of it; adding a textual
# nudge would compete with the model's own handling of the
# task-change signal.
#
# Always exits 0 (non-blocking per #539).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"
# Source the shared HOOK_CWD extractor (inspect-pass4 finding
# p10-p18). Sets HOOK_CWD from the payload; caller decides
# the cd failure mode.
# shellcheck source=lib/hook-cwd.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-cwd.sh"


# Source the shared linear fast-path (activation-source guard +
# python3 lookup). Extracted in inspect-pass2 (2026-09-23) to
# eliminate the 4-copy duplication across the linear-* hooks.
# shellcheck source=lib/linear-fast-path.sh
source "${BASH_SOURCE[0]%/*}/lib/linear-fast-path.sh"

# Fail open with a stderr warning if jq is missing.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "linear-task-change.sh"
  exit 0
fi

# Prefer cwd from the hook payload (more authoritative than $PWD).
# HOOK_CWD extraction + cd (shared via lib/hook-cwd.sh, see
# inspect-pass4 finding p13).
extract_hook_cwd
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Only fire inside git worktrees — the main checkout has no
# per-worktree task yet (the worktree-create + session-start
# hooks cover that path). `outside` and the jq-missing case
# (`""`) stay silent.
case "$WORKTREE_DETECT" in
  worktree) ;;
  *) exit 0 ;;
esac

# Not a dev-harness-kit checkout — bail silently.
if [ ! -f "$PWD/tools/linear_sync.py" ]; then
  exit 0
fi

linear_fast_path "$PWD" "task-change-sync"