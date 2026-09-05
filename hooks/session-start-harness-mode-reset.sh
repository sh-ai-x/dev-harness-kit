#!/usr/bin/env bash
# session-start-harness-mode-reset.sh — SessionStart hook.
#
# Unconditionally resets .dev-kit/harness-mode.session.json to {"mode": "full"}
# at the start of every session. This is the mechanism behind the
# workflow-fast-mode-lean design principle "new window = strict by default" —
# fast/custom mode must be chosen explicitly every session, never inherited
# from a previous one. Best-effort: a missing python3 or unwritable .dev-kit/
# is not a hard failure (lib.harness_mode_state.read_state() already treats a
# missing/corrupt file as mode=full, so silently skipping here is still safe).

set -eo pipefail
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
if command -v python3 >/dev/null 2>&1; then
  (cd "$ROOT" && python3 -m lib.harness_mode_state write full) 2>/dev/null || true
fi
exit 0
