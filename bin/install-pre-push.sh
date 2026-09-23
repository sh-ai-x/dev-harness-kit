#!/usr/bin/env bash
# install-pre-push.sh — install hooks/pre-push.sh as the git pre-push hook.
#
# Idempotent: re-running with the same source symlinks an existing
# symlink to the same target, replaces a divergent symlink, and writes
# a fresh wrapper if no pre-push hook exists yet.
#
# Worktree-aware: each worktree has its own `.git/hooks/` directory
# (the file `.git` inside the worktree is a gitlink, not a real
# directory). `git rev-parse --git-common-dir` resolves to the shared
# gitdir; we install into `<common-dir>/hooks/pre-push`, which every
# worktree picks up because git looks at the common hooks dir first
# for worktree-level hooks (see `git config --type=bool core.hooksPath`
# precedence rules + the worktree-handling in git's run-command hooks).
#
# Default mode: symlink (fast, atomic, easy to inspect).
# Override: `INSTALL_MODE=copy ./bin/install-pre-push.sh` — copies
# the script instead of symlinking. Useful when the worktree lives on
# a different filesystem (e.g. a CI runner with read-only bind mounts)
# or when a hook manager rejects symlinks.
#
# Opt-out: pass `--uninstall` to remove the hook.
#
# Exit codes:
#   0  installed (or already correct) / uninstalled (or absent)
#   1  not a git repo
#   2  source file missing

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  printf 'install-pre-push: not inside a git working tree.\n' >&2
  exit 1
fi

SOURCE="$REPO_ROOT/hooks/pre-push.sh"
if [ ! -f "$SOURCE" ]; then
  printf 'install-pre-push: source hook not found at %s\n' "$SOURCE" >&2
  exit 2
fi

# Resolve the hooks directory:
#   1. If `core.hooksPath` is set (the repo's common shape — this
#      repo uses `/Users/sanghee/dev/dev-harness-kit/.githooks`),
#      install there. `core.hooksPath` is shared across every
#      worktree, so a single install covers them all.
#   2. Else fall back to `<git-common-dir>/hooks/`. `--git-common-dir`
#      resolves to the shared gitdir; the hooks dir there is also
#      worktree-aware (each worktree picks up the common hooks dir
#      unless it overrides `core.hooksPath` locally).
HOOKS_PATH_RAW="$(git config --get core.hooksPath 2>/dev/null || true)"
if [ -n "$HOOKS_PATH_RAW" ]; then
  case "$HOOKS_PATH_RAW" in
    /*) HOOKS_DIR="$HOOKS_PATH_RAW" ;;
    *)  HOOKS_DIR="$REPO_ROOT/$HOOKS_PATH_RAW" ;;
  esac
else
  COMMON_GITDIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  if [ -z "$COMMON_GITDIR" ]; then
    printf 'install-pre-push: failed to resolve --git-common-dir.\n' >&2
    exit 1
  fi
  case "$COMMON_GITDIR" in
    /*) HOOKS_DIR="$COMMON_GITDIR/hooks" ;;
    *)  HOOKS_DIR="$REPO_ROOT/$COMMON_GITDIR/hooks" ;;
  esac
fi
mkdir -p "$HOOKS_DIR" || {
  printf 'install-pre-push: failed to mkdir %s\n' "$HOOKS_DIR" >&2
  exit 1
}

TARGET="$HOOKS_DIR/pre-push"

_mode="symlink"
if [ "${INSTALL_MODE:-}" = "copy" ]; then
  _mode="copy"
fi

case "${1:-}" in
  --uninstall|-u)
    if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
      rm -f "$TARGET"
      printf 'install-pre-push: removed %s\n' "$TARGET"
    else
      printf 'install-pre-push: no hook at %s (nothing to remove).\n' "$TARGET"
    fi
    exit 0
    ;;
esac

# Idempotency: if the target already exists and points to the source,
# nothing to do. If it points elsewhere, replace it (the operator ran
# the install script intentionally — the pre-push stage in this repo
# is owned by hooks/pre-push.sh, not by a custom local override).
_already_correct=0
if [ -L "$TARGET" ]; then
  _resolved="$(readlink "$TARGET" 2>/dev/null || true)"
  if [ "$_resolved" = "$SOURCE" ]; then
    _already_correct=1
  fi
elif [ -f "$TARGET" ]; then
  # A real file exists (not our symlink). Diff its contents vs the
  # source — if identical, treat as already correct (idempotent
  # `INSTALL_MODE=copy` re-runs); otherwise replace.
  if cmp -s "$TARGET" "$SOURCE"; then
    _already_correct=1
  fi
fi

if [ "$_already_correct" = "1" ]; then
  printf 'install-pre-push: %s already installed at %s (mode=%s).\n' \
    "$(basename "$SOURCE")" "$TARGET" "$_mode"
  exit 0
fi

# Replace any pre-existing target before installing.
rm -f "$TARGET"

if [ "$_mode" = "copy" ]; then
  cp "$SOURCE" "$TARGET" || {
    printf 'install-pre-push: failed to cp %s -> %s\n' "$SOURCE" "$TARGET" >&2
    exit 1
  }
else
  ln -s "$SOURCE" "$TARGET" || {
    printf 'install-pre-push: failed to ln -s %s -> %s\n' "$SOURCE" "$TARGET" >&2
    exit 1
  }
fi
chmod +x "$SOURCE" 2>/dev/null || true

printf 'install-pre-push: installed %s -> %s (mode=%s)\n' "$TARGET" "$SOURCE" "$_mode"