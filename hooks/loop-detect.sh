#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# loop-detect.sh — PostToolUse hook for Bash.
#
# Doom-loop detector (Pattern 1 from
# docs/proposals/playbook-application/02-reanalysis.yaml). After every
# Bash call, asks the sourced helper whether the last
# ${LOOP_DETECT_THRESHOLD:-3} entries in
# .dev-kit/hand-off/<session>.log are an identical tool + first-80-chars
# match. If yes, prints an advisory so the agent can break the cycle
# (vary input, switch tool, ask the user) before the next retry.
#
# Why this exists:
#   Doom loops silently burn ~5-10k tokens per session and never
#   recover on their own. A warn-on-third breaks the loop cheaply
#   without needing a process-level kill switch.
#
# Fail-open contract:
#   - Missing jq / payload / session id → exit 0 with no output
#     (doom-loop detection is advisory; we never block a tool that
#     already ran).
#   - Helper returns an unexpected rc != 0,1 → treat as no loop
#     (defensive: a future helper bug must NEVER fabricate a positive).
#
# Pairs with hooks/lib/loop-detect.sh::loop_detected.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat), etc.).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"
# Source the doom-loop helper.
# shellcheck source=lib/loop-detect.sh
source "${BASH_SOURCE[0]%/*}/lib/loop-detect.sh"

# Bail on empty probe payloads (harness sent nothing).
[ -z "$INPUT" ] && exit 0

# Loop detection is advisory; jq is required to parse the payload.
# Without jq we no-op silently (matches slop-detector / secret-scan
# pattern: stderr warning already emitted by the preamble).
command -v jq >/dev/null 2>&1 || exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)"
CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)"

# No tool name → nothing to fingerprint; no-op.
[ -n "$TOOL_NAME" ] || exit 0

# Hand the session id to the helper via env so the log path resolves
# inside the helper (mirrors how stage-gate threads CLAUDE_PLUGIN_ROOT).
export LOOP_DETECT_SESSION_ID="$SESSION_ID"

# Capture the helper's return code explicitly. The contract is:
#   rc=0 → no loop detected
#   rc=1 → doom loop detected
# Anything else is a helper-side error; treat as no-loop so a future
# helper bug cannot fabricate a false positive (see codex review).
LOOP_RC=0
loop_detected "$TOOL_NAME" "$CMD" || LOOP_RC=$?

if [ "$LOOP_RC" -ne 1 ]; then
  exit 0
fi

# Loop detected — surface the recovery hint to the agent (stderr is
# visible in the tool result tail, so this lands in the agent's next
# reasoning step). Threshold is reflected so the agent can tell whether
# to expect a stronger signal at 5+.
THRESHOLD="${LOOP_DETECT_THRESHOLD:-3}"
cat >&2 <<MSG
[loop-detect] DOOM LOOP DETECTED: ${THRESHOLD} consecutive identical Bash calls
  tool_name=${TOOL_NAME}
  input_prefix=$(printf '%s' "$CMD" | tr '\n' ' ' | cut -c1-80)
Break the cycle BEFORE the next retry: vary the tool input, switch tool
(e.g. Read instead of Bash), or ask the user. See hooks/lib/loop-detect.sh.
MSG
exit 0
