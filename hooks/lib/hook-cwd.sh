#!/usr/bin/env bash
# hook-cwd.sh — extract the effective working directory from the hook
# payload. Shared by 9 hooks (session-start-check, worktree-janitor-
# session-start, worktree-auto-cut, worktree-session-cleanup,
# log-on-session-start, acp-tier-assert, linear-session-start,
# linear-task-change, provider-divergence-check) that previously
# copy-pasted the same 3-line `HOOK_CWD=... && cd ...` block.
#
# Extracted by inspect-pass4 (finding p10-p18, 2026-09-23) to
# eliminate the 9-copy duplication. The contract is:
#
#   - INPUT is set by hook-preamble.sh (`INPUT=$(cat)`) for every
#     hook that sources the preamble. This helper additionally
#     falls back to `cat` if INPUT is unset, so hooks that do NOT
#     source the preamble (worktree-session-cleanup, etc.) still
#     work.
#   - Sets HOOK_CWD to the value of the payload's `.cwd` field,
#     or "" if absent / jq-missing.
#   - Does NOT cd. The caller decides the failure mode:
#       cd "$HOOK_CWD" || exit 0   # SessionStart / UserPromptSubmit / Stop
#       cd "$HOOK_CWD" || true     # advisory hooks that tolerate failure
#       # or just inspect HOOK_CWD before deciding (acp-tier-assert,
#       # provider-divergence-check).
#
# Usage:
#   source "${BASH_SOURCE[0]%/*}/lib/hook-cwd.sh"
#   extract_hook_cwd
#   if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
#     cd "$HOOK_CWD" || exit 0
#   fi

extract_hook_cwd() {
  local _input="${INPUT:-}"
  if [ -z "$_input" ]; then
    _input="$(cat 2>/dev/null)"
  fi
  HOOK_CWD="$(printf '%s' "$_input" | jq -r '.cwd // ""' 2>/dev/null)"
  export HOOK_CWD
}