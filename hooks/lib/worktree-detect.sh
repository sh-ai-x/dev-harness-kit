#!/usr/bin/env bash
# worktree-detect.sh — shared "am I in a worktree?" check for the
# every-task-new-worktree rule.
#
# Sourced (not executed) by hooks/worktree-guard.sh,
# hooks/session-start-check.sh.
# Duplication has been a maintenance trap — keep this file as the
# single source of truth for the discriminator.
#
# Public API:
#   worktree_detect [file_path]    — sets $WORKTREE_DETECT to one of:
#                                    "worktree", "main", "outside", or
#                                    leaves it empty on jq-less no-op.
#   worktree_detect_jq_missing_warn — emit a stderr warning to stdout
#                                    then echo 0 (advisory). Used by
#                                    advisory hooks (session-start-check,
#                                    session-start-check) which can't
#                                    hard-block on missing jq.

# worktree_detect [file_path] — set $WORKTREE_DETECT to "worktree" or
# "main" based on the discriminator. With a file_path, classify the
# repository containing that path when it belongs to the same repository
# as the ambient cwd. This matters when a sub-agent's hook still inherits
# the parent session's cwd while the tool targets a linked worktree.
# If the path cannot be resolved into the ambient repository, retain the
# ambient-cwd behavior. Callers without a path retain the original API.
#
# The discriminator is `git rev-parse --git-dir == --git-common-dir`:
#   - In the MAIN checkout both return the same path
#     (`.git` or its absolute form).
#   - In any WORKTREE, --git-dir returns `<common>/worktrees/<name>`
#     while --git-common-dir returns `<common>` — the two differ.
#   - Outside any git working tree, git rev-parse fails and we return
#     "outside" so the caller knows the rule does not apply.
#
# To avoid absolute-vs-relative path mismatches in subdirectories and
# on hosts where /tmp is a symlink (macOS: /tmp → /private/tmp), the
# rev-parse is run from `--show-toplevel` and both paths are
# canonicalized via `realpath`.
worktree_detect() {
  WORKTREE_DETECT=""

  if ! command -v jq >/dev/null 2>&1; then
    # jq missing — caller decides whether to fail closed or warn.
    # Both worktree-guard and the advisory hooks handle this.
    return 1
  fi

  local detect_dir="$PWD"
  local target_path="${1:-}"

  if [ -n "$target_path" ]; then
    local ambient_toplevel target_dir target_toplevel
    local ambient_common_raw target_common_raw ambient_common target_common
    ambient_toplevel="$(git rev-parse --show-toplevel 2>/dev/null)" || ambient_toplevel=""
    target_dir="$(_existing_parent_dir "$target_path")" || target_dir=""

    if [ -n "$ambient_toplevel" ] && [ -n "$target_dir" ]; then
      target_toplevel="$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)" || target_toplevel=""
      if [ -n "$target_toplevel" ]; then
        ambient_common_raw="$(cd "$ambient_toplevel" && git rev-parse --git-common-dir 2>/dev/null)" || ambient_common_raw=""
        target_common_raw="$(cd "$target_toplevel" && git rev-parse --git-common-dir 2>/dev/null)" || target_common_raw=""
        ambient_common="$(cd "$ambient_toplevel" && abspath "$ambient_common_raw")"
        target_common="$(cd "$target_toplevel" && abspath "$target_common_raw")"
        if [ -n "$ambient_common" ] && [ "$ambient_common" = "$target_common" ]; then
          detect_dir="$target_dir"
        fi
      fi
    fi
  fi

  local git_dir_raw git_common_raw toplevel
  toplevel="$(git -C "$detect_dir" rev-parse --show-toplevel 2>/dev/null)" || { WORKTREE_DETECT="outside"; return 0; }
  git_dir_raw="$(cd "$toplevel" && git rev-parse --git-dir 2>/dev/null)" || { WORKTREE_DETECT="outside"; return 0; }
  git_common_raw="$(cd "$toplevel" && git rev-parse --git-common-dir 2>/dev/null)" || { WORKTREE_DETECT="outside"; return 0; }

  local git_dir git_common
  git_dir="$(cd "$toplevel" && abspath "$git_dir_raw")"
  git_common="$(cd "$toplevel" && abspath "$git_common_raw")"
  git_dir="${git_dir%/}"
  git_common="${git_common%/}"

  if [ "$git_dir" = "$git_common" ]; then
    WORKTREE_DETECT="main"
  else
    WORKTREE_DETECT="worktree"
  fi
  return 0
}

# _existing_parent_dir — resolve a target file path to the nearest existing
# directory so `git -C` can classify edits to files that do not exist yet.
_existing_parent_dir() {
  local target_path="$1"
  local candidate
  case "$target_path" in
    /*) candidate="$target_path" ;;
    *)  candidate="$PWD/$target_path" ;;
  esac

  if [ ! -d "$candidate" ]; then
    candidate="${candidate%/*}"
    [ -n "$candidate" ] || candidate="/"
  fi

  while [ ! -d "$candidate" ]; do
    [ "$candidate" = "/" ] && return 1
    local parent="${candidate%/*}"
    [ -n "$parent" ] || parent="/"
    [ "$parent" != "$candidate" ] || return 1
    candidate="$parent"
  done

  abspath "$candidate"
}

# abspath — canonicalize a path to absolute. Uses realpath when
# available (macOS ships it by default since 10.12), falls back to
# identity / manual resolution.
abspath() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath "$p" 2>/dev/null || printf '%s' "$p"
  else
    case "$p" in
      /*) printf '%s' "$p" ;;
      *) printf '%s/%s' "$PWD" "$p" ;;
    esac
  fi
}

# worktree_detect_jq_missing_warn — emit a stderr warning when jq is
# missing. Used by the advisory hooks (session-start-check,
# session-start-check) which can't hard-block on missing jq. The
# hard-block hook (worktree-guard) fails closed (exit 2) instead.
worktree_detect_jq_missing_warn() {
  local hook_name="$1"
  printf '[%s] jq is required to enforce the worktree rule but is not installed. Install jq (apt/brew/apk) — without it, this hook is a no-op.\n' "$hook_name" >&2
  return 0
}
