#!/usr/bin/env bash
# ralph-attended-lock.sh — PreToolUse hook. Mechanical AskUserQuestion
# refusal during /dev-kit:ralph ATTENDED_RUN.
#
# Wires the state-machine `attended_lock` invariant into the host's
# tool-use gate. Per skills/ralph/SKILL.md and lib/ralph_state.py the
# invariant is already enforced at the state-machine layer; this hook
# is the *mechanical* complement so even a misbehaving sub-skill or
# a model invocation that ignores the prose contract cannot call
# AskUserQuestion once SHIP_CONFIRM_GATE → ATTENDED_RUN has crossed.
#
# Matcher wired in hooks/hooks.json:
#   - PreToolUse AskUserQuestion → exits 2 with deny JSON when
#     `.dev-kit/ralph/<session>.json` reports attended_lock=True OR
#     current_stage=ATTENDED_RUN.
#
# Failure semantics
# -----------------
# - jq missing  → exit 0 (fail-OPEN — the rule is advisory without jq,
#   but the state-machine layer still enforces the invariant).
# - python3 missing → exit 0 (fail-OPEN — same rationale; we cannot
#   import ralph_state to read the canonical record).
# - No .dev-kit/ralph state → exit 0 (no active session, no rule).
# - Active session with lock or ATTENDED_RUN → exit 2 with deny JSON.
#
# Why fail-OPEN on toolchain-missing? The mechanical layer is a
# *belt-and-suspenders* complement to the state-machine layer. The
# state machine's `assert_can_ask()` already raises AttendedLockError
# if Ask is attempted during ATTENDED_RUN. Refusing to fail-open
# here would surface a false alarm in test environments without jq
# (see TestSlopDetectorRefactor.fails_closed for the prior pattern).
# The state machine remains the source of truth.

set -uo pipefail

INPUT="$(cat)"

# Probe / empty payload — let the host pass through.
[ -z "$INPUT" ] && exit 0

# Resolve project root — CLAUDE_PROJECT_DIR is preferred; fall back to
# git toplevel; fall back to cwd. None of these is fatal.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

# Toolchain preconditions (advisory, fail-open).
command -v jq >/dev/null 2>&1 || {
  echo "[ralph-attended-lock] WARN: jq missing; mechanical Ask-refusal disabled. State-machine layer still enforces the invariant." >&2
  exit 0
}
command -v python3 >/dev/null 2>&1 || {
  echo "[ralph-attended-lock] WARN: python3 missing; mechanical Ask-refusal disabled. State-machine layer still enforces the invariant." >&2
  exit 0
}

# Read the ralph state via the canonical state-machine module. This is
# the same path the orchestrator uses; we deliberately do NOT parse the
# JSON file directly because future schema changes would silently
# bypass this hook.
STATE_FILE="${PROJECT_ROOT}/.dev-kit/ralph/${RALPH_SESSION:-default}.json"
[ -f "$STATE_FILE" ] || exit 0

# Resolve the ralph_state module location. The hook sits under
# <repo>/hooks/ralph-attended-lock.sh; the lib is at
# <repo>/skills/ralph/lib/ralph_state.py. Derive the lib path from
# the hook's own location (BASH_SOURCE) so the hook works whether or
# not PROJECT_ROOT happens to be the dev-kit repo root.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_LIB="${HOOK_DIR%/hooks}/skills/ralph/lib"
if [ ! -f "${RALPH_LIB}/ralph_state.py" ]; then
  # Fallback: assume PROJECT_ROOT is the dev-kit repo root (canonical
  # install path).
  RALPH_LIB="${PROJECT_ROOT}/skills/ralph/lib"
fi
[ -f "${RALPH_LIB}/ralph_state.py" ] || {
  echo "[ralph-attended-lock] WARN: ralph_state.py not found at ${RALPH_LIB}; mechanical Ask-refusal disabled." >&2
  exit 0
}

# Import ralph_state via PYTHONPATH so we use the same module the
# orchestrator uses. Stderr-only diagnostics; the actual decision is
# the exit code below. NOTE: the heredoc delimiter is bare `PY` (NOT
# quoted) so $STATE_FILE and $PROJECT_ROOT are expanded by bash
# before Python sees the script body.
STATE_JSON=$(PYTHONPATH="${RALPH_LIB}" python3 - <<PY 2>/dev/null
import json, sys
from pathlib import Path
try:
    import ralph_state as rs  # type: ignore
    p = Path("$STATE_FILE")
    root = Path("$PROJECT_ROOT").resolve()
    state = rs.RalphState.load(root, p.stem)
    out = {
        "attended_lock": state.attended_lock,
        "current_stage": state.current_stage,
        "session": state.session,
    }
    print(json.dumps(out))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
PY
)

[ -z "$STATE_JSON" ] && exit 0

# Error from the loader → fail-open (state machine layer still enforces).
if echo "$STATE_JSON" | jq -e '.error' >/dev/null 2>&1; then
  exit 0
fi

# Check the two conditions. Either one trips the deny.
LOCK=$(echo "$STATE_JSON" | jq -r '.attended_lock // false')
STAGE=$(echo "$STATE_JSON" | jq -r '.current_stage // ""')

if [ "$LOCK" != "true" ] && [ "$STAGE" != "ATTENDED_RUN" ]; then
  exit 0
fi

# Deny with the canonical permissionDecision envelope. The reason text
# names the state-machine invariant so the LLM can recover (delete the
# AskUserQuestion call, take the auto-decision branch).
SESSION=$(echo "$STATE_JSON" | jq -r '.session // "default"')
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"RALPH ATTENDED_LOCK: AskUserQuestion is forbidden while ralph session=%s is at stage=%s with attended_lock=%s. Crossed the one-way SHIP_CONFIRM_GATE -> ATTENDED_RUN boundary; the chain auto-decides instead. Drop this AskUserQuestion call and continue the unattended chain (skills/ralph/lib/ralph_chain.py run_attended)."}}\n' \
  "$SESSION" "$STAGE" "$LOCK" >&2
exit 2
