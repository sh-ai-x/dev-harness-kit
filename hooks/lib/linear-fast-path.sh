#!/usr/bin/env bash
# linear-fast-path.sh — shared activation-source guard + python3-binary
# lookup for the four linear-* hooks (linear-autosync, linear-session-start,
# linear-task-change, linear-worktree-create).
#
# Extracted by the 2026-09-23 inspect-pass2 (findings p1-p4) to eliminate
# the 18-line byte-identical block each hook previously copy-pasted. The
# SSOT for the activation-source set lives here; hooks that add a sixth
# source update this file, not four copies.
#
# Usage from a hook:
#   source "${BASH_SOURCE[0]%/*}/lib/linear-fast-path.sh"
#   linear_fast_path "<target_dir>" "<subcommand>" [do_cd]
#
#   target_dir  absolute path of the worktree whose .dev-kit/ the sync
#               targets (typically $PWD after HOOK_CWD extraction, or
#               $WT_PATH for the worktree-create hook).
#   subcommand  tools/linear_sync.py subcommand (auto-sync, task-change-sync).
#   do_cd       "1" to `cd "$target_dir"` before invoking python3 (the
#               worktree-create hook needs this so the handoff lands in
#               the just-cut worktree, not in the bash-hook session cwd).
#
# Always returns 0 (the four callers are non-blocking per #539 and must
# never propagate exit codes back to the harness).

linear_fast_path() {
  local target_dir="$1"
  local subcommand="$2"
  local do_cd="${3:-0}"

  USER_ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
  USER_ENV="$USER_ENV_DIR/dev-kit/.env"
  if [ -z "${LINEAR_API_KEY:-}" ] && \
     [ ! -f "$USER_ENV" ] && \
     [ ! -f "$target_dir/.dev-kit/.env.linear" ] && \
     [ ! -f "$target_dir/.dev-kit/linear-config.json" ] && \
     [ ! -f "$target_dir/.dev-kit/.enabled.json" ]; then
    return 0
  fi

  # Disable-model-invocation users have no `python3` alias guaranteed.
  for py in python3 python py; do
    if command -v "$py" >/dev/null 2>&1; then
      if [ "$do_cd" = "1" ]; then
        (cd "$target_dir" && "$py" "$target_dir/tools/linear_sync.py" "$subcommand") || true
      else
        "$py" "$target_dir/tools/linear_sync.py" "$subcommand" || true
      fi
      return 0
    fi
  done
  return 0
}