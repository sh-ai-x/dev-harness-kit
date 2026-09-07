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
# Auto-apply safety contract (refined after the Sep 2026 incident —
# the security review found the initial implementation could destroy
# active worktrees; this revision narrows scope):
#   - Predicate 2 ONLY (stale fix/classify-request-*). Predicate 1
#     (merged-into-main) is reported in the nudge but never auto-removed;
#     `--is-ancestor` is reflexive and would match fresh branches too.
#   - Skips the main checkout and the current worktree (no self-deletion).
#   - Requires `git -C "$WT_PATH" status --porcelain` to be empty (no
#     uncommitted or untracked files). Drops `--force` so git's own
#     dirty-tree refusal stays as the backstop.
#   - SAFE_REMOVE is resolved to the plugin's own bin/ directory (the
#     same anchor `${BASH_SOURCE[0]%/*}` used on line 35) and never
#     executes anything from CLAUDE_PROJECT_DIR or cwd.
#   - AUTO_PRUNE_MAX is validated as a non-negative integer; invalid input
#     falls back to 50 (clamped, not silent-zero).
#   - Per-attempt audit log appended to `.dev-kit/janitor-audit.log`
#     (path, branch, rc) so silent failures are observable postmortem.
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

# Resolve the safe-remove binary to the plugin's own bin/. Refusing to
# honor CLAUDE_PROJECT_DIR / cwd prevents an untrusted checkout that
# ships an executable `bin/worktree-remove-safe.sh` from being executed
# at SessionStart (silent arbitrary code execution surface).
SAFE_REMOVE="${BASH_SOURCE[0]%/*}/../bin/worktree-remove-safe.sh"
SAFE_REMOVE="$(cd "$(dirname "$SAFE_REMOVE")" 2>/dev/null && pwd)/$(basename "$SAFE_REMOVE")"
[ -x "$SAFE_REMOVE" ] || SAFE_REMOVE=""

# Probe orphan candidates via `git worktree list --porcelain`.
# We compute two overlapping sets so the operator sees a single number
# representing the action surface for `bin/worktree-prune.sh`:
#   - merged:  branch is reachable from origin/main (REPORTED ONLY)
#   - stale:   branch matches fix/classify-request-* AND last commit
#              age > 7 days (auto-classify orphan pattern; auto-applied
#              when the env-gated mode is on)
# Both predicates are intentionally conservative -- `bin/worktree-prune.sh
# --dry-run` is the audit step, this hook is just the "you should run
# that" signal. Auto-apply narrows to predicate 2 only.
ORPHAN_COUNT=0
STALE_COUNT=0          # predicate 2 only — auto-applicable
MERGED_COUNT=0         # predicate 1 only — reported, never auto-removed
DETECTED_AT_CAP=0      # 1 when MAX_PROBE fired so REMAINING can stay honest
REMOVED_COUNT=0
ARCHIVE_FAIL_COUNT=0
RECORDS_SEEN=0
# Hard cap on records processed: a 1500-worktree inventory would fork
# `git merge-base` + `git log` 3000+ times per SessionStart. Stop after
# MAX_PROBE records and report the truncated count as "≥MAX_PROBE";
# the operator still sees the nudge surface, just with a floor on
# the displayed number rather than a precise count of every record.
MAX_PROBE="${DEV_KIT_JANITOR_MAX_PROBE:-500}"
# Optional auto-apply (issue #792 follow-up). Default off; requires
# BOTH flags to be set so a single misspelled env var cannot trigger
# removals. The cap (default 50) bounds the cost per session start so a
# long-running repo cannot have its first prompt blocked on a 4k-
# worktree safe-remove sweep.
AUTO_PRUNE=0
if [ "${DEV_KIT_JANITOR_AUTO_PRUNE:-0}" = "1" ] \
   && [ "${DEV_KIT_JANITOR_AUTO_PRUNE_YES:-0}" = "1" ]; then
  AUTO_PRUNE=1
fi
# Validate AUTO_PRUNE_MAX: non-negative integer only. Anything else
# falls back to 50 instead of producing a `[ -lt abc ]` syntax error
# per record (which would also leak to stderr on each call). Use a
# separate expansion step so the bare `${VAR}` in `[[ ... =~ ... ]]`
# doesn't trigger set -u when the env var is unset (the preamble sets
# `-uo pipefail`; referencing an unset var inside `[[ =~ ]]` blows up).
AUTO_PRUNE_MAX_RAW="${DEV_KIT_JANITOR_AUTO_PRUNE_MAX-50}"
if ! [[ "${AUTO_PRUNE_MAX_RAW}" =~ ^[0-9]+$ ]]; then
  AUTO_PRUNE_MAX=50
else
  AUTO_PRUNE_MAX="${AUTO_PRUNE_MAX_RAW}"
