#!/usr/bin/env bash
# tdd-guard.sh — PreToolUse hook. Blocks prod code edits without adjacent test file.
# MUST-L1 / MUST-12: advisory mode by default (exit 0). Hard-block (exit 2) only with --strict.
#
# Adapted from dev-harness/.claude/hooks/tdd-guard.sh (sh-ai-x/dev-harness).

set -eo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full,lite
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
require_jq "TDD GUARD"
INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
[ -z "$FILE" ] && exit 0
hook_stage_active tdd-guard || exit 0

# Session-scoped bypass via /dev-kit:guard-mode. Reset to "on" at every
# SessionStart (hooks/session-start-guard-mode-reset.sh) — never persists
# across sessions. Fails to "on" (enforced) if python3 is unavailable.
if [ "$(python3 -m lib.guard_mode_state get tdd_guard 2>/dev/null)" = "off" ]; then
  exit 0
fi
case "$FILE" in
  *.md|*.mdx|*.txt|*.rst|*.adoc|*.html|*.json|*.yaml|*.yml|*.toml|*.cfg|*.ini|*.sh) exit 0 ;;
  */docs/*|*/tools/*|*/scripts/*|*/bin/*|*/hooks/*|*/fixtures/*|*/eval/*) exit 0 ;;
esac

# Enforce paths
case "$FILE" in
  *)
    DECISION=$(python3 -m lib.tdd_scope_policy "$FILE" 2>/dev/null || echo judge)
    [ "$DECISION" = "exempt" ] && exit 0
    if [ "$DECISION" = "judge" ] && [ -f "${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/.dev-kit/.tdd-scope.json" ] && jq -e '.tdd_required == false' "${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/.dev-kit/.tdd-scope.json" >/dev/null 2>&1; then exit 0; fi
    ROOT="${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
    STATE="${ROOT}/.dev-kit/.tdd-cycle.json"
    if [ "$DECISION" = "required" ] || [ "$DECISION" = "judge" ]; then
      if [ ! -f "$STATE" ] || ! jq -e '.phase == "red" and (.exit_code | numbers) != 0' "$STATE" >/dev/null 2>&1; then
        deny "TDD GUARD" "RED evidence is required before this code edit. Run: python3 -m lib.tdd_cycle red -- <test command>"
      fi
    fi
    exit 0
esac
exit 0
