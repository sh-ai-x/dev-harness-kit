#!/usr/bin/env bash
# context-window-guard.sh — UserPromptSubmit advisory hook.
#
# Watches the session transcript's recent input-token volume and emits a
# stderr WARN recommending /compact once the threshold is crossed. Per
# /dev-kit:token-analyzer (2026-08-11), HEAVY_CONTEXT fired on 253 sessions
# totalling $10,157 — the dominant cost signal. The standard recovery is
# /compact (cache-preserving) rather than /clear (cache-busting); see
# rules/session-hygiene.md#iron-laws.
#
# Sampling strategy (NOT a full file walk):
#   The prior implementation walked the entire session JSONL on every
#   prompt and timed out on long babysit sessions. We instead read only
#   the last 100 records via jq's array-slice (`.[-100:]`). Constant-time
#   in the worst case, well under 100ms — the hook's "timeout" key is
#   removed from hooks/hooks.json because the operation is provably fast.
#
#   Signal location: cumulative input tokens are monotonically non-
#   decreasing across a session, so the tail window is a sufficient
#   proxy for the cumulative total. If the tail is over threshold, the
#   cumulative total is too.
#
#   Thresholds (100K / 200K / 300K) are read from environment vars so
#   an operator can tune them per repo without editing the hook:
#     CONTEXT_WINDOW_WARN_KB=100   # default; first warn
#     CONTEXT_WINDOW_CAUTION_KB=200 # second warn
#     CONTEXT_WINDOW_HARD_KB=300    # final warn
#   Setting any to 0 disables that tier.
#
# Output: stderr WARN, exit 0 (advisory, non-blocking).
#
# Fail-open contract: missing jq / unreadable transcript → exit 0.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat)).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

TRANSCRIPT="$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)"
[ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ] && exit 0

# Sum `input_tokens + cache_read_input_tokens` across only the last 100
# records. `.[-100:]` returns up to 100 trailing records (or fewer if the
# transcript is shorter than 100). `select(.message.usage)` filters out
# records that lack a usage block; `add // 0` returns 0 on the empty
# array. The result is a constant-time tail sample — the prior full-file
# `jq -rs [...] | add` walked every record and timed out on long sessions.
TOKENS_RAW="$(
  jq -rs '
    [ .[-100:] | select(.message.usage) | .message.usage
      | ((.input_tokens // 0) + (.cache_read_input_tokens // 0)) ]
    | add // 0
  ' "$TRANSCRIPT" 2>/dev/null || echo 0
)"

# Sanitize: jq -r on a numeric yields a number string; fall back to 0
# on anything else (jq parse error → empty → 0).
if ! [[ "$TOKENS_RAW" =~ ^[0-9]+$ ]]; then
  TOKENS_RAW=0
fi
TOKENS_KB=$((TOKENS_RAW / 1000))

WARN_KB="${CONTEXT_WINDOW_WARN_KB:-100}"
CAUTION_KB="${CONTEXT_WINDOW_CAUTION_KB:-200}"
HARD_KB="${CONTEXT_WINDOW_HARD_KB:-300}"

TIER=""
MSG=""
if   [ "$HARD_KB" -gt 0 ] && [ "$TOKENS_KB" -ge "$HARD_KB" ]; then
  TIER="HARD"
  MSG="input token volume ≥ ${HARD_KB}K (tail sample: ${TOKENS_KB}K). Run /compact now — cache will reset on /clear."
elif [ "$CAUTION_KB" -gt 0 ] && [ "$TOKENS_KB" -ge "$CAUTION_KB" ]; then
  TIER="CAUTION"
  MSG="input token volume ≥ ${CAUTION_KB}K (tail sample: ${TOKENS_KB}K). /compact recommended."
elif [ "$WARN_KB" -gt 0 ] && [ "$TOKENS_KB" -ge "$WARN_KB" ]; then
  TIER="WARN"
  MSG="input token volume ≥ ${WARN_KB}K (tail sample: ${TOKENS_KB}K). Consider /compact at next break."
fi

[ -z "$TIER" ] && exit 0

cat >&2 <<MSG
[context-window-guard] ${TIER}: ${MSG}
  See hooks/context-window-guard.sh and rules/session-hygiene.md (Iron Law 4).
MSG

exit 0