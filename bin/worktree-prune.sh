#!/usr/bin/env bash
# Count worktrees, list them oldest-first, and remove the N oldest on demand.
#
# Companion command: /dev-kit:worktree-prune. The slash command is a thin
# wrapper that forwards here; this script owns the actual logic.
#
# Why bash+Python: the age-sort + select-N + remove-N loop has two halves
# — deterministic data work (parse porcelain, build epoch map, sort) and
# user-facing shell work (prompt, confirm, dispatch safe-remove). The
# data half lives in lib/worktree_prune.py (one for-each-ref call, single
# pass over the porcelain blocks) and runs in ~2s for 1.5k worktrees; a
# pure-bash version took 90s because every row paid a subshell cost.
# This script is the shell half.
#
# What it does, in order:
#   1. `python3 -m lib.worktree_prune --repo <root> --table` — emits the
#      age-sorted candidate list (excludes main checkout + detached)
#      with a `Worktrees registered: N` summary line.
#   2. `python3 -m lib.worktree_prune --repo <root> --count` — the same
#      N as a standalone int. Used for the interactive ask.
#   3. Read N from stdin (interactive) or accept it positionally.
#   4. Print the would-be-removed rows + final y/N gate.
#   5. For each selected row, call `bin/worktree-remove-safe.sh <path>`
#      so the per-worktree log archive (issue #689 Phase 2) runs first.
#
# Flags:
#   -y, --yes       Skip the final y/N gate (CI / batch mode).
#   -n, --dry-run   Print what would be removed; never mutate.
#   -k, --keep N    Keep at least N newest worktrees (selects from the
#                   oldest side). Mutually exclusive with positional N.
#   -h, --help      Show usage and exit 0.
#
# Exit codes:
#   0   Removed (or nothing selected, or dry-run completed).
#   1   Invalid CLI / runtime error.
#   2   User aborted at the y/N gate.
#   3   At least one removal failed (partial success).
#
# Examples:
#   bin/worktree-prune.sh                 # interactive — ask how many
#   bin/worktree-prune.sh 5               # remove the 5 oldest
#   bin/worktree-prune.sh -y 10           # remove 10 oldest, no prompt
#   bin/worktree-prune.sh --keep 3        # prune to 3 newest
#   bin/worktree-prune.sh -n 5            # show the 5 oldest; don't remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
SAFE_REMOVE="$SCRIPT_DIR/worktree-remove-safe.sh"

# --- arg parsing ---------------------------------------------------------

YES=0
DRY_RUN=0
KEEP=0
POSITIONAL=()

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" \
    | sed -e '$d' -e 's/^# \{0,1\}//' \
    | awk 'NF'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)     YES=1; shift ;;
    -n|--dry-run) DRY_RUN=1; shift ;;
    -k|--keep)    KEEP="${2:-}"; shift 2 || { echo "error: --keep needs an integer" >&2; exit 1; } ;;
    -h|--help)    usage ;;
    --)           shift; POSITIONAL+=("$@"); break ;;
    -*)           echo "error: unknown flag $1 (try --help)" >&2; exit 1 ;;
    *)            POSITIONAL+=("$1"); shift ;;
  esac
done

