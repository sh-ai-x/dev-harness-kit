#!/usr/bin/env bash
# trace-session-end.sh — emit step.completed for the session-scoped
# subject so the harness-effectiveness reducer's event_coverage metric
# has a clean lifecycle pair. Issue #702.
#
# Fires on SessionEnd (Claude Code) and Stop (Codex). Best-effort: any
# failure (missing jq, missing session_id, missing .dev-kit/trace dir)
# is suppressed with `|| true` so this hook never gates session end.
# When Stop / SessionEnd does not fire (SIGKILL, OOM, ExitWorktree),
# the matching step.started is left orphaned; the
# subject_observability submetric surfaces the orphan via its finding
# string (no heartbeat needed).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat), jq warn).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Warn (not fail) if jq is missing — fail-open contract.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "trace-session-end.sh"
  exit 0
fi

# Read the session_id + cwd from the stdin payload. Fall back gracefully
# if the runtime does not provide them (Codex may use different field
# names for sessionId, and some hooks run without a cwd field).
SESSION_ID=$(printf '%s' "${INPUT:-}" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

# Which hook trigger actually fired (Stop vs SessionEnd). This script is
# registered on both, and downstream diagnostics (e.g. why so few
# sessions reach a terminal event) need to know which path fired —
# an earlier revision hardcoded "SessionEnd" in evidence_ref regardless
# of trigger, making that diagnosis impossible.
HOOK_EVENT_NAME=$(printf '%s' "${INPUT:-}" | jq -r '.hook_event_name // "unknown"' 2>/dev/null || echo unknown)

# Resolve the worktree root. The payload's `cwd` is the source of truth
# (matches the cwd that produced step.started in session-start-check.sh,
# so the matching pair lands in the same trace file). Fall back to $PWD
# for runtimes that don't supply a cwd field.
PAYLOAD_CWD=$(printf '%s' "${INPUT:-}" | jq -r '.cwd // ""' 2>/dev/null || true)
if [ -n "$PAYLOAD_CWD" ] && [ -d "$PAYLOAD_CWD" ]; then
  EFFECTIVE_CWD="$PAYLOAD_CWD"
else
  EFFECTIVE_CWD="$PWD"
fi
[ -z "$EFFECTIVE_CWD" ] && exit 0

# Idempotency guard: trace-session-end.sh is registered on BOTH Stop
# (per-turn) AND SessionEnd (per-session) in hooks/hooks.json so the
# script's hook signature stays in parity with Codex's
# .codex-plugin/hooks/hooks.json (see tools/portability_check.py).
# Without this guard, a multi-turn Claude session emits one
# step.completed per turn, polluting the reducer's event_coverage
# denominator. Skip the append when a terminal event already exists
# for this session-scoped subject_id.
EVENTS_FILE="${EFFECTIVE_CWD}/.dev-kit/trace/events.jsonl"
SUBJECT_ID="session:${SESSION_ID}"
if [ -f "$EVENTS_FILE" ] && command -v jq >/dev/null 2>&1; then
  if jq -e --arg sid "$SUBJECT_ID" \
    'select(.subject_id == $sid and (.event_type == "step.completed" or .event_type == "step.failed" or .event_type == "step.blocked"))' \
    "$EVENTS_FILE" >/dev/null 2>&1; then
    exit 0
  fi
fi

# Emit the matching step.completed. subject_id is identical to the
# step.started subject so the reducer's set-intersection finds the pair.
python3 -m lib.trace_log append-event \
  --root "$EFFECTIVE_CWD" --type step.completed \
  --run-id "session:${SESSION_ID}" --workflow-id "session-lifecycle" \
  --stage session --subject-id "session:${SESSION_ID}" \
  --outcome completed --source "hook:trace-session-end" \
  --evidence-json "$(jq -nc --arg sid "$SESSION_ID" --arg hn "$HOOK_EVENT_NAME" '{session_id:$sid, hook_event:$hn}')" \
  >/dev/null 2>&1 || true

exit 0
