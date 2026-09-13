#!/usr/bin/env bash
# trace-verify.sh — best-effort PostToolUse producer for verify.* events.
#
# Only recognized verification commands are observed. The command text is
# capped before persistence; stdout/stderr are deliberately not copied into
# the trace. A missing/invalid tool response is recorded as exit 0 only when
# the runtime supplied no exit code, preserving the non-gating telemetry
# contract without pretending the command output was a test result.

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

COMMAND="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -n "$COMMAND" ] || exit 0

# Keep this set intentionally small and explicit. It covers the common
# project-level checks without treating arbitrary shell commands as evidence.
if ! printf '%s' "$COMMAND" | grep -qE '(^|[[:space:];&|])(pytest|python(3)?[[:space:]]+-m[[:space:]]+pytest|python(3)?[[:space:]]+-m[[:space:]]+unittest|npm[[:space:]]+(test|run[[:space:]]+build)|yarn[[:space:]]+(test|build)|pnpm[[:space:]]+(test|build)|make[[:space:]]+(test|check)|cargo[[:space:]]+test|go[[:space:]]+test|ruff[[:space:]]+check|mypy|eslint|tsc)([[:space:];&|]|$)'; then
  exit 0
fi

ROOT="$(printf '%s' "$INPUT_JSON" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -d "$ROOT" ] || ROOT="$PWD"
RUN_ID="$(printf '%s' "$INPUT_JSON" | jq -r '.run_id // .session_id // .sessionId // empty' 2>/dev/null || true)"
RUN_ID="${RUN_ID:-hook-$(date -u +%Y%m%d)}"
WORKFLOW_ID="${DEV_KIT_WORKFLOW_ID:-interactive-hooks}"
COMMAND_BOUNDED="${COMMAND//$'\n'/ }"
COMMAND_BOUNDED="${COMMAND_BOUNDED//$'\r'/ }"
COMMAND_BOUNDED="${COMMAND_BOUNDED:0:240}"

EXIT_CODE="$(printf '%s' "$INPUT_JSON" | jq -r '
  def number_or_empty:
    if type == "number" then .
    elif type == "string" then (try tonumber catch empty)
    else empty end;
  [(.exit_code | number_or_empty),
   (.exitCode | number_or_empty),
   (if (.tool_response | type) == "object" then (.tool_response.exit_code | number_or_empty) else empty end),
   (if (.tool_response | type) == "object" then (.tool_response.exitCode | number_or_empty) else empty end),
   (if (.tool_response | type) == "string" then
      (try ((.tool_response
        | capture("(?i)(?:exit(?:ed|s)?|exit_code)[^0-9]{0,20}(?<code>[0-9]+)")
        | .code) | tonumber) catch empty)
    else empty end)]
  | map(select(. != null)) | .[0] // 0
' 2>/dev/null || printf '0')"
case "$EXIT_CODE" in
  ''|*[!0-9-]*) EXIT_CODE=0 ;;
esac

# Read the latest write and any recovery marker in one plugin-root Python
# process. Passing values through the environment avoids shell interpolation
# of untrusted payload content. Output is fixed-width and bounded.
PLUGIN_ROOT_FOR_TRACE="$(trace_plugin_root)"
ERROR_ROOT_FOR_TRACE="$(trace_root_for_log "$ROOT")"
ERROR_LOG_FOR_TRACE="$ERROR_ROOT_FOR_TRACE/.dev-kit/trace/emitter-errors.log"
mkdir -p "$ERROR_ROOT_FOR_TRACE/.dev-kit/trace" 2>/dev/null || true
CONTEXT="$(
  TRACE_VERIFY_ROOT="$ROOT" TRACE_VERIFY_RUN_ID="$RUN_ID" \
    TRACE_VERIFY_WORKFLOW_ID="$WORKFLOW_ID" \
    PYTHONPATH="$PLUGIN_ROOT_FOR_TRACE${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - <<'PY' 2>>"$ERROR_LOG_FOR_TRACE" || true
import json
import os
from pathlib import Path

from lib.trace_log import read_events

events = [
    event for event in read_events(Path(os.environ["TRACE_VERIFY_ROOT"]))
    if event.get("run_id") == os.environ.get("TRACE_VERIFY_RUN_ID")
    and event.get("workflow_id") == os.environ.get("TRACE_VERIFY_WORKFLOW_ID")
]
writes = [event for event in events if event.get("event_type") == "write.observed"]
if not writes:
    print(json.dumps({"subject": "verify:interactive", "parent": "", "retry_count": 0}))
else:
    write = max(writes, key=lambda event: event.get("ts", ""))
    subject = write["subject_id"]
    related = [event for event in events if event.get("subject_id") == subject]
    failures = [event for event in related if event.get("event_type") == "verify.failed"]
    failed = max(failures, key=lambda event: event.get("ts", "")) if failures else None
    heals = [event for event in related if event.get("event_type") == "heal.attempted"]
    heal = max(heals, key=lambda event: event.get("ts", "")) if heals else None
    recovering = bool(
        failed and heal
        and failed.get("ts", "") <= heal.get("ts", "") <= write.get("ts", "")
        and not any(
            event.get("event_type") == "verify.passed"
            and event.get("ts", "") > heal.get("ts", "")
            for event in related
        )
    )
    retry_count = len([
        event for event in heals
        if not failed or event.get("ts", "") > failed.get("ts", "")
    ])
    print(json.dumps({
        "subject": subject[:256],
        "parent": heal["event_id"] if recovering else write["event_id"],
        "retry_count": retry_count,
    }))
PY
)"
[ -n "$CONTEXT" ] || CONTEXT='{"subject":"verify:interactive","parent":"","retry_count":0}'
SUBJECT="$(printf '%s' "$CONTEXT" | jq -r '.subject // "verify:interactive"' 2>/dev/null || printf 'verify:interactive')"
PARENT_ID="$(printf '%s' "$CONTEXT" | jq -r '.parent // empty' 2>/dev/null || true)"
RETRY_COUNT="$(printf '%s' "$CONTEXT" | jq -r '.retry_count // 0' 2>/dev/null || printf '0')"
case "$RETRY_COUNT" in
  ''|*[!0-9]*) RETRY_COUNT=0 ;;
esac

if [ "$EXIT_CODE" -eq 0 ]; then
  EVENT_TYPE="verify.passed"
  OUTCOME="passed"
  REQUIRED=true
else
  EVENT_TYPE="verify.failed"
  OUTCOME="failed"
  REQUIRED=false
fi
EVIDENCE="$(jq -cn --arg command "$COMMAND_BOUNDED" --argjson exit_code "$EXIT_CODE" \
  --argjson required_checks_passed "$REQUIRED" --argjson retry_count "$RETRY_COUNT" \
  '{command:$command,exit_code:$exit_code,required_checks_passed:$required_checks_passed,retry_count:$retry_count,independent:true,evidence_provenance:"tool-response",checks_run:["recognized-command"]}' \
  2>/dev/null || printf '{}')"
trace_emit_event "$ROOT" "$EVENT_TYPE" "$RUN_ID" "$WORKFLOW_ID" verify \
  "$SUBJECT" "$OUTCOME" "hook:trace-verify" "$EVIDENCE" "$PARENT_ID"
exit 0
