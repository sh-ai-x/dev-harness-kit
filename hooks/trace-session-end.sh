#!/usr/bin/env bash
# trace-session-end.sh — Stop + SessionEnd lifecycle adapter.
#
# Fires on both Stop (per-turn) AND SessionEnd (per-session) in
# hooks/hooks.json. The proposal (PR #817) mandates:
#   * Stop ONLY requests a collect(). It never closes a session.
#   * Actual SessionEnd records controller_close + observed_terminal
#     through lib.effectiveness_collection.
#   * Multiple Stops in a row never produce multiple closures.
# Without this split, a multi-turn session would emit one closure per
# turn, polluting the reducer's denominator.
#
# Best-effort: any failure (missing jq, missing session_id, missing
# .dev-kit/trace dir) is suppressed with `|| true` so this hook never
# gates session end. When neither Stop nor SessionEnd fires (SIGKILL,
# OOM, ExitWorktree), the matching enrollment stays unresolved; the
# envelope's `missing_terminal` count surfaces it.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat), jq warn).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Warn (not fail) if jq is missing — fail-open contract.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "trace-session-end.sh"
  exit 0
fi

# Read the session_id + cwd from the stdin payload.
SESSION_ID=$(printf '%s' "${INPUT:-}" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

# Which hook trigger actually fired (Stop vs SessionEnd). The Stop path
# only triggers a collect(); SessionEnd records the close.
HOOK_EVENT_NAME=$(printf '%s' "${INPUT:-}" | jq -r '.hook_event_name // .hookEventName // "unknown"' 2>/dev/null || echo unknown)

# Resolve the worktree root.
PAYLOAD_CWD=$(printf '%s' "${INPUT:-}" | jq -r '.cwd // ""' 2>/dev/null || true)
if [ -n "$PAYLOAD_CWD" ] && [ -d "$PAYLOAD_CWD" ]; then
  EFFECTIVE_CWD="$PAYLOAD_CWD"
else
  EFFECTIVE_CWD="$PWD"
fi
[ -z "$EFFECTIVE_CWD" ] && exit 0

# Common lib path so the python helpers can be invoked.
LIB_DIR="${BASH_SOURCE[0]%/*}/../lib"

# 1) Idempotent enroll + observed_start (best-effort, never blocks).
#    The existing legacy `events.jsonl` emission at the bottom of this
#    file is unchanged; the new bounded journal is an additive layer
#    that lives in .dev-kit/trace/measurement/.
#
# Security: A05 injection defense-in-depth. The hook payload
# (SESSION_ID, HOOK_EVENT_NAME, PAYLOAD_CWD) is untrusted from the
# shell's perspective — any value containing a `"` would break out of
# the heredoc string literal and execute arbitrary Python. We pass
# every value through os.environ after exporting it as a discrete
# variable, and the heredoc terminator is single-quoted (<<'PY') so
# bash performs NO expansion inside the script. The Python source
# becomes a static literal regardless of the payload's contents.
EFFECTIVE_CWD="$EFFECTIVE_CWD" \
  SESSION_ID="$SESSION_ID" \
  HOOK_EVENT_NAME="$HOOK_EVENT_NAME" \
  LIB_DIR="$LIB_DIR" \
  python3 - <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ["LIB_DIR"])
from effectiveness_collection import (
    enroll, observe, TRANSITION_OBSERVED_START,
)
root = os.environ["EFFECTIVE_CWD"]
sid = os.environ["SESSION_ID"]
hn = os.environ["HOOK_EVENT_NAME"]
try:
    enroll(root, run_id=f"session:{sid}", workflow_id="session-lifecycle",
           subject_id=f"session:{sid}", attempt_id=f"sess-{sid}",
           controller="session")
    observe(root, run_id=f"session:{sid}", workflow_id="session-lifecycle",
            subject_id=f"session:{sid}", attempt_id=f"sess-{sid}",
            transition=TRANSITION_OBSERVED_START, outcome="started",
            payload={"hook_event": hn})
except Exception:
    pass
PY

# 2) Stop: collect() only. Never record a closure.
if [ "$HOOK_EVENT_NAME" = "Stop" ]; then
  EFFECTIVE_CWD="$EFFECTIVE_CWD" \
    LIB_DIR="$LIB_DIR" \
    python3 - <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ["LIB_DIR"])
from effectiveness_collection import collect
try:
    collect(os.environ["EFFECTIVE_CWD"])
except Exception:
    pass
PY
  # Legacy: still emit one step.completed to events.jsonl on Stop so
  # the existing trajectory reducer (lib/trace_log.py) and the
  # test_trace_session_end_hook.py regression contract stay green.
  # The bounded journal path above is additive; this emission is
  # idempotent (one terminal per session-scoped subject) and preserves
  # the existing public evidence surface.
  python3 -m lib.trace_log append-event \
    --root "$EFFECTIVE_CWD" --type step.completed \
    --run-id "session:${SESSION_ID}" --workflow-id "session-lifecycle" \
    --stage session --subject-id "session:${SESSION_ID}" \
    --outcome completed --source "hook:trace-session-end" \
    --evidence-json "$(jq -nc --arg sid "$SESSION_ID" --arg hn "$HOOK_EVENT_NAME" '{session_id:$sid, hook_event:$hn}')" \
    >/dev/null 2>&1 || true
  exit 0
fi

# 3) SessionEnd (or unknown): record observed_terminal + controller_close.
#    Unknown trigger is treated as a session close because anything that
#    reaches the SessionEnd hook is by definition the terminal boundary
#    for this session; we surface the trigger label in the payload so
#    audits can disambiguate.
EFFECTIVE_CWD="$EFFECTIVE_CWD" \
  SESSION_ID="$SESSION_ID" \
  HOOK_EVENT_NAME="$HOOK_EVENT_NAME" \
  LIB_DIR="$LIB_DIR" \
  python3 - <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ["LIB_DIR"])
from effectiveness_collection import (
    collect, observe, TRANSITION_OBSERVED_TERMINAL, TRANSITION_CONTROLLER_CLOSE,
)
root = os.environ["EFFECTIVE_CWD"]
sid = os.environ["SESSION_ID"]
hn = os.environ["HOOK_EVENT_NAME"]
try:
    observe(root, run_id=f"session:{sid}", workflow_id="session-lifecycle",
            subject_id=f"session:{sid}", attempt_id=f"sess-{sid}",
            transition=TRANSITION_OBSERVED_TERMINAL, outcome="completed",
            payload={"hook_event": hn})
    observe(root, run_id=f"session:{sid}", workflow_id="session-lifecycle",
            subject_id=f"session:{sid}", attempt_id=f"sess-{sid}",
            transition=TRANSITION_CONTROLLER_CLOSE, outcome="completed",
            payload={"hook_event": hn})
    collect(root)
except Exception:
    pass
PY

# Legacy: keep the existing step.completed emission so the
# `events.jsonl` trajectory reducer stays unchanged. Best-effort.
python3 -m lib.trace_log append-event \
  --root "$EFFECTIVE_CWD" --type step.completed \
  --run-id "session:${SESSION_ID}" --workflow-id "session-lifecycle" \
  --stage session --subject-id "session:${SESSION_ID}" \
  --outcome completed --source "hook:trace-session-end" \
  --evidence-json "$(jq -nc --arg sid "$SESSION_ID" --arg hn "$HOOK_EVENT_NAME" '{session_id:$sid, hook_event:$hn}')" \
  >/dev/null 2>&1 || true

exit 0
