#!/usr/bin/env bash
# tdd-scope-judge.sh — UserPromptSubmit hook.
#
# Per-prompt TDD scope classifier. Fires on every user prompt and
# writes the decision to `.dev-kit/.tdd-scope.json`. `tdd-guard.sh`
# (PreToolUse Write|Edit|MultiEdit) reads that file and honors
# `tdd_required: false` to allow edits without TDD evidence (for
# doc fixes, config tweaks, one-off scripts, formatting, etc.).
#
# Why UserPromptSubmit (not a less-hot surface):
#   The judge needs the user's INTENT — the prompt text — to classify
#   the request. UserPromptSubmit is the only hook that sees the full
#   prompt; SessionStart sees only the first-prompt payload (and
#   only at session open); PreToolUse sees the file path but not the
#   user's reasoning.
#
# Why this is the canonical exception to the UserPromptSubmit lint:
#   `lib.tdd_scope_judge` calls `claude -p` (or `codex exec`) with a
#   45s subprocess timeout baked into the Python. The hook is
#   fail-safe: any error or timeout defaults to `tdd_required: true`
#   (the strict policy), so a stalled judge cannot accidentally allow
#   an edit that should require TDD. The 45s ceiling is well under
#   the hook's UserPromptSubmit gate budget — `tdd-scope-judge` was
#   never the source of the timeout cascade #836 (that was
#   `worktree-auto-cut.sh`'s unbounded `git fetch origin main`).
#
# Discriminator (worktree-detect.sh):
#   WORKTREE_DETECT=worktree → fire (every worktree session gets the
#                                per-prompt judgment)
#   WORKTREE_DETECT=main     → fire (in-worktree sessions always need it;
#                                main sessions are read-only investigations
#                                in practice — worktree-guard blocks edits)
#   WORKTREE_DETECT=outside  → silent (not a dev-harness-kit checkout)
#   WORKTREE_DETECT=""       → silent (jq missing — fail open, no-op)
#
# Opt-out paths (fail-safe defaults):
#   - `DEV_KIT_SKIP_TDD=1` short-circuits the LLM judge (issue #647
#     Option B). `tdd_required: false` is recorded so the guard allows
#     the edit. Use only when the judge runtime is unresponsive.
#   - `harness-mode fast` or `custom` with `tdd_scope_judge=off`
#     sets the gate to off (workflow-fast-mode-lean). Same effect.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Fail open with a stderr warning if jq is missing — the preamble
# already populated $WORKTREE_DETECT="" so the case below treats it
# as silent / no-op.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "tdd-scope-judge.sh"
  exit 0
fi

# Prefer cwd from the hook payload (more authoritative than $PWD).
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Discriminator: only fire in a dev-harness-kit checkout. `outside`
# covers non-plugin projects that borrowed these hooks.
case "$WORKTREE_DETECT" in
  worktree|main) ;;
  *) exit 0 ;;
esac

# Not a dev-harness-kit checkout — bail silently. Other Claude Code
# projects may share this hook.
if [ ! -f "$PWD/lib/tdd_scope_judge.py" ]; then
  exit 0
fi

# Extract the prompt payload. UserPromptSubmit hooks receive the full
# prompt text (or empty for an Enter press); both are valid.
PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
[ -n "$PROMPT" ] || exit 0

# Resolve the TDD root. The judge writes `.dev-kit/.tdd-scope.json`
# under this root; `tdd-guard.sh` reads from the same root via the
# `DEV_KIT_TDD_ROOT` env var. Falls back to the git toplevel when the
# env var is unset so the two sides agree on the path.
TDD_ROOT="${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

# Run the judge. The Python module owns its own subprocess timeout
# (`JUDGE_TIMEOUT_SECONDS = 45` inside lib/tdd_scope_judge.py) and
# fail-safe default (`tdd_required: true` on error), so a stalled
# `claude -p` cannot accidentally allow an edit that should require
# TDD. The hook itself exits 0 always (non-blocking per #539).
#
# The lint allows the canonical `python3 -m lib.<module>` shape on
# UserPromptSubmit; other python invocations (bare `python -c`, etc.)
# are forbidden. We use that one shape and exit silently if
# `python3` is missing — `tdd_scope_judge` is unavailable in that
# environment and the guard falls back to its strict policy
# (`tdd_required` defaults to true when no judge ran).
if command -v python3 >/dev/null 2>&1; then
  DEV_KIT_TDD_ROOT="$TDD_ROOT" \
    python3 -m lib.tdd_scope_judge --root "$TDD_ROOT" --prompt "$PROMPT" \
    >/dev/null 2>&1 || true
fi

exit 0