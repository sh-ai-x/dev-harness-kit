#!/usr/bin/env bash
# worktree-janitor.sh — non-interactive stale-worktree cleanup.
#
# Companion to /dev-kit:worktree-prune (interactive). Identifies worktrees
# older than JANITOR_AGE_DAYS (default 14) whose branch has no open PR and
# is not under babysit-pr retention, then renders a fixed-width table and
# (with -y or JANITOR_APPROVE=1) dispatches bin/worktree-remove-safe.sh per
# row (which archives logs/ before git worktree remove — issue #689 Phase 2).
#
# Usage: bin/worktree-janitor.sh [N] [--keep N] [--dry-run|-n] [-y|--yes]
#                                [--except-self] [--age-days N] [--help]
#
# Exit codes:
#   0 = success / nothing-to-do / dry-run printed
#   1 = runtime error
#   2 = invalid CLI
#   3 = partial success (some rows failed; see fail_count)
#
# Env vars:
#   JANITOR_AGE_DAYS         (default 14)
#   JANITOR_APPROVE=1        (skip y/N gate — same as -y)
#   WORKTREE_SESSION_PATH    (auto-excluded, like --except-self)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  echo "error: worktree-janitor.sh must run inside a git worktree" >&2
  exit 1
fi
SAFE_REMOVE="$SCRIPT_DIR/worktree-remove-safe.sh"

usage() {
  sed -n '2,/^set -euo pipefail/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//' | awk 'NF'
}

YES=0
DRY_RUN=0
KEEP=0
EXCEPT_SELF=0
AGE_DAYS="${JANITOR_AGE_DAYS:-14}"
POSITIONAL=()
EXCLUDE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)             YES=1 ;;
    -n|--dry-run)         DRY_RUN=1 ;;
    -k|--keep)            [ $# -ge 2 ] || { echo "error: --keep requires N" >&2; exit 2; }
                          KEEP=1; KEEP_N="$2"; shift ;;
    --age-days)           [ $# -ge 2 ] || { echo "error: --age-days requires N" >&2; exit 2; }
                          AGE_DAYS="$2"; shift ;;
    --except-self)        EXCEPT_SELF=1 ;;
    -h|--help)            usage; exit 0 ;;
    [0-9]*)               POSITIONAL+=("$1") ;;
    *)                    echo "error: unknown flag $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ "$KEEP" = "1" ] && [ "${#POSITIONAL[@]}" -gt 0 ]; then
  echo "error: --keep and positional N are mutually exclusive" >&2; exit 2
fi
if [ "${#POSITIONAL[@]}" -gt 1 ]; then
  echo "error: at most one positional N allowed" >&2; exit 2
fi
if [ "${#POSITIONAL[@]}" -eq 1 ]; then
  if ! [[ "${POSITIONAL[0]}" =~ ^[0-9]+$ ]]; then
    echo "error: positional must be a non-negative integer" >&2; exit 2
  fi
  KEEP=1; KEEP_N="${POSITIONAL[0]}"
fi

if [ "$EXCEPT_SELF" = "1" ] || [ -n "${WORKTREE_SESSION_PATH:-}" ]; then
  SELF_WT="${WORKTREE_SESSION_PATH:-$(git rev-parse --show-toplevel 2>/dev/null || echo)}"
  if [ -n "$SELF_WT" ]; then
    EXCLUDE_ARGS+=(--except-self "$SELF_WT")
  fi
fi

JSON_OUT="$(python3 -m lib.worktree_janitor \
  --repo "$REPO_ROOT" \
  --age-days "$AGE_DAYS" \
  "${EXCLUDE_ARGS[@]+"${EXCLUDE_ARGS[@]}"}" \
  --json)"

TOTAL="$(printf '%s' "$JSON_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])')"

if [ "$TOTAL" = "0" ]; then
  echo "worktree-janitor: 0 candidates (age>=${AGE_DAYS}d, no open PR, not babysit-retained)"
  exit 0
fi

# Render table.
printf '%-4s  %-6s  %-48s  %s\n' "#" "AGE(d)" "BRANCH" "PATH"
printf '%-4s  %-6s  %-48s  %s\n' "----" "------" "------------------------------------------------" "----------------------------------------"
printf '%s' "$JSON_OUT" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for i, r in enumerate(data["rows"], 1):
    print(f"{i:<4d}  {r[\"age_days\"]:6.1f}  {r[\"branch\"]:<48}  {r[\"path\"]}")
'

# Resolve target count from --keep or positional N.
TARGET="$TOTAL"
if [ "$KEEP" = "1" ]; then
  TARGET="$KEEP_N"
fi

# If --keep N or positional N selects fewer than total, build a slice.
if [ "$TARGET" -lt "$TOTAL" ]; then
  echo
  echo "will remove the ${TARGET} oldest (candidates=${TOTAL})"
  SELECTED="$(printf '%s' "$JSON_OUT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
n = ${TARGET}
for r in data['rows'][:n]:
    print(r['path'])
")"
else
  SELECTED="$(printf '%s' "$JSON_OUT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data['rows']:
    print(r['path'])
")"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "dry-run: ${TARGET} worktree(s) eligible. Re-run with -y to remove."
  exit 0
fi

if [ "$YES" != "1" ] && [ "${JANITOR_APPROVE:-0}" != "1" ]; then
  echo "warning: pass -y or set JANITOR_APPROVE=1 to remove. Dry-run only this invocation."
  exit 0
fi

# Remove each row through the safe-remove wrapper (preserves logs/).
fail_count=0
while IFS= read -r path; do
  [ -n "$path" ] || continue
  echo "removing $path"
  if ! "$SAFE_REMOVE" "$path" -- --force; then
    fail_count=$((fail_count + 1))
  fi
done <<< "$SELECTED"

if [ "$fail_count" -gt 0 ]; then
  echo "worktree-janitor: partial — ${fail_count} removal(s) failed" >&2
  exit 3
fi
exit 0
