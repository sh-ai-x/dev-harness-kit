#!/usr/bin/env bash
# trace-write.sh — best-effort PostToolUse producer for write.observed.
#
# It records only the bounded, stable file identity and tool name. The hook
# never gates the completed Write/Edit/MultiEdit operation: trace failures are
# routed to emitter-errors.log by payload-parse.sh and the hook exits 0.

set -uo pipefail
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

INPUT_JSON="$(cat 2>/dev/null || true)"
[ -z "$INPUT_JSON" ] && exit 0
if ! printf '%s' "$INPUT_JSON" | jq empty >/dev/null 2>&1; then
  exit 0
fi

ROOT="$(printf '%s' "$INPUT_JSON" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -d "$ROOT" ] || ROOT="$PWD"
FILE_PATH="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
[ -n "$FILE_PATH" ] || exit 0
TOOL_NAME="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_name // "Write"' 2>/dev/null || printf 'Write')"

REPO_ROOT="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$ROOT")"
case "$FILE_PATH" in
  "$REPO_ROOT"/*) SUBJECT="${FILE_PATH#"$REPO_ROOT"/}" ;;
  *) SUBJECT="$FILE_PATH" ;;
esac
# Keep the reducer key and evidence bounded even for an adversarial payload.
SUBJECT="${SUBJECT//$'\n'/ }"
SUBJECT="${SUBJECT//$'\r'/ }"
SUBJECT="${SUBJECT:0:256}"
TOOL_NAME="${TOOL_NAME:0:32}"

RUN_ID="$(printf '%s' "$INPUT_JSON" | jq -r '.run_id // .session_id // .sessionId // empty' 2>/dev/null || true)"
RUN_ID="${RUN_ID:-hook-$(date -u +%Y%m%d)}"
WORKFLOW_ID="${DEV_KIT_WORKFLOW_ID:-interactive-hooks}"

# A write after a failed verification is the observable repair attempt. The
# ordering is intentional: emit heal first so a following verify can parent
# to it, then emit the write observation that remains the first-pass join key.
RECOVERY=""
FAILED_CONTEXT="$(trace_latest_event "$ROOT" "$RUN_ID" "$WORKFLOW_ID" "$SUBJECT" \
  "verify.failed,heal.attempted")"
IFS=$'\t' read -r FAILED_ID _FAILED_SUBJECT FAILED_TYPE _FAILED_TS <<< "$FAILED_CONTEXT" || true
if [ "$FAILED_TYPE" = "verify.failed" ] && [ -n "$FAILED_ID" ]; then
  HEAL_EVIDENCE="$(jq -cn --arg reason "write after failed verification" \
    --arg tool "$TOOL_NAME" '{reason:$reason,tool:$tool,attempt_kind:"interactive-write"}' 2>/dev/null || printf '{}')"
  trace_emit_event "$ROOT" heal.attempted "$RUN_ID" "$WORKFLOW_ID" write \
    "$SUBJECT" attempted "hook:trace-write" "$HEAL_EVIDENCE" "$FAILED_ID"
  # Parent the observed write to the durable heal event, not to the prior
  # failure. If the lookup is unavailable, retain the failure link rather
  # than inventing a successful recovery parent.
  HEAL_CONTEXT="$(trace_latest_event "$ROOT" "$RUN_ID" "$WORKFLOW_ID" "$SUBJECT" \
    "heal.attempted")"
  IFS=$'\t' read -r HEAL_ID _HEAL_SUBJECT _HEAL_TYPE _HEAL_TS <<< "$HEAL_CONTEXT" || true
  RECOVERY="${HEAL_ID:-$FAILED_ID}"
fi

EVIDENCE="$(jq -cn --arg path "$SUBJECT" --arg tool "$TOOL_NAME" \
  --argjson recovery "$( [ -n "$RECOVERY" ] && printf true || printf false )" \
  '{path:$path,tool:$tool,recovery_write:$recovery}' 2>/dev/null || printf '{}')"
trace_emit_event "$ROOT" write.observed "$RUN_ID" "$WORKFLOW_ID" write \
  "$SUBJECT" written "hook:trace-write" "$EVIDENCE" "$RECOVERY"
exit 0
