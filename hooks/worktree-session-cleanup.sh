#!/usr/bin/env bash
# Advisory pre-session-end choice for a clean task worktree.
#
# SessionEnd cannot ask the user a question or block termination. Stop is the
# last interactive boundary, so this hook injects a choice only when the last
# assistant message looks like a completed task. It never removes anything;
# explicit `--decision remove` is required by bin/worktree-session-cleanup.sh.

source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"
# shellcheck source=lib/hook-cwd.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-cwd.sh"

if [ "${DEV_KIT_WORKTREE_CLEANUP_OFF:-0}" = "1" ]; then
  exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "worktree-session-cleanup.sh"
  exit 0
fi

HOOK_EVENT="$(printf '%s' "$INPUT" | jq -r '.hook_event_name // .hookEventName // ""' 2>/dev/null)"
[[ "$HOOK_EVENT" == "Stop" ]] || exit 0

LAST_MSG="$(printf '%s' "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null)"
if ! printf '%s' "$LAST_MSG" | grep -qiE \
  '(완료|마쳤|마무리|끝났|PR[[:space:]]*(생성|올렸|열|created|opened)|pull request[[:space:]]*(created|opened)|tests?[[:space:]]+(passed|통과)|\b(done|finished|fixed)\b)'; then
  exit 0
fi

extract_hook_cwd
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi
worktree_detect
[[ "$WORKTREE_DETECT" == "worktree" ]] || exit 0

WT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
WT_ROOT="$(cd "$WT_ROOT" && pwd -P)"
if [ -n "$(git -C "$WT_ROOT" status --porcelain --untracked-files=all 2>/dev/null)" ]; then
  exit 0
fi

CLEANUP="${BASH_SOURCE[0]%/*}/../bin/worktree-session-cleanup.sh"
if [ ! -x "$CLEANUP" ]; then
  exit 0
fi
CHOICE="$("$CLEANUP" --worktree "$WT_ROOT" --decision ask 2>/dev/null || true)"
[ -n "$CHOICE" ] || exit 0

BRANCH="$(git symbolic-ref --short -q HEAD 2>/dev/null || echo detached)"
CTX="[dev-kit worktree cleanup] This completed session is in a clean task worktree.
  branch: $BRANCH
  path:   $WT_ROOT
  Ask the user to choose KEEP or REMOVE before ending the session.
  KEEP:   leave the worktree and branch as-is.
  REMOVE: after explicit confirmation, run:
          $CLEANUP --worktree '$WT_ROOT' --decision remove
  REMOVE archives logs first, refuses dirty/retained worktrees, keeps the local branch, and never uses --force.
  The SessionEnd event itself cannot receive input, so do not delete from the hook without the user's explicit choice."
jq -nc --arg ctx "$CTX" \
  '{hookSpecificOutput:{hookEventName:"Stop",additionalContext:$ctx}}'
exit 0
