#!/usr/bin/env bash
# notification-collapse.sh — UserPromptSubmit advisory hook.
#
# Detects verbose `<task-notification>` envelopes that the harness
# re-injects into the session context every time a background task
# (Monitor / run_in_background) emits. Each envelope is ~600 bytes of
# XML carrying the task-id, summary, output file path, and full
# stdout tail — none of which the assistant needs verbatim to decide
# what to do next.
#
# Why this hook exists:
#   Per /dev-kit:token-analyzer (2026-08-11), the REPEATED_USER_MSG
#   warning fired on 170 sessions totalling $8,568 of cost. The single
#   dominant trigger was the same `<task-notification>` summary
#   repeated 2–38 times in a single babysit-pr session.
#
# This hook CANNOT mutate the incoming prompt — UserPromptSubmit hooks
# can only inject additionalContext via hookSpecificOutput or emit
# advisory stderr. We choose the advisory path because:
#   1. The verbose envelope may still be useful for the model on the
#      first occurrence (to decide which task finished).
#   2. Subsequent occurrences within the same session are duplicates;
#      the WARN lets the model know it can reference the prior one.
#
# Output:
#   - stderr advisory only; the prompt passes through unchanged.
#   - Exit 0 always (non-blocking).
#
# Fail-open contract:
#   - missing jq / empty prompt / unparseable payload → exit 0 silently.
#   - detection runs once per prompt; the hook itself is cheap (<5 ms).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat)).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
[ -z "$PROMPT" ] && exit 0

# Count <task-notification> occurrences in the prompt. The harness
# emits one envelope per background-task fire, so duplicate summaries
# in the same prompt are the signature of the bloat pattern. We only
# warn when count >= 2 — the first occurrence is always useful.
# Use `grep -o ... | wc -l` rather than `grep -c` because the latter
# counts *lines*, and the harness occasionally concatenates multiple
# envelopes onto a single line in the bached-Monitor case (the very
# bloat pattern this rule exists to catch).
COUNT="$(printf '%s' "$PROMPT" | { grep -o '<task-notification>' || true; } | wc -l | tr -d '[:space:]')"
if [ "${COUNT:-0}" -lt 2 ]; then
  exit 0
fi

# Extract the first <summary>...</summary> as a reference anchor.
# The greedy `.*` (rather than `[^<]*`) keeps the body intact even
# when the summary itself contains a `<` (e.g. `<br>`, JSON
# fragments). Greedy `.*` matches up to the LAST `</summary>` on
# the matched line; in practice the harness emits one summary per
# line so this is correct. The `sed` then strips the literal tags
# (two simpler substitutions — easier to reason about than the BRE
# `\?` optional-slash).
SUMMARY="$(
  printf '%s' "$PROMPT" \
    | grep -m1 -oE '<summary>.*</summary>' \
    | sed 's|<summary>||g; s|</summary>||g' || true
)"
ANCHOR="${SUMMARY:-<task-notification>}"
# Clamp to 200 chars to bound terminal noise if a long summary slips
# through (per security review note 2026-08-11).
ANCHOR="${ANCHOR:0:200}"

cat >&2 <<MSG
[notification-collapse] ${COUNT} <task-notification> blocks in this prompt.
  First summary: ${ANCHOR}
  Subsequent envelopes are duplicates of the same background-task fire.
  Reference the first occurrence instead of re-reading each envelope.
  See hooks/notification-collapse.sh.
MSG

exit 0