if [[ "$KEEP" -gt 0 && ${#POSITIONAL[@]} -gt 0 ]]; then
  echo "error: --keep and a positional count are mutually exclusive" >&2
  exit 1
fi

SELECT_COUNT=0
if [[ "$KEEP" -gt 0 ]]; then
  :  # resolved below once total is known
elif [[ ${#POSITIONAL[@]} -gt 0 ]]; then
  SELECT_COUNT="${POSITIONAL[0]}"
  if ! [[ "$SELECT_COUNT" =~ ^[0-9]+$ ]]; then
    echo "error: count must be a non-negative integer, got '$SELECT_COUNT'" >&2
    exit 1
  fi
fi

# --- candidate list ------------------------------------------------------

# Single Python invocation. Each mode of `lib.worktree_prune` triggers a
# fresh `git worktree list --porcelain` which costs ~10s on a 1.5k-tree
# repo; we only want to pay that once per CLI run. We get JSON (the
# richest form) and synthesize the count + table from it in shell —
# bash is free, the git call is the bottleneck.
JSON_OUT="$(python3 -m lib.worktree_prune --repo "$REPO_ROOT")"
TOTAL="$(printf '%s' "$JSON_OUT" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")"

if [[ -z "$TOTAL" || "$TOTAL" -eq 0 ]]; then
  echo "No removable worktrees (only the main checkout is registered)."
  exit 0
fi

# Render the table from the same JSON so the "all candidates" view and
# the "selected" preview use identical formatting.
TABLE_OUT="$(printf '%s' "$JSON_OUT" | python3 -c "
import json, sys, time
rows = json.load(sys.stdin)
now = int(time.time())
def trunc(s, m): return s if len(s) <= m else s[:m-3] + '...'
print(f'Worktrees registered: {len(rows)} (excluding main checkout)')
print()
print(f\"{'#':>4}  {'AGE(d)':>6}  {'BRANCH':<30}  PATH\")
print(f\"{'----':>4}  {'------':>6}  {'-' * 30}  {'-' * 4}\")
for i, r in enumerate(rows, 1):
    age = max(0, (now - r['epoch']) // 86400) if r['epoch'] else 0
    print(f\"{i:>4}  {age:>6}  {trunc(r['branch'], 30):<30}  {r['path']}\")
")"

# Print the table.
printf '%s\n' "$TABLE_OUT"
echo

# Resolve --keep into a count.
if [[ "$KEEP" -gt 0 ]]; then
  if [[ "$KEEP" -ge "$TOTAL" ]]; then
    echo "--keep $KEEP ≥ $TOTAL removable worktrees; nothing to prune."
    exit 0
  fi
  SELECT_COUNT=$((TOTAL - KEEP))
  echo "Selection: --keep $KEEP → removing the $SELECT_COUNT oldest."
elif [[ ${#POSITIONAL[@]} -eq 0 ]]; then
  if [[ -t 0 && "$YES" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    read -r -p "How many oldest worktrees to remove? (0-${TOTAL}, q to quit) " answer
    case "${answer,,}" in
      q|quit|"") SELECT_COUNT=0 ;;
      *)        SELECT_COUNT="$answer" ;;
    esac
    if ! [[ "$SELECT_COUNT" =~ ^[0-9]+$ ]]; then
      echo "error: count must be a non-negative integer, got '$SELECT_COUNT'" >&2
      exit 1
    fi
  fi
fi

if [[ "$SELECT_COUNT" -eq 0 ]]; then
  echo "Nothing selected — exiting."
  exit 0
fi

if [[ "$SELECT_COUNT" -gt "$TOTAL" ]]; then
  echo "error: requested $SELECT_COUNT, only $TOTAL removable worktrees available" >&2
  exit 1
fi

# --- selected list + confirm --------------------------------------------

# Render the first N rows in the same fixed-width format as the table so
# the "Will remove" preview matches the audit table the user just saw.
SELECTED_TABLE="$(printf '%s' "$JSON_OUT" | python3 -c "
import json, sys, time
rows = json.load(sys.stdin)
n = ${SELECT_COUNT}
now = int(time.time())
def trunc(s, m): return s if len(s) <= m else s[:m-3] + '...'
print(f\"{'#':>4}  {'AGE(d)':>6}  {'BRANCH':<30}  PATH\")
print(f\"{'----':>4}  {'------':>6}  {'-' * 30}  {'-' * 4}\")
for i, r in enumerate(rows[:n], 1):
    age = max(0, (now - r['epoch']) // 86400) if r['epoch'] else 0
    print(f\"{i:>4}  {age:>6}  {trunc(r['branch'], 30):<30}  {r['path']}\")
")"

NOW="$(date +%s)"

echo
echo "Will remove the following $SELECT_COUNT oldest worktree(s):"
echo
printf '%s\n' "$SELECTED_TABLE" | tail -n +3 | while IFS= read -r line; do
  printf '  %s\n' "$line"
done
echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "(dry-run: no changes made)"
  exit 0
fi

if [[ "$YES" -eq 0 && -t 0 ]]; then
  read -r -p "Proceed? [y/N] " confirm
  case "${confirm,,}" in
    y|yes) ;;
    *)     echo "Aborted."; exit 2 ;;
  esac
fi

# Materialize the slice into arrays so the removal loop runs in the
# current shell (avoids the subshell-boundary that would swallow the
# fail counter). Pull paths out of the cached JSON.
mapfile -t SELECTED < <(printf '%s' "$JSON_OUT" | python3 -c "
import json, sys
rows = json.load(sys.stdin)
for r in rows[:${SELECT_COUNT}]:
    print(r['path'])
")

fail_count=0
for path in "${SELECTED[@]}"; do
  echo "→ removing: $path"
  if ! "$SAFE_REMOVE" "$path"; then
    echo "  ! removal failed: $path" >&2
    fail_count=$((fail_count + 1))
  fi
done

if [[ "$fail_count" -gt 0 ]]; then
  echo
  echo "$fail_count removal(s) failed. Remaining worktrees:"
  git -C "$REPO_ROOT" worktree list
  exit 3
fi

echo
echo "Done. $SELECT_COUNT worktree(s) removed."
git -C "$REPO_ROOT" worktree list | wc -l | awk '{print "Remaining worktree count: " $1}'
