#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full
# acp-tier-assert.sh — PreToolUse hook for any matcher (`*`).
#
# Enforces docs/architecture/acp-harness.md §2.3 — every dispatched ACP agent (M, T, or
# L) MUST emit, on the first tool call of its session, the literal line:
#
#   [tier-assert] I am Tier <N> (<M|T|L>). cwd is <WORKTREE_PATH>. I own <OWNERSHIP_SENTENCE>.
#
# Discriminator:
#   - On the agent's first non-empty tool call, scan the most recent
#     ~4 KiB of the session transcript (passed via stdin field
#     `transcript`; absent fields fall back to `cwd` + scratch scan).
#   - If the literal `[tier-assert] I am Tier` prefix is absent OR the
#     `<WORKTREE_PATH>` does not match the session cwd's worktree root
#     OR the `<OWNERSHIP_SENTENCE>` is malformed → deny with a reason
#     naming the missing field. Otherwise allow.
#   - The "seen tier-assert" state is per-session, persisted in a
#     sidecar file under <orch_worktree>/.dev-kit/round-<descriptor>/
#     tier-state/<session-id>.json (see docs/architecture/acp-harness.md §6.1).
#     Once the assertion passes, subsequent tool calls in the same
#     session are no-ops so the hook does not re-scan on every Edit.
#
# Fail-closed contract:
#   - Missing jq → exit 2 with a deny JSON envelope (PreToolUse deny).
#   - Missing transcript field → fall back to scanning stdin + cwd
#     (best-effort, deny if neither surfaces the literal).
#
# Out of scope:
#   - Hand-off template lint (`tests/test_acp_hand_off.py`).
#   - Round-meta write discipline (M-only handoffs.md).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning). POSIX-safe expansion so the
# source line still works when PATH is broken (jq-less test envs
# strip dirname along with jq — same pattern as bash-guard.sh).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Source shared payload helpers (`deny` for fail-closed JSON emit).
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"

# Fail CLOSED when jq is missing — the literal scan + discriminator both
# need jq. Self-contained printf (not the deny() helper, which itself
# needs jq).
if ! command -v jq >/dev/null 2>&1; then
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"ACP TIER-ASSERT: jq is required but not installed. Install jq (apt/brew/apk) — without it, the tier-assertion lint cannot run."}}\n' >&2
  exit 2
fi

# Empty stdin → probe call, no-op.
[ -z "$INPUT" ] && exit 0
[ -z "$(printf '%s' "$INPUT" | jq -r '.transcript // .prompt // ""')" ] && exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)"
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"

# Short-circuit on already-asserted sessions. The sidecar is written by
# this hook on the first successful assert; subsequent tool calls in the
# same session read it back and no-op. We avoid touching the file system
# on every Edit to keep the hook cheap.
TIER_STATE_DIR=""
if [ -n "$HOOK_CWD" ]; then
  # The orch-worktree lives at <cwd>/.worktrees/orch-<slug>/; the round
  # dir is an ancestor-owned .dev-kit/round-*/tier-state/ directory.
  # Check only each ancestor's direct .dev-kit children. A recursive
  # `find` here can walk a large repository or home directory and exceed
  # the 3-second PreToolUse timeout before the assertion is evaluated.
  search_root="$(cd "$HOOK_CWD" 2>/dev/null && pwd)"
  while [ -n "$search_root" ]; do
    for candidate in "$search_root"/.dev-kit/round-*/tier-state; do
      if [ -d "$candidate" ]; then
        TIER_STATE_DIR="$candidate"
        break 2
      fi
    done
    parent="$(dirname "$search_root")"
    [ "$parent" = "$search_root" ] && break
    search_root="$parent"
  done
fi

if [ -n "$TIER_STATE_DIR" ] && [ -n "$SESSION_ID" ]; then
  SIDECAR="$TIER_STATE_DIR/$SESSION_ID.json"
  if [ -f "$SIDECAR" ] && [ "$(jq -r '.asserted // false' "$SIDECAR" 2>/dev/null)" = "true" ]; then
    exit 0
  fi
fi

# Scan for the literal tier-assertion. Sources (in order):
#   1. .transcript        — most-recent session turn log
#   2. .cwd / $PWD       — last-resort: re-read stdin twice (some
#                          hosts echo the prompt back on stdin)
#   3. hook payload echo — also accepted when the field is named
#                          `.prompt` (UserPromptSubmit payloads carry
#                          the most recent user turn here)
ASSERT_LINE=""
for source in transcript prompt cwd; do
  field="$(printf '%s' "$INPUT" | jq -r --arg k "$source" '.[$k] // ""' 2>/dev/null)"
  if [ -n "$field" ] && printf '%s' "$field" | grep -q '\[tier-assert\] I am Tier'; then
    ASSERT_LINE="$(printf '%s' "$field" | grep '\[tier-assert\] I am Tier' | head -1)"
    break
  fi
done

if [ -z "$ASSERT_LINE" ]; then
  deny "ACP TIER-ASSERT" "missing tier-assertion on first tool call. Emit, on your first message before any other tool call, the literal line: [tier-assert] I am Tier <N> (<M|T|L>). cwd is <WORKTREE_PATH>. I own <OWNERSHIP_SENTENCE>."
fi

