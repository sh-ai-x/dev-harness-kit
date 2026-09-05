#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# linear-session-start.sh — SessionStart hook.
#
# Fires once at every session start inside a Linear-configured
# worktree. Triggers one auto-sync round so a fresh session in
# a worktree (e.g. one cut minutes ago by `worktree-auto-cut.sh`
# or manually via `git worktree add`) is reflected in Linear
# immediately, without waiting for the first Edit|Write.
#
# The auto-sync is owner-gated inside tools/linear_sync.py::
# auto_sync — non-owners bail silently so contributors never
# leak their work into the owner's Linear workspace.
#
# Discriminator (worktree-detect.sh):
#   WORKTREE_DETECT=worktree → fire (sync into the worktree's handoff).
#   WORKTREE_DETECT=main     → silent (no per-worktree work yet; the
#                              worktree-create hook covers the cut case,
#                              and the Edit|Write hook covers in-place work).
#   WORKTREE_DETECT=outside  → silent (not a git working tree).
#   WORKTREE_DETECT=""       → silent (jq missing — fail open, no-op).
#
# Always exits 0 (non-blocking per #539).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Fail open with a stderr warning if jq is missing — the preamble
# already populated $WORKTREE_DETECT="" so the case below treats
# it as silent / no-op.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "linear-session-start.sh"
  exit 0
fi

# Prefer cwd from the hook payload (more authoritative than $PWD),
# fall back to PWD if missing.
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Discriminator: only fire in linked worktrees. Main checkout has no
# per-worktree task yet; the Edit|Write hook will fire once work
# starts (and the main checkout is also where the worktree-create
# hook fires if applicable).
case "$WORKTREE_DETECT" in
  worktree) ;;
  *) exit 0 ;;
esac

# Not a dev-harness-kit checkout — other Claude Code projects may
# share this hook. Bail silently.
if [ ! -f "$PWD/tools/linear_sync.py" ]; then
  exit 0
fi

# Fast-path mirror of hooks/linear-autosync.sh: bail before forking
# Python when no activation source is present. The owner-gate +
# enabled checks live in Python; this is just a micro-optimization.
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
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1; then
    "$py" "$PWD/tools/linear_sync.py" auto-sync || true
    exit 0
  fi
done

exit 0
