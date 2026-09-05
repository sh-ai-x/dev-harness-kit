#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full
# worktree-janitor-session-start.sh — SessionStart hook (issue #717).
#
# Read-only nudge that surfaces orphan worktrees at session start. Counts
# worktrees whose branch is reachable from origin/main (merged) or whose
# branch matches `fix/classify-request-*` and is older than 7 days
# (orphans from the auto-classify pipeline) and prints a single line via
# `additionalContext` so the operator knows to consider
# `bin/worktree-prune.sh --dry-run`.
#
# NEVER deletes. The user-invoked `bin/worktree-prune.sh --apply` is the
# only mutation surface, and it explicitly requires --apply before doing
# anything.
#
# Opt-out: DEV_KIT_JANITOR_OFF=1 makes the hook a silent no-op (mirrors
# the opt-out pattern in `bin/review-local.sh` /
# `bin/babysit-pr-local.sh`).
#
# Fails open with a stderr warning when `jq` is missing — same posture
# as `session-start-check.sh` (advisory, not blocking).
#
# Only nudges when the session is starting in a worktree. Main-checkout
# sessions see the same probe but skipping the nudge avoids noise (the
# main session is rarely where prune decisions are made).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Opt-out gate (must run BEFORE any output to honor per-worktree skip).
if [ "${DEV_KIT_JANITOR_OFF:-0}" = "1" ]; then
  exit 0
fi

# Warn (not fail) if jq is missing. The preamble already emitted a
# `::warning::jq missing` marker; if jq was absent $WORKTREE_DETECT is
# "" and the case statement below treats it as silent / no-op.
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# extract_hook_cwd — read HOOK_CWD from stdin payload and cd into it.
HOOK_CWD="$(printf '%s' "${INPUT:-$(cat 2>/dev/null)}" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || true
fi

# Re-run worktree_detect after the cd so the discriminator reflects the
# EFFECTIVE cwd (the hook was launched from PROJECT_ROOT, but the
# session actually starts in HOOK_CWD). Without this re-run, the
# preamble's value still reflects PROJECT_ROOT's classification.
worktree_detect
EFFECTIVE_DETECT="$WORKTREE_DETECT"

# Only nudge in worktree sessions (skip main-checkout noise).
case "$EFFECTIVE_DETECT" in
  worktree) ;;
  *) exit 0 ;;
esac

# Probe orphan candidates via `git worktree list --porcelain`.
# We compute two overlapping sets so the operator sees a single number
# representing the action surface for `bin/worktree-prune.sh`:
#   - merged:  branch is reachable from origin/main
#   - stale:   branch matches fix/classify-request-* AND last commit
#              age > 7 days (auto-classify orphan pattern)
# Both predicates are intentionally conservative -- `bin/worktree-prune.sh
# --dry-run` is the audit step, this hook is just the "you should run
# that" signal.
ORPHAN_COUNT=0
RECORDS_SEEN=0
# Hard cap on records processed: a 1500-worktree inventory would fork
# `git merge-base` + `git log` 3000+ times per SessionStart. Stop after
# MAX_PROBE records and report the truncated count as "≥MAX_PROBE";
# the operator still sees the nudge surface, just with a floor on
# the displayed number rather than a precise count of every record.
MAX_PROBE="${DEV_KIT_JANITOR_MAX_PROBE:-500}"
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  STALE_DAYS=7
  # Iterate porcelain worktree records; each record is a multi-line
  # block separated by blank lines. Extract the worktree path + branch,
  # then apply the two predicates.
  RECORD=""
  while IFS= read -r line; do
    if [ -z "$line" ]; then
      # End of a record. Evaluate.
      # `git worktree list --porcelain` uses SPACE-separated fields
      # (the tag is the first token, value is everything after).
      WT_PATH="$(printf '%s\n' "$RECORD" | awk '$1=="worktree"{print $2}')"
      WT_BRANCH="$(printf '%s\n' "$RECORD" | awk '$1=="branch"{print $2}' | sed 's#^refs/heads/##')"
      if [ -n "$WT_PATH" ] && [ -n "$WT_BRANCH" ]; then
        # Predicate 1: merged into origin/main.
        if git merge-base --is-ancestor "$WT_BRANCH" origin/main 2>/dev/null; then
          ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
        else
          # Predicate 2: fix/classify-request-* older than STALE_DAYS.
          if [[ "$WT_BRANCH" == fix/classify-request-* ]]; then
            LAST_TS="$(git log -1 --pretty=%ct "$WT_BRANCH" 2>/dev/null || echo 0)"
            NOW="$(date +%s)"
            if [ "${LAST_TS:-0}" -gt 0 ] 2>/dev/null; then
              AGE_DAYS=$(( (NOW - LAST_TS) / 86400 ))
              if [ "${AGE_DAYS:-0}" -gt "${STALE_DAYS}" ]; then
                ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
              fi
            fi
          fi
        fi
      fi
      RECORD=""
      RECORDS_SEEN=$((RECORDS_SEEN + 1))
      # Short-circuit once we've seen enough records to bound the cost.
      # The nudge surfaces "≥MAX_PROBE" so operators still see the
      # signal that pruning is needed without forcing the hook to walk
      # the entire inventory.
      if [ "${RECORDS_SEEN:-0}" -ge "${MAX_PROBE}" ]; then
        ORPHAN_COUNT="${MAX_PROBE}"
        break
      fi
      continue
    fi
    RECORD="${RECORD}${line}"$'\n'
  done < <(git worktree list --porcelain)
fi

# Always exit 0 with optional additionalContext. SessionStart is a
# gentle nudge surface; a non-zero exit would block session startup.
if [ "${ORPHAN_COUNT:-0}" -gt 0 ] 2>/dev/null; then
  # When the cap fired, ORPHAN_COUNT is set to MAX_PROBE — render the
  # count as a floor so operators know it's a truncated read.
  if [ "${ORPHAN_COUNT}" = "${MAX_PROBE}" ] && [ "${RECORDS_SEEN:-0}" -ge "${MAX_PROBE}" ]; then
    DISPLAY="≥${ORPHAN_COUNT}"
  else
    DISPLAY="${ORPHAN_COUNT}"
  fi
  jq -nc --arg n "$DISPLAY" --arg cmd "bin/worktree-prune.sh --dry-run" \
    '{
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: ("[dev-kit janitor] " + $n + " orphan worktree(s) detected (merged into main, or stale classify-request orphans >7 days). Run `" + $cmd + "` to see candidates. Set DEV_KIT_JANITOR_OFF=1 to suppress this nudge.")
      }
    }'
else
  jq -nc '{hookSpecificOutput: {hookEventName: "SessionStart"}}'
fi
exit 0
