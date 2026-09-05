#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full
# context-window-guard.sh — UserPromptSubmit advisory hook.
#
# Watches the session transcript for accumulated input-token volume and
# emits a stderr WARN recommending /compact once the threshold is
# crossed. Per /dev-kit:token-analyzer (2026-08-11), HEAVY_CONTEXT
# fired on 253 sessions totalling $10,157 — the dominant cost signal.
# The standard recovery is /compact (cache-preserving) rather than
# /clear (cache-busting); see rules/session-hygiene.md#iron-laws.
#
# Token counting strategy:
#   We do not have a wire-level token counter. As a proxy, we read the
#   session transcript_path from the hook payload (the harness exposes
#   the active JSONL file) and sum `message.usage.input_tokens` plus
#   `cache_read_input_tokens` across every record that carries a
#   usage block (Claude Code transcripts do; Codex transcripts do
#   not, so they sum to 0 and the hook stays silent on Codex runs —
#   see "Limitations" below for the rationale).
#
#   Metric semantics: this is **cumulative input tokens processed**
#   over the lifetime of the session, NOT the size of the prompt the
#   model currently sees. /dev-kit:token-analyzer uses the same
#   metric on its HEAVY_CONTEXT trigger, so the thresholds line up.
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
# Limitations:
#   - Codex transcripts lack `.message.usage`; the hook stays silent.
#     A byte-length/4 fallback was considered but rejected: the
#     false-positive risk (a transcript full of repeated boilerplate
#     would trip the warn even though the real context is small) is
#     higher than the value of catching the rare Codex case where
#     context genuinely bloats. Operators who want Codex coverage
#     can grep the transcript with their own script.
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

# Extract total input + cache_read tokens from the JSONL. Each record
# is one of: {message:{usage:{input_tokens,cache_read_input_tokens}}}.
# We sum them all and divide by 1000 to land in the K-tier (the
# thresholds are user-tunable in KB-of-tokens-equivalent).
TOKENS_RAW="$(
  jq -rs '
    [ .[] | select(.message.usage) | .message.usage
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
  MSG="input token volume ≥ ${HARD_KB}K (now ${TOKENS_KB}K). Run /compact now — cache will reset on /clear."
elif [ "$CAUTION_KB" -gt 0 ] && [ "$TOKENS_KB" -ge "$CAUTION_KB" ]; then
  TIER="CAUTION"
  MSG="input token volume ≥ ${CAUTION_KB}K (now ${TOKENS_KB}K). /compact recommended."
elif [ "$WARN_KB" -gt 0 ] && [ "$TOKENS_KB" -ge "$WARN_KB" ]; then
  TIER="WARN"
  MSG="input token volume ≥ ${WARN_KB}K (now ${TOKENS_KB}K). Consider /compact at next break."
fi

[ -z "$TIER" ] && exit 0

cat >&2 <<MSG
[context-window-guard] ${TIER}: ${MSG}
  See hooks/context-window-guard.sh and rules/session-hygiene.md (Iron Law 4).
MSG

exit 0
