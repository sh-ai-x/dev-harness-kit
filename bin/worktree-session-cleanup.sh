#!/usr/bin/env bash
# Explicit session-end cleanup for one task worktree.
#
# The hook that offers this command is advisory because SessionEnd itself
# cannot receive interactive input. Removal therefore requires the caller to
# pass --decision remove after the user explicitly chooses it. The remove path
# archives logs first, refuses dirty worktrees, keeps the local branch, and
# never uses --force.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKTREE_PATH=""
DECISION="ask"

usage() {
  cat <<'EOF'
usage: bin/worktree-session-cleanup.sh --worktree PATH [--decision ask|keep|remove]

`ask` reports the eligible cleanup choice without changing anything.
`keep` explicitly retains the worktree.
`remove` archives logs and removes one clean linked worktree; its local branch
is retained. Removal never uses --force.
EOF
}

fail() {
  echo "error: $*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worktree)
      [[ $# -ge 2 ]] || fail "--worktree needs a path"
      WORKTREE_PATH="$2"
      shift 2
      ;;
    --decision)
      [[ $# -ge 2 ]] || fail "--decision needs ask, keep, or remove"
      DECISION="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument '$1'"
      ;;
  esac
done

[[ -n "$WORKTREE_PATH" ]] || fail "--worktree is required"
case "$DECISION" in
  ask|keep|remove) ;;
  *) fail "--decision must be ask, keep, or remove" ;;
esac

case "$WORKTREE_PATH" in
  /*) WT_INPUT="$WORKTREE_PATH" ;;
  *) WT_INPUT="$PWD/$WORKTREE_PATH" ;;
esac
[[ -d "$WT_INPUT" ]] || fail "worktree path is not a directory: $WT_INPUT"
WT_ROOT="$(git -C "$WT_INPUT" rev-parse --show-toplevel 2>/dev/null)" \
  || fail "path is not a registered git worktree: $WT_INPUT"
WT_ROOT="$(cd "$WT_ROOT" && pwd -P)"

# Reuse the canonical worktree metadata parser to locate the main checkout.
MAIN_ROOT="$(PYTHONPATH="$PLUGIN_ROOT/tools${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -c 'import sys; from worktree_cleanup import find_main_repo_root; print(find_main_repo_root(sys.argv[1]) or "")' \
  "$WT_ROOT" 2>/dev/null)"
[[ -n "$MAIN_ROOT" && -d "$MAIN_ROOT" ]] \
  || fail "path is the main checkout or is not linked to a main checkout: $WT_ROOT"
MAIN_ROOT="$(cd "$MAIN_ROOT" && pwd -P)"
[[ "$MAIN_ROOT" != "$WT_ROOT" ]] || fail "refusing to remove the main checkout"

# Confirm the exact path is still registered under this repository. This
# avoids treating an arbitrary nested git repository as a task worktree.
REGISTERED=0
while IFS= read -r line; do
  if [[ "$line" == "worktree $WT_ROOT" ]]; then
    REGISTERED=1
    break
  fi
done < <(git -C "$MAIN_ROOT" worktree list --porcelain 2>/dev/null)
[[ "$REGISTERED" == "1" ]] || fail "worktree is not registered by the main checkout: $WT_ROOT"

BRANCH="$(git -C "$WT_ROOT" symbolic-ref --short -q HEAD 2>/dev/null || true)"
[[ -n "$BRANCH" ]] || fail "refusing to remove a detached worktree: $WT_ROOT"

# babysit-pr owns its worktree until its retention marker is explicitly
# cleared by an operator.
RETENTION_MARKER="$WT_ROOT/.dev-kit/babysit-retention.json"
RETAINED=0
if [[ -f "$RETENTION_MARKER" ]] \
  && grep -Eq '"owner"[[:space:]]*:[[:space:]]*"babysit-pr"' "$RETENTION_MARKER"; then
  RETAINED=1
  [[ "$DECISION" != "remove" ]] \
    || fail "worktree is retained by babysit-pr: $WT_ROOT"
fi

DIRTY="$(git -C "$WT_ROOT" status --porcelain --untracked-files=all 2>/dev/null || true)"
if [[ "$DECISION" == "ask" ]]; then
  if [[ "$RETAINED" == "1" ]]; then
    STATE="retained by babysit-pr — removal is unavailable"
  elif [[ -n "$DIRTY" ]]; then
    STATE="dirty — removal is unavailable until changes are committed or moved"
  else
    STATE="clean — eligible for archive-and-remove"
  fi
  cat <<EOF
[choice-required] task worktree cleanup
  branch: $BRANCH
  path: $WT_ROOT
  state: $STATE
  keep:   bin/worktree-session-cleanup.sh --worktree "$WT_ROOT" --decision keep
  remove: bin/worktree-session-cleanup.sh --worktree "$WT_ROOT" --decision remove
EOF
  exit 0
fi

if [[ "$DECISION" == "keep" ]]; then
  echo "[kept] $WT_ROOT (branch=$BRANCH)"
  exit 0
fi

[[ -z "$DIRTY" ]] || fail "worktree is dirty; refusing removal (branch=$BRANCH)"

ARCHIVER="$PLUGIN_ROOT/tools/worktree_cleanup.py"
[[ -f "$ARCHIVER" ]] || fail "missing log archiver: $ARCHIVER"
ARCHIVE_JSON=""
if ! ARCHIVE_JSON="$(python3 "$ARCHIVER" "$WT_ROOT" --main-root "$MAIN_ROOT" --strict --json)"; then
  echo "error: log archival failed; worktree was kept: $WT_ROOT" >&2
  exit 3
fi
ARCHIVE_STATUS="$(printf '%s' "$ARCHIVE_JSON" | python3 -c \
  'import json, sys; data=json.load(sys.stdin); print(data.get("status", "error"))')"
[[ "$ARCHIVE_STATUS" != "error" ]] \
  || fail "log archival failed; worktree was kept: $WT_ROOT"

# Run from the main checkout so the process never has its cwd inside the
# directory it is removing. No --force: the clean-tree check above and git's
# own refusal remain independent safety barriers.
(cd "$MAIN_ROOT" && git worktree remove -- "$WT_ROOT") \
  || fail "git worktree remove failed; archived logs were kept: $WT_ROOT"

echo "[removed] $WT_ROOT"
echo "  branch retained: $BRANCH"
ARCHIVE_TARGET="$(printf '%s' "$ARCHIVE_JSON" | python3 -c \
  'import json, sys; print(json.load(sys.stdin).get("archive_target") or "no logs to archive")')"
echo "  logs: $ARCHIVE_TARGET"
