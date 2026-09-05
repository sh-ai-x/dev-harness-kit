#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full
# worktree-log-auto-install.sh — PostToolUse hook for Bash.
#
# When `git worktree add ...` succeeds, auto-install /dev-kit:log hooks
# (save_log.py + SessionEnd/Stop) into the NEW worktree so future sessions
# in that worktree are captured by /dev-kit:token-analyzer.
#
# Why this exists: per-worktree cost tracking requires per-worktree hook
# install. Manual `log setup --target <wt>` per worktree doesn't scale
# (98+ worktrees on a typical dev machine, each forgotten = silent data
# loss). The existing `git worktree add` command fires this hook with
# the tool_input.command on stdin; we parse out the new worktree dir
# and run log-setup/log-on for it.
#
# Safe to fail: the hook exits 0 even on parse/inspect error. Auto-install
# is a convenience, not a correctness requirement; the user can always
# run `log setup --target <dir>` manually. Logging happens on stderr so
# the user sees what (if anything) was installed.
#
# Fails open (exit 0 with stderr warning) when jq is missing — see the
# worktree-guard.sh pattern; this hook is also non-blocking.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning). The preamble also sources
# lib/worktree-detect.sh for us, so we no longer need that line.
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Source lib.sh to get jq detection + log script paths.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if ! command -v jq >/dev/null 2>&1; then
  echo "worktree-log-auto-install: jq missing; skipping auto-install (run /dev-kit:log setup manually)" >&2
  exit 0
fi

# Extract the Bash command from the tool_input. Bail out on empty.
CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# Only act on `git worktree add`. Skip detach (`worktree add --detach`),
# prune, list, remove, move, lock, unlock — they don't create a new
# captured dir.
case "$CMD" in
  *"git worktree add"*) ;;
  *) exit 0 ;;
esac

REST="${CMD#*git worktree add }"
# Strip leading flags. A flag is `--word[=val]` or `-X val`/`-Xval`.
# Easier: just find the first arg without leading `-` AND not equal to
# one of the well-known flag values (`HEAD`, `<branch>`, `<commit-ish>`).
#
# But the layout is: `git worktree add [-b branch] [-B branch] [--detach]
# [--force] [--lock] [--no-checkout] [--reason <text>] <path> [<commit>]`.
# So the first non-flag arg is the <path>, the second is the optional
# <commit-ish>.
#
# We do a simple scan: skip tokens starting with `-` (and the next token
# if the flag is `--reason` / `-f` / `--force`), and skip a token that
# matches a branch-like pattern after `-b` / `-B`. The first remaining
# token is the worktree path.
NEW_WT=""
PREV_FLAG=""
for tok in $REST; do
  case "$PREV_FLAG" in
    -b|-B|--reason|--force)
      PREV_FLAG=""
      continue
      ;;
  esac
  case "$tok" in
    --detach|--lock|--no-checkout|--quiet|-q|-f|--force)
      PREV_FLAG=""
      continue
      ;;
    -b|-B|--reason)
      PREV_FLAG="$tok"
      continue
      ;;
    --*=*)
      PREV_FLAG=""
      continue
      ;;
    -*)
      PREV_FLAG=""
      continue
      ;;
    *)
      NEW_WT="$tok"
      break
      ;;
  esac
done

if [[ -z "$NEW_WT" ]]; then
  exit 0
fi

# Resolve to an absolute path so log-setup's --target gets a real dir.
# git worktree add may accept a relative path; resolve from the current
# working directory of the command (provided as cwd in tool_input).
CWD_FROM_HOOK="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [[ -n "$CWD_FROM_HOOK" && -d "$CWD_FROM_HOOK" ]]; then
  case "$NEW_WT" in
    /*) ;;
    *)  NEW_WT="$CWD_FROM_HOOK/$NEW_WT" ;;
  esac
fi

# The worktree might not exist yet on disk (git worktree add creates it
# in the same command we just ran, but PostToolUse fires after the
# command returns — so the dir SHOULD exist). Sanity check + skip
# silently if the dir vanished.
if [[ ! -d "$NEW_WT" ]]; then
  echo "worktree-log-auto-install: new dir does not exist: $NEW_WT" >&2
  exit 0
fi

# Invoke log-setup.sh + log-on.sh against the new worktree. Re-use the
# source repo from the env (set by log-on-session-start.sh) or fall
# back to $HOME/dev/loghooks.
LOGHOOKS_DIR="${LOGHOOKS_DIR:-$HOME/dev/loghooks}"
LOG_SCRIPTS="$PLUGIN_ROOT/skills/log/scripts"

if [[ ! -x "$LOG_SCRIPTS/log-setup.sh" || ! -x "$LOG_SCRIPTS/log-on.sh" ]]; then
  echo "worktree-log-auto-install: log scripts missing at $LOG_SCRIPTS" >&2
  exit 0
fi

LOGHOOKS_DIR="$LOGHOOKS_DIR" \
  TARGET_DIR="$NEW_WT" \
  "$LOG_SCRIPTS/log-setup.sh" --target "$NEW_WT" >/dev/null 2>&1

LOGHOOKS_DIR="$LOGHOOKS_DIR" \
  TARGET_DIR="$NEW_WT" \
  "$LOG_SCRIPTS/log-on.sh" --target "$NEW_WT" --claude-only >/dev/null 2>&1

echo "worktree-log-auto-install: hooks installed at $NEW_WT" >&2
exit 0
