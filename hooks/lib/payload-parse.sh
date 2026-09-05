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
deny() {
    local hook_prefix="$1"
    local reason="$2"
    jq -nc --arg hp "$hook_prefix" --arg r "$reason" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:($hp + ": " + $r)}}' \
        >&2
    exit 2
}
