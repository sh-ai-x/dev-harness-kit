#!/usr/bin/env bash
# payload-parse.sh — shared stdin + JSON + content extraction for hooks.
#
# Source (do not execute) from any PreToolUse / PostToolUse hook that
# reads tool_input via stdin. Three helpers:
#
#   require_jq HOOK_NAME      — emit PreToolUse deny + exit 2 if jq missing
#   read_stdin_json HOOK_NAME — read stdin, validate JSON, set $INPUT_JSON
#   extract_content           — set $CONTENT from Write content +
#                               Edit new_string + every MultiEdit
#                               edits[].new_string (concatenated)
#
# All three fail closed: a missing tool or a malformed payload makes the
# hook exit 2 with a structured deny so Claude never silently bypasses
# the check on a degraded host. PostToolUse hooks (secret-scan,
# slop-detector) accept the same fail-closed contract: the deny JSON is
# accepted by Claude for any event name; the safer default is to deny
# rather than to skip scanning.

# Bail if executed directly — this file is meant to be sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  printf 'payload-parse.sh must be sourced, not executed.\n' >&2
  exit 1
fi

# require_jq HOOK_NAME — exit 2 with PreToolUse deny if jq is absent.
# Used by every scanner/guard hook to fail closed on hosts without jq
# (Alpine, stripped Docker, fresh macOS). HOOK_NAME is interpolated into
# the deny reason so the user can identify which hook is complaining.
require_jq() {
  local hook_name="${1:-HOOK}"
  if ! command -v jq >/dev/null 2>&1; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s: jq is required but not installed. Install jq (apt/brew/apk) — without it, this hook is a no-op."}}\n' "$hook_name" >&2
    exit 2
  fi
}

# read_stdin_json HOOK_NAME — read stdin into $INPUT_JSON.
# Empty stdin → $INPUT_JSON="" and return 0 (caller can short-circuit
# with `[ -z "$INPUT_JSON" ] && exit 0`).
# Malformed JSON → PreToolUse deny + exit 2 (fail closed).
read_stdin_json() {
  local hook_name="${1:-HOOK}"
  local input
  input="$(cat 2>/dev/null || true)"
  if [ -z "$input" ]; then
    INPUT_JSON=""
    return 0
  fi
  # `jq .` exits 0 for any valid JSON (including null/false/empty obj)
  # and exits 2 for parse errors. That's the right discriminator — we
  # want the hook to proceed on any structurally valid payload, not
  # only on objects.
  if ! printf '%s' "$input" | jq . >/dev/null 2>&1; then
    deny "$hook_name" "stdin payload is not valid JSON."
  fi
  INPUT_JSON="$input"
}

# extract_content — set $CONTENT to the joined write/edit/multiedit
# body. Returns "" when no recognized body field is present.
#
# Sources (in this order, concatenated with no separator):
#   - .tool_input.content         (Write tool)
#   - .tool_input.new_string      (Edit tool)
#   - .tool_input.edits[].new_string (MultiEdit tool, one per edit)
#
# Closing the MultiEdit scan-skip gap: scalar-only extraction returns
# "" for MultiEdit payloads, which makes secret-scan / slop-detector
# silently skip credential and slop-pattern checks. This helper joins
# every edit's new_string so MultiEdit is scanned end-to-end.
extract_content() {
  if [ -z "${INPUT_JSON:-}" ]; then
    CONTENT=""
    return 0
  fi
  CONTENT="$(printf '%s' "$INPUT_JSON" | jq -r '
    [
      (.tool_input.content // ""),
      (.tool_input.new_string // ""),
      (.tool_input.edits // [] | .[] | .new_string // "")
    ] | join("")
  ' 2>/dev/null || true)"
}


# deny HOOK_PREFIX REASON — emit PreToolUse deny JSON envelope to stderr and
# exit 2. Single source of truth for the 6 hook sites that previously each
# hand-built the envelope (3 different mechanisms: jq -nc --arg / heredoc /
# printf / echo). Uses jq for proper JSON escaping of $REASON (which may
# contain backticks / quotes / newlines / $variables from the call site).
# Emits to stderr per Claude Code hook contract (deny JSON via stderr +
# exit 2). Fails closed if jq is missing — callers must source this file
# AFTER require_jq.
emit_guard_event() {
    local hook_prefix="$1"
    local reason="$2"
    local outcome="${3:-blocked}"
    local root subject run_id workflow_id evidence
    root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
    subject="$(printf '%s' "${INPUT_JSON:-}" | jq -r '.tool_name // .tool_input.file_path // "unknown"' 2>/dev/null || printf 'unknown')"
    run_id="${DEV_KIT_RUN_ID:-hook-$(date -u +%Y%m%d)}"
    workflow_id="${DEV_KIT_WORKFLOW_ID:-hook:${hook_prefix}}"
    # Policy-driven default ground_truth (issue #663). Blocked guards are
    # presumed unsafe (the policy triggered), allowed guards presumed
    # legitimate (the policy did not trigger). Operators can override via
    DEV_KIT_GROUND_TRUTH for golden / adversarial runs.
    local default_gt="legitimate"
    [ "$outcome" = "blocked" ] && default_gt="unsafe"
    local explicit_gt="${DEV_KIT_GROUND_TRUTH:-$default_gt}"
    evidence="$(jq -cn --arg policy "$hook_prefix" --arg why "$reason" --arg gt "$explicit_gt" \
        '{policy_id:$policy, reason:$why, ground_truth:$gt}' 2>/dev/null || printf '{}')"
    local event_type="guard.blocked"
    [ "$outcome" = "allowed" ] && event_type="guard.allowed"
    [ "$outcome" = "ask" ] && event_type="guard.ask"
    python3 -m lib.trace_log append-event \
        --root "$root" --type "$event_type" --run-id "$run_id" \
        --workflow-id "$workflow_id" --stage guard --subject-id "$subject" \
        --outcome "$outcome" --source "hook:${hook_prefix}" --evidence-json "$evidence" \
        >/dev/null 2>&1 || true
}

deny() {
    local hook_prefix="$1"
    local reason="$2"
    emit_guard_event "$hook_prefix" "$reason"
    jq -nc --arg hp "$hook_prefix" --arg r "$reason" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:($hp + ": " + $r)}}' \
        >&2
    exit 2
}

# ask HOOK_PREFIX REASON — emit PreToolUse "ask" envelope and exit 0.
#
# The third permission tier, alongside `deny` (hard block, exit 2) and
# plain `exit 0` (silent allow). `ask` surfaces a confirmation prompt to
# the human before the tool runs — the only Claude Code mechanism that
# does so. Use it for destructive-but-legitimate operations where a hard
# deny would be wrong (the agent genuinely needs to push a branch, remove
# a worktree, or write a credential file) but silent execution is worse.
#
# Contract difference from `deny`: the envelope goes to STDOUT with exit
# 0, not stderr with exit 2. Claude Code reads a PreToolUse decision from
# stdout JSON; an exit-2 "ask" would be coerced to a block. Emitting to
# stderr here would make the hook fail open (decision ignored, tool runs
# unconfirmed) — that is the bug this contract exists to prevent.
ask() {
    local hook_prefix="$1"
    local reason="$2"
    emit_guard_event "$hook_prefix" "$reason" ask
    jq -nc --arg hp "$hook_prefix" --arg r "$reason" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:($hp + ": " + $r)}}'
    exit 0
}
