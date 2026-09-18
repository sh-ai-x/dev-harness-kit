#!/usr/bin/env bash
# tdd-guard.sh — PreToolUse hook. Blocks prod code edits without adjacent test file.
# MUST-L1 / MUST-12: advisory mode by default (exit 0). Hard-block (exit 2) only with --strict.
#
# Adapted from dev-harness/.claude/hooks/tdd-guard.sh (sh-ai-x/dev-harness).

set -eo pipefail
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
source "${BASH_SOURCE[0]%/*}/lib/guard-policy.sh"
require_jq "TDD GUARD"
INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
[ -z "$FILE" ] && exit 0
hook_stage_active tdd-guard || exit 0

# Session state is reset from DEV_KIT_GUARDS at SessionStart. The thin
# default is off; guard-mode can still change this session explicitly.
GUARD_ROOT="${DEV_KIT_TDD_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}"
if [ "$(dev_kit_guard_state tdd_guard "$GUARD_ROOT")" = "off" ]; then
  exit 0
fi
case "$FILE" in
  *.md|*.mdx|*.txt|*.rst|*.adoc|*.html|*.json|*.yaml|*.yml|*.toml|*.cfg|*.ini|*.sh) exit 0 ;;
  */docs/*|*/tools/*|*/scripts/*|*/bin/*|*/hooks/*|*/fixtures/*|*/eval/*|*/tests/*) exit 0 ;;
esac

# Unknown code paths are conservative by default. The old prompt-time LLM
# judge was removed: an explicit build decision may still mark an ambiguous
# path as exempt in `.dev-kit/.tdd-scope.json`, while normal code edits require
# the same RED evidence as known core paths.
ROOT="${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SCOPE_STATE="${ROOT}/.dev-kit/.tdd-scope.json"
if [ -f "$SCOPE_STATE" ] && jq -e '.tdd_required == false' "$SCOPE_STATE" >/dev/null 2>&1; then
  exit 0
fi
STATE="${ROOT}/.dev-kit/.tdd-cycle.json"
if [ ! -f "$STATE" ] || ! jq -e '.phase == "red" and (.exit_code | numbers) != 0' "$STATE" >/dev/null 2>&1; then
  deny "TDD GUARD" "RED evidence is required before this code edit. Run: python3 -m lib.tdd_cycle red -- <test command>"
fi
exit 0
