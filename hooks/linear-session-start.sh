#!/usr/bin/env bash
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

# Fail open with a stderr warning if jq is missing — the preamble
# already populated $WORKTREE_DETECT="" so the case below treats
# it as silent / no-op.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "linear-session-start.sh"
  exit 0
fi

# Prefer cwd from the hook payload (more authoritative than $PWD),
# fall back to PWD if missing.
# HOOK_CWD extraction + cd (shared via lib/hook-cwd.sh, see
# inspect-pass4 finding p12).
extract_hook_cwd
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

linear_fast_path "$PWD" "auto-sync"