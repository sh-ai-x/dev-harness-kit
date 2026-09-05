#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# provider-divergence-check.sh -- SessionStart hook.
#
# Emits an additionalContext nudge when the user local
# .env:CI_REVIEW_PROVIDER is in an unexpected state:
#
#   1. Set to a value OUTSIDE the allowlist (minimax/anthropic/deepseek)
#      -- a typo or stale value will break CI selection with a confusing
#      error deep inside the review workflow.
#   2. Set to a value DIFFERENT from the tracked .env.example default
#      -- the operator local expectation silently diverges from the
#      repo documented default; .env.example should be updated OR the
#      operator should accept they are opting out.
#   3. Completely missing -- the operator has never run bin/set-provider.sh
#      and may not realize CI needs an explicit selection.
#
# Detection logic (best-effort, fails open):
#   1. Read .env from the session cwd; CI_REVIEW_PROVIDER missing => silent
#      skip (this is case 3 -- a discrete nudge comes from a separate code
#      path below; if we can read .env.example we can still emit it).
#   2. Read .env.example CI_REVIEW_PROVIDER default for cross-reference.
#   3. Validate the local value against the allowlist mirrored from
#      bin/set-provider.sh. Off-list => high-priority nudge.
#   4. On-list but different from .env.example default => medium-priority
#      nudge suggesting either update template or accept opt-out.
#
# Fails open (exit 0, no nudge) when:
#   - jq is missing
#   - cwd is not a git working tree
#   - .env is unreadable or absent AND .env.example is also absent
#   - either value is empty (we silently allow unset as the operator
#     may want CI to fall back to vars.CI_REVIEW_PROVIDER alone)

set -uo pipefail

INPUT="$(cat)"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# Prefer cwd from the hook payload.
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  exit 0
fi

ALLOWLIST="minimax anthropic deepseek"

is_allowed() {
  case " $1 " in
    *" $2 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# Parse KEY=VALUE from .env-style file. Last occurrence wins; comments
# and blank lines ignored; quotes around the value are stripped so the
# behavior matches bin/set-provider.sh and lib/ci_setup.py.
read_key() {
  local f="$1" key="$2" line val=""
  [ -f "$f" ] || { printf '%s' ""; return; }
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "#"*|"") continue ;;
    esac
    local k="${line%%=*}" v="${line#*=}"
    if [ "$k" = "$key" ]; then
      val="$v"
      val="${val%\"}"
      val="${val#\"}"
      val="${val%\'}"
      val="${val#\'}"
    fi
  done < "$f"
  printf '%s' "$val"
}

LOCAL="$(read_key '.env' 'CI_REVIEW_PROVIDER')"
TEMPLATE="$(read_key '.env.example' 'CI_REVIEW_PROVIDER')"

# Case A -- never set: skip silently. bin/set-provider.sh reminders exist
# in pl; nudging on every SessionStart is too noisy.
[ -z "$LOCAL" ] && exit 0

# Case B -- off-list: hard reminder (will break CI).
if ! is_allowed "$ALLOWLIST" "$LOCAL"; then
  NUDGE="PROVIDER-OFFLIST WARNING: .env CI_REVIEW_PROVIDER is '$LOCAL' which is NOT in the allowlist (minimax / anthropic / deepseek). The CI review workflow will fail to dispatch. Fix with:
  bin/set-provider.sh minimax   # or one of the other two
The script refuses any other value (mirrors bin/set-provider.sh allowlist)."
  jq -nc --arg ctx "$NUDGE" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi

# Case C -- on-list but diverges from .env.example default.
if [ -n "$TEMPLATE" ] && [ "$TEMPLATE" != "$LOCAL" ] \
   && is_allowed "$ALLOWLIST" "$TEMPLATE"; then
  NUDGE="PROVIDER-DIVERGENCE: .env CI_REVIEW_PROVIDER='$LOCAL' differs from .env.example default='$TEMPLATE'. CI uses vars.CI_REVIEW_PROVIDER on GitHub (not your local .env). Your PR will be reviewed against the GitHub repo variable, not your local value. Pick one:
  - update .env.example to '$LOCAL' so the new default is documented for future operators (and run 'gh variable set CI_REVIEW_PROVIDER --body $LOCAL' on the GitHub repo)
  - revert to '$TEMPLATE' locally (bin/set-provider.sh $TEMPLATE)
This hook does NOT modify either file."
  jq -nc --arg ctx "$NUDGE" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi

# Default: aligned and on-list -- silent.
exit 0