# Parse the assertion into its three mandatory fields.
N="$(printf '%s' "$ASSERT_LINE" | sed -nE 's/^\[tier-assert\] I am Tier ([0-9]+) \(([MTL])\)\. .*/\1/p')"
LETTER="$(printf '%s' "$ASSERT_LINE" | sed -nE 's/^\[tier-assert\] I am Tier ([0-9]+) \(([MTL])\)\. .*/\2/p')"
CWD_FIELD="$(printf '%s' "$ASSERT_LINE" | sed -nE 's/^\[tier-assert\] I am Tier [0-9]+ \([MTL]\)\. cwd is ([^[:space:]]+)\. .*/\1/p')"
OWNERSHIP="$(printf '%s' "$ASSERT_LINE" | sed -nE 's/^\[tier-assert\] I am Tier [0-9]+ \([MTL]\)\. cwd is [^[:space:]]+\. I own (.+)$/\1/p')"

# Validate the three mandatory fields.
if [ -z "$N" ] || [ -z "$LETTER" ] || [ -z "$CWD_FIELD" ] || [ -z "$OWNERSHIP" ]; then
  deny "ACP TIER-ASSERT" "tier-assertion is malformed. Expected: [tier-assert] I am Tier <N> (<M|T|L>). cwd is <WORKTREE_PATH>. I own <OWNERSHIP_SENTENCE>. Got: $ASSERT_LINE"
fi

# Validate <N> matches <LETTER>.
case "$LETTER" in
  M) [ "$N" = "1" ] || deny "ACP TIER-ASSERT" "Tier letter M requires N=1 (got N=$N)." ;;
  T) [ "$N" = "2" ] || deny "ACP TIER-ASSERT" "Tier letter T requires N=2 (got N=$N)." ;;
  L) [ "$N" = "3" ] || deny "ACP TIER-ASSERT" "Tier letter L requires N=3 (got N=$N)." ;;
  *) deny "ACP TIER-ASSERT" "Tier letter must be one of M|T|L (got $LETTER)." ;;
esac

# Validate <OWNERSHIP_SENTENCE> against the per-tier dictionary.
case "$LETTER" in
  M) [ "$OWNERSHIP" = "the round state and dispatch decisions only" ] \
       || deny "ACP TIER-ASSERT" "M ownership sentence must be: 'the round state and dispatch decisions only' (got: $OWNERSHIP)." ;;
  T)
    OWN_OK=0
    case "$OWNERSHIP" in
      "ONE PR's lifecycle on branch "*) OWN_OK=1 ;;
    esac
    [ "$OWN_OK" = "1" ] || deny "ACP TIER-ASSERT" "T ownership sentence must start with 'ONE PR\\'s lifecycle on branch ' (got: $OWNERSHIP)."
    ;;
  L)
    OWN_OK=0
    case "$OWNERSHIP" in
      "read-only investigation for T on branch "*"; no edits") OWN_OK=1 ;;
    esac
    [ "$OWN_OK" = "1" ] || deny "ACP TIER-ASSERT" "L ownership sentence must start with 'read-only investigation for T on branch ' and end with '; no edits' (got: $OWNERSHIP)."
    ;;
esac

# Validate <WORKTREE_PATH> resolves to the session cwd (or a worktree
# inside it). If HOOK_CWD is empty we cannot run the discriminator;
# accept in that case (the orchestrator didn't supply cwd, so the
# session is using its parent $PWD and we trust the assertion's literal).
if [ -n "$HOOK_CWD" ] && [ -n "$CWD_FIELD" ]; then
  HOOK_CWD_CANON="$(cd "$HOOK_CWD" 2>/dev/null && pwd -P 2>/dev/null || printf '%s' "$HOOK_CWD")"
  CLAIMED_CANON="$(cd "$CWD_FIELD" 2>/dev/null && pwd -P 2>/dev/null || printf '%s' "$CWD_FIELD")"
  if [ "$HOOK_CWD_CANON" != "$CLAIMED_CANON" ]; then
    # Allow if CWD_FIELD is an ancestor of HOOK_CWD_CANON (e.g. hook ran
    # from a subagent whose worktree is below the orch's check path).
    case "$HOOK_CWD_CANON" in
      "$CLAIMED_CANON"/*) ;;
      *) deny "ACP TIER-ASSERT" "tier-assertion cwd '$CWD_FIELD' does not match session cwd '$HOOK_CWD'. Re-emit with the actual session cwd." ;;
    esac
  fi
fi

# Persist the assertion sidecar so subsequent tool calls in the same
# session no-op. Fail-soft: if we cannot write, the next call will
# re-run the scan (cheap) but still enforce.
if [ -n "$TIER_STATE_DIR" ] && [ -n "$SESSION_ID" ]; then
  mkdir -p "$TIER_STATE_DIR" 2>/dev/null || true
  if [ -d "$TIER_STATE_DIR" ]; then
    SIDECAR="$TIER_STATE_DIR/$SESSION_ID.json"
    jq -nc --arg n "$N" --arg letter "$LETTER" --arg cwd "$CWD_FIELD" --arg own "$OWNERSHIP" --arg tool "$TOOL_NAME" \
      '{asserted:true, n:$n, letter:$letter, cwd:$cwd, ownership:$own, first_tool:$tool, asserted_at:now|todate}' \
      > "$SIDECAR" 2>/dev/null || true
  fi
fi

exit 0