fi
# Audit log: appended on every attempt (success and failure alike) so
# silent misfires have a postmortem trail. Path is the worktree's
# own .dev-kit/ (same convention as .dev-kit/babysit.log).
AUDIT_LOG="${BASH_SOURCE[0]%/*}/../.dev-kit/janitor-audit.log"
AUDIT_DIR="$(dirname "$AUDIT_LOG")"
mkdir -p "$AUDIT_DIR" 2>/dev/null || true

# Paths to never auto-remove: the main checkout and the session's own
# worktree. Both would either be git-protected (main) or self-destructive
# (the session's cwd). Resolved to absolute paths for byte-exact match.
CURRENT_WT_PATH="$(cd "${HOOK_CWD:-.}" 2>/dev/null && pwd -P 2>/dev/null || echo "")"
MAIN_WT_PATH="$(git -C "${HOOK_CWD:-.}" rev-parse --show-toplevel 2>/dev/null)"
# Identify the main checkout: it's the worktree whose branch is main
# (or detached, depending on consumer setup). Resolve via porcelain so
# the path is byte-exact.
MAIN_WT_PATH=""
while IFS= read -r mt_line; do
  case "$mt_line" in
    "worktree "*) MAIN_WT_PATH="${mt_line#worktree }" ;;
    "branch refs/heads/main"|"branch refs/heads/master")
      # first occurrence — main is conventionally the first porcelain record.
      break
      ;;
  esac
done < <(git -C "${HOOK_CWD:-.}" worktree list --porcelain 2>/dev/null)

if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  STALE_DAYS=7
  # Iterate porcelain worktree records; each record is a multi-line
  # block separated by blank lines. Extract the worktree path + branch,
  # then apply the two predicates. Auto-apply (when enabled) dispatches
  # `bin/worktree-remove-safe.sh` against predicate-2 candidates only,
  # skips the main checkout and the session's own worktree, requires
  # a clean git status, and never passes `--force` (git's dirty-tree
  # refusal stays as the backstop).
  RECORD=""
  while IFS= read -r line; do
    if [ -z "$line" ]; then
      # End of a record. Evaluate.
      # `git worktree list --porcelain` uses SPACE-separated fields
      # (the tag is the first token, value is everything after the
      # first space — `awk $2` would truncate paths containing spaces,
      # so we strip the leading tag and keep the rest of the line).
      WT_PATH="$(printf '%s\n' "$RECORD" | sed -n 's/^worktree //p' | head -1)"
      WT_BRANCH="$(printf '%s\n' "$RECORD" | awk '$1=="branch"{print $2}' | sed 's#^refs/heads/##')"
      ORPHAN_THIS=0
      STALE_THIS=0
      if [ -n "$WT_PATH" ] && [ -n "$WT_BRANCH" ]; then
        # Predicate 1: merged into origin/main.
        if git merge-base --is-ancestor "$WT_BRANCH" origin/main 2>/dev/null; then
          ORPHAN_THIS=1
          MERGED_COUNT=$((MERGED_COUNT + 1))
        else
          # Predicate 2: fix/classify-request-* older than STALE_DAYS.
          if [[ "$WT_BRANCH" == fix/classify-request-* ]]; then
            LAST_TS="$(git log -1 --pretty=%ct "$WT_BRANCH" 2>/dev/null || echo 0)"
            NOW="$(date +%s)"
            if [ "${LAST_TS:-0}" -gt 0 ] 2>/dev/null; then
              AGE_DAYS=$(( (NOW - LAST_TS) / 86400 ))
              if [ "${AGE_DAYS:-0}" -gt "${STALE_DAYS}" ]; then
                ORPHAN_THIS=1
                STALE_THIS=1
                STALE_COUNT=$((STALE_COUNT + 1))
              fi
            fi
          fi
        fi
      fi
      if [ "$ORPHAN_THIS" = "1" ]; then
        ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
        # Auto-apply path: predicate 2 only, with safety guards.
        # Predicate 1 (merged-into-main) is reported but NEVER auto-
        # removed because `--is-ancestor` is reflexive and a fresh
        # branch off main would match.
        if [ "$AUTO_PRUNE" = "1" ] \
           && [ "$STALE_THIS" = "1" ] \
           && [ "${REMOVED_COUNT:-0}" -lt "${AUTO_PRUNE_MAX}" ] \
           && [ -n "$WT_PATH" ] && [ -d "$WT_PATH" ] \
           && [ -n "$SAFE_REMOVE" ] && [ -x "$SAFE_REMOVE" ] \
           && [ "$WT_PATH" != "$CURRENT_WT_PATH" ] \
           && [ "$WT_PATH" != "$MAIN_WT_PATH" ] \
           && [ -z "$(git -C "$WT_PATH" status --porcelain 2>/dev/null)" ]; then
          # Drop `--force` so git's dirty-tree refusal is the backstop.
          if "$SAFE_REMOVE" "$WT_PATH" -- >/dev/null 2>"$AUDIT_DIR/.last-rm-stderr"; then
            REMOVED_COUNT=$((REMOVED_COUNT + 1))
            ARCHIVE_STATUS="$(printf '%s' "$(cat "$AUDIT_DIR/.last-rm-stderr" 2>/dev/null)" | python3 -c "
