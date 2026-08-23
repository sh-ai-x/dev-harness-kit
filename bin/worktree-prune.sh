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
#   1. `python3 -m lib.worktree_prune --repo <root>` — emits the JSON
#      candidate list (excludes main checkout + detached).
#   2. `python3 -m lib.worktree_prune --repo <root> --table` — renders
#      the audit table (one porcelain + one for-each-ref under the hood;
#      this script only pays the git-call cost once via step 1's JSON).
#   3. Read N from stdin (interactive) or accept it positionally.
#   4. `python3 -m lib.worktree_prune --repo <root> --table --head N`
#      renders the would-be-removed slice in the same format as step 2
#      (review finding #1: previous two Python heredocs duplicated the
#      f-string layout; now both views call the module's renderer).
#   5. Final y/N gate, then per-row
#      `bin/worktree-remove-safe.sh <path>` so the per-worktree log
#      archive (issue #689 Phase 2) runs first.
#
# Flags:
#   -y, --yes       Skip the final y/N gate (CI / batch mode).
#   -n, --dry-run   Print what would be removed; never mutate.
#   -k, --keep N    Keep exactly N newest worktrees (selects the
#                   TOTAL-N oldest for removal). Mutually exclusive with
#                   positional N.
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

# Use the module's --table mode so the audit table shares the exact
# rendering path with the "Will remove" preview below (review finding
# #1 in PR #721: was previously two near-identical Python heredocs).
TABLE_OUT="$(python3 -m lib.worktree_prune --repo "$REPO_ROOT" --table)"

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

# --table --head N reuses render_table() so the "Will remove" preview
# format matches the audit table verbatim (review finding #1 in PR
# #721: was a duplicate Python heredoc).
SELECTED_TABLE="$(python3 -m lib.worktree_prune --repo "$REPO_ROOT" --table --head "$SELECT_COUNT")"

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

# Pull paths straight from the cached JSON with jq (review finding
# #5: the third inline heredoc was dead-from-the-caller's-perspective
# code — lib.worktree_prune exposes CLI modes but jq is lighter here).
# mapfile in the current shell keeps the fail counter alive across
# iterations (subshell boundaries would swallow it).
mapfile -t SELECTED < <(printf '%s' "$JSON_OUT" | jq -r --argjson n "$SELECT_COUNT" '.[:$n] | .[].path')

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
