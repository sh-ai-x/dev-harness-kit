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

# trace_plugin_root — locate the installed plugin's Python package.
#
# Hooks run with the consumer project's cwd, not necessarily the checkout
# that contains this script. Prefer the runtime-provided plugin root and
# fall back to the directory containing this shared hook. The fallback keeps
# direct black-box tests and source checkouts working without changing the
# caller's process cwd.
trace_plugin_root() {
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
        printf '%s' "$CLAUDE_PLUGIN_ROOT"
        return 0
    fi
    if [ -n "${PLUGIN_ROOT:-}" ]; then
        printf '%s' "$PLUGIN_ROOT"
        return 0
    fi
    (cd "${BASH_SOURCE[0]%/*}/../.." 2>/dev/null && pwd) || printf '%s' "."
}

# trace_root_for_log ROOT — shell-side mirror of trace_log.resolve_trace_root.
# It is used only to place emitter diagnostics; the Python resolver remains
# the authority for the actual JSONL destination.
trace_root_for_log() {
    local root="${1:-$PWD}"
    if [ -n "${DEV_KIT_TRACE_ROOT:-}" ]; then
        case "$DEV_KIT_TRACE_ROOT" in
            /*) printf '%s' "$DEV_KIT_TRACE_ROOT" ;;
            *) printf '%s/%s' "$root" "$DEV_KIT_TRACE_ROOT" ;;
        esac
        return 0
    fi
    local common_dir
    common_dir="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null || true)"
    case "$common_dir" in
        /*/.git) printf '%s' "${common_dir%/.git}" ;;
        .git) printf '%s' "$root" ;;
        */.git) printf '%s' "$root/${common_dir%/.git}" ;;
        *) printf '%s' "$root" ;;
    esac
}

# trace_emit_event ROOT TYPE RUN_ID WORKFLOW STAGE SUBJECT OUTCOME SOURCE
# EVIDENCE_JSON [PARENT_ID] — best-effort shared event writer.
#
# The emitter is intentionally non-blocking for policy hooks: its stderr is
# retained for diagnosis, but an import/permission/JSONL failure never
# changes the guard's safety decision. Evidence is supplied by callers and
# must remain compact; this helper does not persist raw tool payloads.
trace_emit_event() {
    local root="$1"
    local event_type="$2"
    local run_id="$3"
    local workflow_id="$4"
    local stage="$5"
    local subject="$6"
    local outcome="$7"
    local source="$8"
    local evidence_json="$9"
    local parent_id="${10:-}"
    local plugin_root error_root error_log
    plugin_root="$(trace_plugin_root)"
    error_root="$(trace_root_for_log "$root")"
    error_log="$error_root/.dev-kit/trace/emitter-errors.log"
    mkdir -p "$error_root/.dev-kit/trace" 2>/dev/null || true

    local -a command_args=(
        -m lib.trace_log append-event
        --root "$root" --type "$event_type" --run-id "$run_id"
        --workflow-id "$workflow_id" --stage "$stage"
        --subject-id "$subject" --outcome "$outcome" --source "$source"
        --evidence-json "$evidence_json"
    )
    [ -n "$parent_id" ] && command_args+=(--parent "$parent_id")
    PYTHONPATH="$plugin_root${PYTHONPATH:+:$PYTHONPATH}" \
        python3 "${command_args[@]}" \
        >/dev/null 2>>"$error_log" || true
}

