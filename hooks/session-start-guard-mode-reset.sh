#!/usr/bin/env bash
# session-start-guard-mode-reset.sh — SessionStart hook.
#
# Unconditionally resets .dev-kit/guard-mode.session.json to both guards
# "on" at the start of every session. Mirrors
# session-start-harness-mode-reset.sh's "new window = strict by default"
# design: /dev-kit:guard-mode off must be chosen explicitly every session,
# it never carries over from a previous one. Best-effort: a missing
# python3 or unwritable .dev-kit/ is not a hard failure (guard_mode_state's
# read_state() already treats a missing/corrupt file as all-"on", so
# silently skipping here is still safe).

set -eo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
if command -v python3 >/dev/null 2>&1; then
  (cd "$ROOT" && python3 -m lib.guard_mode_state reset) 2>/dev/null || true
fi
exit 0
