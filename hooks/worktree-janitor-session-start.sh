#!/usr/bin/env bash
# worktree-janitor-session-start.sh — SessionStart hook (issue #717).
#
# Surfaces orphan worktrees at session start. Counts worktrees whose
# branch is reachable from origin/main (merged) or whose branch matches
# `fix/classify-request-*` and is older than 7 days (orphans from the
# auto-classify pipeline) and prints a single line via
# `additionalContext` so the operator knows to consider
# `bin/worktree-prune.sh --dry-run`.
#
# Optional auto-apply mode (added when the 895-fix-classify accumulation
# went unpruned despite the existing nudge): when BOTH
# `DEV_KIT_JANITOR_AUTO_PRUNE=1` AND `DEV_KIT_JANITOR_AUTO_PRUNE_YES=1`
# are exported, the hook dispatches `bin/worktree-remove-safe.sh` for
# up to `DEV_KIT_JANITOR_AUTO_PRUNE_MAX` (default 50) candidate worktrees
# per session start. The double-gate prevents a single typo from
# triggering mass removals; the cap bounds the per-session cost so a
# 4k-worktree sweep cannot stall the first prompt.
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
REMOVED_COUNT=0
RECORDS_SEEN=0
# Hard cap on records processed: a 1500-worktree inventory would fork
# `git merge-base` + `git log` 3000+ times per SessionStart. Stop after
# MAX_PROBE records and report the truncated count as "≥MAX_PROBE";
# the operator still sees the nudge surface, just with a floor on
# the displayed number rather than a precise count of every record.
MAX_PROBE="${DEV_KIT_JANITOR_MAX_PROBE:-500}"
# Optional auto-apply (issue #792 follow-up). Default off; requires
# BOTH flags to be set so a single misspelled env var cannot trigger
# removals. The cap (default 50) bounds the cost per session start so
# a long-running repo cannot have its first prompt blocked on a 4k-
# worktree safe-remove sweep.
AUTO_PRUNE=0
if [ "${DEV_KIT_JANITOR_AUTO_PRUNE:-0}" = "1" ] \
   && [ "${DEV_KIT_JANITOR_AUTO_PRUNE_YES:-0}" = "1" ]; then
  AUTO_PRUNE=1
fi
AUTO_PRUNE_MAX="${DEV_KIT_JANITOR_AUTO_PRUNE_MAX:-50}"
# Resolve the safe-remove binary once; same path the worktree-prune
# skill dispatches through (preserves the per-worktree log archive).
SAFE_REMOVE="${CLAUDE_PROJECT_DIR:-.}/bin/worktree-remove-safe.sh"
if [ ! -x "$SAFE_REMOVE" ]; then
  SAFE_REMOVE="$(git rev-parse --show-toplevel 2>/dev/null)/bin/worktree-remove-safe.sh"
fi

if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  STALE_DAYS=7
  # Iterate porcelain worktree records; each record is a multi-line
  # block separated by blank lines. Extract the worktree path + branch,
  # then apply the two predicates. Auto-apply (when enabled) dispatches
  # `bin/worktree-remove-safe.sh` against the same candidates — never
  # outside this predicate set, so legit in-progress worktrees stay.
  RECORD=""
  while IFS= read -r line; do
    if [ -z "$line" ]; then
      # End of a record. Evaluate.
      # `git worktree list --porcelain` uses SPACE-separated fields
      # (the tag is the first token, value is everything after).
      WT_PATH="$(printf '%s\n' "$RECORD" | awk '$1=="worktree"{print $2}')"
      WT_BRANCH="$(printf '%s\n' "$RECORD" | awk '$1=="branch"{print $2}' | sed 's#^refs/heads/##')"
      ORPHAN_THIS=0
      if [ -n "$WT_PATH" ] && [ -n "$WT_BRANCH" ]; then
        # Predicate 1: merged into origin/main.
        if git merge-base --is-ancestor "$WT_BRANCH" origin/main 2>/dev/null; then
          ORPHAN_THIS=1
        else
          # Predicate 2: fix/classify-request-* older than STALE_DAYS.
          if [[ "$WT_BRANCH" == fix/classify-request-* ]]; then
            LAST_TS="$(git log -1 --pretty=%ct "$WT_BRANCH" 2>/dev/null || echo 0)"
            NOW="$(date +%s)"
            if [ "${LAST_TS:-0}" -gt 0 ] 2>/dev/null; then
              AGE_DAYS=$(( (NOW - LAST_TS) / 86400 ))
              if [ "${AGE_DAYS:-0}" -gt "${STALE_DAYS}" ]; then
                ORPHAN_THIS=1
              fi
            fi
          fi
        fi
      fi
      if [ "$ORPHAN_THIS" = "1" ]; then
        ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
        # Auto-apply path: dispatch safe-remove, respect the cap, never
        # block the session start on a hung git. rc is intentionally
        # swallowed (best-effort); the nudge still surfaces the
        # remaining count via ORPHAN_COUNT - REMOVED_COUNT.
        if [ "$AUTO_PRUNE" = "1" ] \
           && [ "${REMOVED_COUNT:-0}" -lt "${AUTO_PRUNE_MAX}" ] \
           && [ -n "$WT_PATH" ] && [ -d "$WT_PATH" ] \
           && [ -x "$SAFE_REMOVE" ]; then
          if "$SAFE_REMOVE" "$WT_PATH" -- --force >/dev/null 2>&1; then
            REMOVED_COUNT=$((REMOVED_COUNT + 1))
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
  if [ "$AUTO_PRUNE" = "1" ] && [ "${REMOVED_COUNT:-0}" -gt 0 ]; then
    REMAINING=$(( ORPHAN_COUNT - REMOVED_COUNT ))
    [ "$REMAINING" -lt 0 ] && REMAINING=0
    jq -nc --arg detected "$DISPLAY" --arg removed "${REMOVED_COUNT}" \
          --arg remaining "${REMAINING}" --arg max "${AUTO_PRUNE_MAX}" \
      '{
        hookSpecificOutput: {
          hookEventName: "SessionStart",
          additionalContext: ("[dev-kit janitor] auto-pruned " + $removed + " of " + $detected + " orphan worktree(s) (cap=" + $max + "/session); " + $remaining + " remain. Set DEV_KIT_JANITOR_AUTO_PRUNE=0 to disable, raise DEV_KIT_JANITOR_AUTO_PRUNE_MAX to remove more per session.")
        }
      }'
  else
    jq -nc --arg n "$DISPLAY" --arg cmd "bin/worktree-prune.sh --dry-run" \
      '{
        hookSpecificOutput: {
          hookEventName: "SessionStart",
          additionalContext: ("[dev-kit janitor] " + $n + " orphan worktree(s) detected (merged into main, or stale classify-request orphans >7 days). Run `" + $cmd + "` to see candidates. To auto-prune next session: export DEV_KIT_JANITOR_AUTO_PRUNE=1 DEV_KIT_JANITOR_AUTO_PRUNE_YES=1. Set DEV_KIT_JANITOR_OFF=1 to suppress this nudge.")
        }
      }'
  fi
else
  jq -nc '{hookSpecificOutput: {hookEventName: "SessionStart"}}'
fi
exit 0