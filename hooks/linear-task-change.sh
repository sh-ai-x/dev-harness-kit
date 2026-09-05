#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
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

# Fail open with a stderr warning if jq is missing.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "linear-task-change.sh"
  exit 0
fi

# Prefer cwd from the hook payload (more authoritative than $PWD).
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
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

# Fast-path mirror of hooks/linear-autosync.sh: bail before
# forking Python when no activation source is present. The
# owner-gate + enabled + scope-change checks live in Python.
USER_ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
USER_ENV="$USER_ENV_DIR/dev-kit/.env"
if [ -z "${LINEAR_API_KEY:-}" ] && \
   [ ! -f "$USER_ENV" ] && \
   [ ! -f "$PWD/.dev-kit/.env.linear" ] && \
   [ ! -f "$PWD/.dev-kit/linear-config.json" ] && \
   [ ! -f "$PWD/.dev-kit/.enabled.json" ]; then
  exit 0
fi

# Disable-model-invocation users have no `python3` alias guaranteed.
# `task-change-sync` (not `auto-sync`) is the entry point that
# does the scope-change short-circuit before any Linear round-trip —
# see tools/linear_sync.py::task_change_sync.
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1; then
    "$py" "$PWD/tools/linear_sync.py" task-change-sync || true
    exit 0
  fi
done

exit 0
