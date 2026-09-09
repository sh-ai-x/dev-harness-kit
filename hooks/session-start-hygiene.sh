#!/usr/bin/env bash
# session-start-hygiene.sh — SessionStart advisory.
#
# Read-only soft-limit check. Never blocks. Emits `additionalContext` lines
# via the same envelope shape as session-start-check.sh:127-128 when:
#   - .worktrees/ exceeds WORKTREE_SOFT_LIMIT (default 50)
#   - ~/.claude/projects exceeds LOG_SOFT_LIMIT_MB (default 200)
# Suggests /dev-kit:worktree-prune or bin/worktree-janitor.sh / log-retention.sh.
#
# Fails open with stderr warning if jq missing (worktree_detect_jq_missing_warn).
# Mirrors session-start-guard-mode-reset.sh shape.

set -uo pipefail
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "session-start-hygiene.sh"
  exit 0
fi

HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi
ROOT="${HOOK_CWD:-$PWD}"

WT_SOFT="${WORKTREE_SOFT_LIMIT:-50}"
LOG_SOFT="${LOG_SOFT_LIMIT_MB:-200}"

NOTES=""

WT_COUNT="$(find "$ROOT/.worktrees" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
if [ "${WT_COUNT:-0}" -gt "$WT_SOFT" ]; then
  NOTES+="${WT_COUNT} worktrees above soft limit ${WT_SOFT}; run: /dev-kit:worktree-prune or bin/worktree-janitor.sh --dry-run"$'\n'
fi

LOG_MB="$(du -sm "$HOME/.claude/projects" 2>/dev/null | awk '{print $1}')"
if [ -n "$LOG_MB" ] && [ "$LOG_MB" -gt "$LOG_SOFT" ]; then
  NOTES+="${LOG_MB}MB transcripts above soft limit ${LOG_SOFT}MB; run: bin/log-retention.sh --dry-run"$'\n'
fi

[ -z "$NOTES" ] && exit 0
NOTES="${NOTES%$'\n'}"

jq -nc --arg ctx "$NOTES" --arg ev "SessionStart" \
  '{hookSpecificOutput:{hookEventName:$ev,additionalContext:$ctx}}'
exit 0