import json, sys
try:
    r = json.loads(sys.stdin.read())
except json.JSONDecodeError:
    sys.exit(0)
sys.exit(1 if r.get('status') == 'error' else 0)
" 2>/dev/null || echo 0)"
            [ "${ARCHIVE_STATUS:-0}" = "1" ] && ARCHIVE_FAIL_COUNT=$((ARCHIVE_FAIL_COUNT + 1))
            printf '%s path=%s branch=%s rc=0\n' "$(date -Iseconds)" "$WT_PATH" "$WT_BRANCH" >> "$AUDIT_LOG" 2>/dev/null || true
          else
            rc=$?
            printf '%s path=%s branch=%s rc=%d\n' "$(date -Iseconds)" "$WT_PATH" "$WT_BRANCH" "$rc" >> "$AUDIT_LOG" 2>/dev/null || true
          fi
          rm -f "$AUDIT_DIR/.last-rm-stderr" 2>/dev/null || true
        fi
      fi
      RECORD=""
      RECORDS_SEEN=$((RECORDS_SEEN + 1))
      # Short-circuit once we've seen enough records to bound the cost.
      # The nudge surfaces "≥MAX_PROBE" so operators still see the
      # signal that pruning is needed without forcing the hook to walk
      # the entire inventory. DETECTED_AT_CAP is set so the REMAINING
      # math stays honest (subtracting from a synthetic MAX_PROBE would
      # under-report residual).
      if [ "${RECORDS_SEEN:-0}" -ge "${MAX_PROBE}" ]; then
        DETECTED_AT_CAP=1
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
  # When the cap fired, the real total is "≥MAX_PROBE" — never let
  # REMAINING subtract from a synthetic sentinel.
  if [ "${DETECTED_AT_CAP}" = "1" ]; then
    DISPLAY="≥${MAX_PROBE}"
  else
    DISPLAY="${ORPHAN_COUNT}"
  fi
  if [ "$AUTO_PRUNE" = "1" ] && [ "${REMOVED_COUNT:-0}" -gt 0 ]; then
    if [ "${DETECTED_AT_CAP}" = "1" ]; then
      REMAINING="≥$(( MAX_PROBE - REMOVED_COUNT ))"
    else
      REMAINING=$(( ORPHAN_COUNT - REMOVED_COUNT ))
      [ "$REMAINING" -lt 0 ] && REMAINING=0
    fi
    ARCHIVE_NOTE=""
    if [ "${ARCHIVE_FAIL_COUNT:-0}" -gt 0 ]; then
      ARCHIVE_NOTE=" (${ARCHIVE_FAIL_COUNT} archive-failures; see .dev-kit/janitor-audit.log)"
    fi
    jq -nc --arg detected "$DISPLAY" --arg removed "${REMOVED_COUNT}" \
          --arg remaining "${REMAINING}" --arg max "${AUTO_PRUNE_MAX}" \
          --arg note "${ARCHIVE_NOTE}" \
      '{
        hookSpecificOutput: {
          hookEventName: "SessionStart",
          additionalContext: ("[dev-kit janitor] auto-pruned " + $removed + " of " + $detected + " stale fix/classify-request-* worktree(s) (cap=" + $max + "/session); " + $remaining + " remain" + $note + ". Set DEV_KIT_JANITOR_AUTO_PRUNE=0 to disable, raise DEV_KIT_JANITOR_AUTO_PRUNE_MAX to remove more per session.")
        }
      }'
  else
    jq -nc --arg n "$DISPLAY" --arg cmd "bin/worktree-prune.sh --dry-run" \
          --arg merged "${MERGED_COUNT}" \
      '{
        hookSpecificOutput: {
          hookEventName: "SessionStart",
          additionalContext: ("[dev-kit janitor] " + $n + " orphan worktree(s) detected (" + $merged + " merged into main; rest stale classify-request orphans >7 days). Run `" + $cmd + "` to see candidates. Auto-prune applies only to the stale-classify subset (never to merged-into-main). To enable: export DEV_KIT_JANITOR_AUTO_PRUNE=1 DEV_KIT_JANITOR_AUTO_PRUNE_YES=1. Set DEV_KIT_JANITOR_OFF=1 to suppress this nudge.")
        }
      }'
  fi
else
  jq -nc '{hookSpecificOutput: {hookEventName: "SessionStart"}}'
fi
exit 0
