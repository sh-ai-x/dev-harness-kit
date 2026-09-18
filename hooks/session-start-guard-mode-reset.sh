#!/usr/bin/env bash
# session-start-guard-mode-reset.sh — SessionStart hook.
#
# Applies the scoped DEV_KIT_GUARDS policy to the session state. Every
# checkout starts with the thin default (all repository guards off) until
# project/local bootstrap configuration explicitly opts in. This hook never
# prompts and never treats main differently from a worktree.

set -eo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
HOOK_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  POLICY_LIB="$HOOK_DIR/lib/guard-policy.sh"
  (
    cd "$ROOT" || exit 0
    # shellcheck source=lib/guard-policy.sh
    source "$POLICY_LIB"
    DEV_KIT_GUARD_ROOT="$ROOT" dev_kit_guards_resolve
    DEV_KIT_GUARD_ROOT="$ROOT" \
      python3 -m lib.guard_mode_state reset \
        --policy "${DEV_KIT_GUARDS:-off}" \
        --source "${DEV_KIT_GUARDS_SOURCE:-default}" \
        --branch-class "$(DEV_KIT_GUARD_ROOT="$ROOT" dev_kit_guards_branch_class)"
  ) 2>/dev/null || true
fi
exit 0
