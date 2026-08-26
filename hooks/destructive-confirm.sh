#!/usr/bin/env bash
# destructive-confirm.sh — PreToolUse "ask" gate for destructive-but-legitimate ops.
#
# The third tier of the guard stack. The other two already existed:
#
#   bash-guard / git-guard  → deny   (block outright, agent must reroute)
#   silent exit 0           → allow  (invisible to the human)
#
# ...which left no way to say "this is probably fine, but a human should
# see it first." Before this hook, the repo had ZERO uses of Claude Code's
# `permissionDecision: "ask"` — the only mechanism that surfaces a
# confirmation prompt to the user before a tool runs. Every destructive
# action was therefore either hard-denied or executed silently.
#
# Covers three classes:
#
#   1. Credential/key file writes (.env, *.pem, id_rsa, .aws/credentials).
#      A deny would be wrong — writing .env.example or rotating a local
#      key is legitimate. Silent-allow is how a secret gets committed.
#   2. Bare `git worktree remove`, which bypasses bin/worktree-remove-safe.sh
#      and therefore discards the worktree's logs/ tree instead of archiving
#      it. The safe wrapper was previously enforced by convention only.
#   3. First-time branch push (`git push -u`) and force-with-lease. Neither
#      is catastrophic; both are externally visible.
#
# NOT stage-gated: an ask costs one keystroke and never blocks a correct
# action, so there is no stage where suppressing it is worth the risk.
# Fails closed on missing jq (inherited from require_jq).

set -eo pipefail
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
require_jq destructive-confirm
read_stdin_json destructive-confirm
[ -z "$INPUT_JSON" ] && exit 0

# Escape hatch, deliberately explicit. Unlike bash-guard's catastrophic
# tier (which ignores all overrides), an ask gate is safe to disable —
# the user opting out of their own confirmation prompt is coherent.
[ "${DEV_KIT_NO_CONFIRM:-0}" = "1" ] && exit 0

TOOL=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_name // ""')

# ---- Class 1: sensitive-path writes (Write|Edit|MultiEdit) ----
case "$TOOL" in
  Write|Edit|MultiEdit)
    FILE_PATH=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.file_path // ""')
    [ -z "$FILE_PATH" ] && exit 0
    BASE="${FILE_PATH##*/}"

    # Ordered most-specific first so the reason names the real reason.
    # .env.example / .env.sample are template files with no live secret —
    # asking on those trains the user to reflex-approve, so they are
    # excluded rather than matched and explained.
    case "$BASE" in
      .env.example|.env.sample|.env.template) exit 0 ;;
    esac

    SENSITIVE_REASON=""
    case "$FILE_PATH" in
      *.pem|*.key|*.p12|*.pfx)          SENSITIVE_REASON="private key / certificate material" ;;
      */.ssh/*|*/id_rsa*|*/id_ed25519*) SENSITIVE_REASON="SSH key material" ;;
      */.aws/credentials|*/.aws/config) SENSITIVE_REASON="AWS credential file" ;;
      */.netrc|*/.npmrc|*/.pypirc)      SENSITIVE_REASON="registry auth file" ;;
      */.kube/config)                   SENSITIVE_REASON="Kubernetes cluster credential" ;;
    esac
    if [ -z "$SENSITIVE_REASON" ]; then
      case "$BASE" in
        .env|.env.*)                    SENSITIVE_REASON="environment file (may hold live secrets)" ;;
        secrets.*|*credentials*.json)   SENSITIVE_REASON="credential store" ;;
      esac
    fi

    if [ -n "$SENSITIVE_REASON" ]; then
      ask "DESTRUCTIVE CONFIRM" \
        "$TOOL targets $SENSITIVE_REASON at '$FILE_PATH'. Confirm this write is intended and contains no live credential. Set DEV_KIT_NO_CONFIRM=1 to disable this gate."
    fi
    exit 0
    ;;
esac

# ---- Classes 2 & 3: destructive git plumbing (Bash) ----
[ "$TOOL" = "Bash" ] || exit 0
CMD=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // ""')
[ -z "$CMD" ] && exit 0

# Bare `git worktree remove` — but not the safe wrapper that calls it
# internally. Without this exclusion the hook would ask twice on the
# correct path and train the user to approve reflexively.
if echo "$CMD" | grep -qE "git .*worktree remove" && ! echo "$CMD" | grep -q "worktree-remove-safe.sh"; then
  ask "DESTRUCTIVE CONFIRM" \
    "bare 'git worktree remove' discards the worktree's logs/ tree. bin/worktree-remove-safe.sh archives it first and then removes. Confirm only if losing those logs is intended."
fi

if echo "$CMD" | grep -qE "git push .*--force-with-lease"; then
  ask "DESTRUCTIVE CONFIRM" \
    "force-with-lease rewrites remote history on this branch. Per rules/git-workflow.md this is allowed only on your own unmerged branch, never after review has started."
fi

if echo "$CMD" | grep -qE "git push (-u|--set-upstream)"; then
  ask "DESTRUCTIVE CONFIRM" \
    "first push of this branch to the remote — externally visible. Confirm the branch name and target remote are correct."
fi

exit 0