# trace_latest_event ROOT RUN_ID WORKFLOW_ID SUBJECT EVENT_TYPES — print the
# latest matching event as ``event_id<TAB>subject_id<TAB>event_type<TAB>ts``.
# This is a bounded lookup over the reducer's already-validated event view;
# failures are diagnostics only and never affect a caller's policy result.
trace_latest_event() {
    local root="$1"
    local run_id="$2"
    local workflow_id="$3"
    local subject="$4"
    local event_types="$5"
    local plugin_root error_root error_log
    plugin_root="$(trace_plugin_root)"
    error_root="$(trace_root_for_log "$root")"
    error_log="$error_root/.dev-kit/trace/emitter-errors.log"
    mkdir -p "$error_root/.dev-kit/trace" 2>/dev/null || true
    TRACE_LOOKUP_ROOT="$root" TRACE_LOOKUP_RUN_ID="$run_id" \
        TRACE_LOOKUP_WORKFLOW_ID="$workflow_id" TRACE_LOOKUP_SUBJECT="$subject" \
        TRACE_LOOKUP_TYPES="$event_types" \
        PYTHONPATH="$plugin_root${PYTHONPATH:+:$PYTHONPATH}" \
        python3 - <<'PY' 2>>"$error_log" || true
import os
import sys
from pathlib import Path

from lib.trace_log import read_events

types = set(os.environ.get("TRACE_LOOKUP_TYPES", "").split(","))
events = [
    event for event in read_events(Path(os.environ["TRACE_LOOKUP_ROOT"]))
    if event.get("run_id") == os.environ.get("TRACE_LOOKUP_RUN_ID")
    and event.get("workflow_id") == os.environ.get("TRACE_LOOKUP_WORKFLOW_ID")
    and event.get("subject_id") == os.environ.get("TRACE_LOOKUP_SUBJECT")
    and event.get("event_type") in types
]
if events:
    event = max(events, key=lambda item: item.get("ts", ""))
    sys.stdout.write("\t".join(str(event.get(key, "")) for key in
                                ("event_id", "subject_id", "event_type", "ts")))
PY
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
    # Policy-driven default ground_truth (issue #663).
    #
    # Earlier revisions defaulted `blocked→unsafe` and `allowed→legitimate`,
    # which made the prevention_quality reducer trivially score 100%
    # precision/recall against the same hook that emits the event — the
    # metric's definition was circular (Review Critical #1). Now the policy
    # default is `unknown` (no claim about correctness); the reducer skips
    # `unknown` events entirely, so the metric only scores guards that the
    # operator has explicitly classified via DEV_KIT_GROUND_TRUTH (golden
    # / adversarial runs).
    #
    # The override is validated against an allowed set so a stray
    # `DEV_KIT_GROUND_TRUTH=bogus` cannot silently become a TP/FP
    # misclassification (A10-5). Unknown values degrade to `unknown`.
    #
    # NOTE: every line in this block must stay commented. A bare
    # `DEV_KIT_GROUND_TRUTH ...` line here parses fine under `bash -n`
    # but executes as a command at runtime (exit 127), and the callers
    # (bash-guard.sh, destructive-confirm.sh, secret-scan.sh) run under
    # `set -eo pipefail` — the shell would die here, before deny() writes
    # its permissionDecision envelope, making every guard fail OPEN.
    local default_gt="unknown"
    local explicit_gt="${DEV_KIT_GROUND_TRUTH:-$default_gt}"
    case "$explicit_gt" in
        unsafe|legitimate|pending|unknown) ;;
        *) explicit_gt="unknown" ;;
    esac
    evidence="$(jq -cn --arg policy "$hook_prefix" --arg why "$reason" --arg gt "$explicit_gt" \
        '{policy_id:$policy, reason:$why, ground_truth:$gt}' 2>/dev/null || printf '{}')"
    local event_type="guard.blocked"
    [ "$outcome" = "allowed" ] && event_type="guard.allowed"
    [ "$outcome" = "ask" ] && event_type="guard.ask"
    trace_emit_event "$root" "$event_type" "$run_id" "$workflow_id" guard \
        "$subject" "$outcome" "hook:${hook_prefix}" "$evidence"
}

# allow HOOK_PREFIX REASON — record an allowed guard decision and return 0.
# This is telemetry only; callers must use it only on paths that previously
# exited 0 so the policy outcome remains unchanged.
allow() {
    local hook_prefix="$1"
    local reason="$2"
    emit_guard_event "$hook_prefix" "$reason" allowed
    exit 0
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
