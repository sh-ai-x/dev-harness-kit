#!/usr/bin/env bash
# Cleanup-safe `git worktree remove` wrapper (issue #689 Phase 2).
#
# Before invoking `git worktree remove`, archives the worktree's
# `logs/` tree to AGENT_LOG_ROOT (if set) or the main checkout's
# `logs/.archive/<branch>/<ts>/`. This decouples the worktree
# lifecycle from telemetry retention so an experiment's logs survive
# even when the worktree directory is gone.
#
# Usage:
#   bin/worktree-remove-safe.sh <worktree_path> [-- git-worktree-remove-args...]
#
# Examples:
#   bin/worktree-remove-safe.sh /path/to/repo.worktrees/feat-x
#   bin/worktree-remove-safe.sh /path/to/repo.worktrees/feat-x -- --force
#
# Arguments after `--` are forwarded to `git worktree remove`.
# Set DEV_KIT_WORKTREE_REMOVE_STRICT=1 to make archival failures block
# the removal (default: warn and continue so a misconfigured
# AGENT_LOG_ROOT cannot strand a worktree).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null)" || {
  echo "error: worktree-remove-safe.sh must live inside a git repo" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  cat <<EOF >&2
usage: $(basename "$0") <worktree_path> [-- git-worktree-remove-args...]

Archives <worktree>/logs/ to AGENT_LOG_ROOT or main checkout's
logs/.archive/<branch>/<ts>/ before invoking 'git worktree remove'.
EOF
  exit 64
fi

WORKTREE_PATH="$1"
shift

# Anything after `--` is forwarded to git worktree remove verbatim.
GW_ARGS=()
if [[ "${1:-}" == "--" ]]; then
  shift
  GW_ARGS=("$@")
fi

# Resolve to absolute path so the cleanup log is unambiguous.
case "$WORKTREE_PATH" in
  /*) WT_ABS="$WORKTREE_PATH" ;;
  *)  WT_ABS="$PWD/$WORKTREE_PATH" ;;
esac

# 1. Archive in-worktree logs/ before removal. Always emit JSON so
# downstream tools can parse the result; surface the JSON on stdout
# for the operator.
ARCHIVE_ARGS=("$WT_ABS" "--main-root" "$REPO_ROOT" "--json")
[[ "${DEV_KIT_WORKTREE_REMOVE_STRICT:-0}" == "1" ]] && ARCHIVE_ARGS+=("--strict")
[[ "${DEV_KIT_WORKTREE_REMOVE_DRY_RUN:-0}" == "1" ]] && ARCHIVE_ARGS+=("--dry-run")

ARCHIVE_JSON="$(python3 "$REPO_ROOT/tools/worktree_cleanup.py" \
  "${ARCHIVE_ARGS[@]}" 2>&1)" || true
echo "$ARCHIVE_JSON"

# Surface a one-line warning on error; never block unless strict.
ARCHIVE_STATUS="$(printf '%s' "$ARCHIVE_JSON" | python3 -c "
import json, sys
try:
    r = json.loads(sys.stdin.read())
except json.JSONDecodeError:
    sys.exit(0)
sys.exit(1 if r.get('status') == 'error' else 0)
" || true)"
if [[ "$ARCHIVE_STATUS" == "1" ]]; then
  if [[ "${DEV_KIT_WORKTREE_REMOVE_STRICT:-0}" == "1" ]]; then
    echo "error: archival failed and DEV_KIT_WORKTREE_REMOVE_STRICT=1 — aborting before removal" >&2
    exit 2
  fi
  echo "warning: archival reported an error; continuing with removal (set DEV_KIT_WORKTREE_REMOVE_STRICT=1 to block)" >&2
fi

# 2. Run the actual `git worktree remove`.
git -C "$REPO_ROOT" worktree remove "$WT_ABS" "${GW_ARGS[@]}"
