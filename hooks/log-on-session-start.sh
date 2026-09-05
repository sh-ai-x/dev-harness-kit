#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full
# log-on-session-start.sh — SessionStart hook.
#
# Auto-installs dev-kit loghooks (Stop + SessionEnd transcripts) at
# every session start. log-on is idempotent (entry merge is sentinel-
# keyed on the command string), so this never duplicates. Without
# this hook, every new `git worktree add` cuts a session that is
# invisible to /dev-kit:token-analyzer until the developer remembers
# to run `/dev-kit:log on` by hand.
#
# Discriminator (worktree-detect.sh):
#   WORKTREE_DETECT=worktree → fire (auto-copy tools/save_log.py from
#                                main checkout if missing, then log-on).
#   WORKTREE_DETECT=main     → fire IFF dev-kit is installed locally
#                              (`tools/save_log.py` in cwd) OR globally
#                              (`~/.claude/save_log.py` exists). Without
#                              either, stay silent — refuse to fabricate
#                              a setup we don't own.
#   WORKTREE_DETECT=outside  → silent (not a git working tree).
#   WORKTREE_DETECT=""       → silent (jq missing — fails open, no-op).
#
# Why main now fires: a fresh `dev-kit:bootstrap` project leaves the
# per-project loghook uninstalled until the developer runs /dev-kit:log
# on by hand. That gap is silent data loss — every main checkout session
# between bootstrap and the first manual on goes un-captured. Firing
# here closes the gap with zero manual ritual.
#
# Fails open (exit 0 with stderr warning) when `jq` is missing or
# when log-on.sh itself errors out — same policy as
# session-start-check.sh.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Warn (not fail) if jq is missing. The preamble's worktree_detect
# leaves $WORKTREE_DETECT="" when jq is absent; the case below
# already treats "" as silent.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "log-on-session-start.sh"
  exit 0
fi

# Prefer the cwd from the hook payload (more authoritative than $PWD),
# fall back to PWD if missing.
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Discriminator: already populated by the preamble.
case "$WORKTREE_DETECT" in
  worktree|main) ;;  # proceed to dev-kit check
  *) exit 0 ;;       # outside, jq-missing, or unset → silent
esac

# Dev-kit-presence check (main case only — worktree path is covered
# by the auto-copy from main below).
#
# Fires when EITHER:
#   1. tools/save_log.py is present in cwd (project install), OR
#   2. ~/.claude/save_log.py is present (global install).
#
# Without either, the project has no dev-kit logging setup and we
# refuse to fabricate one — a stub save_log.py would silently swallow
# transcripts and break /dev-kit:token-analyzer's cost picture.
if [ "$WORKTREE_DETECT" = "main" ]; then
  HAS_PROJECT_SAVE_LOG=0
  HAS_GLOBAL_SAVE_LOG=0
  [ -x "./tools/save_log.py" ] && HAS_PROJECT_SAVE_LOG=1
  [ -x "${HOME}/.claude/save_log.py" ] && HAS_GLOBAL_SAVE_LOG=1
  if [ "$HAS_PROJECT_SAVE_LOG" -eq 0 ] && [ "$HAS_GLOBAL_SAVE_LOG" -eq 0 ]; then
    # No dev-kit present. Stay silent — same as outside-git.
    exit 0
  fi
fi

# Worktree bootstrap: if the worktree is missing `tools/save_log.py`,
# try to copy it from the main checkout (the worktree's git-common-dir
# parent holds it; same content because both checkouts share a single
# git tree). Without this auto-copy, every fresh worktree stays
# invisible to /dev-kit:token-analyzer until the user runs
# `/dev-kit:log setup` by hand. If main also lacks the file, stay
# silent — the project has no logging setup at all and we won't
# fabricate one.
if [ "$WORKTREE_DETECT" = "worktree" ] && [ ! -f "tools/save_log.py" ]; then
  MAIN_CKOUT="$(git rev-parse --git-common-dir 2>/dev/null)/.."
  MAIN_CKOUT="$(cd "$MAIN_CKOUT" 2>/dev/null && pwd || true)"
  if [ -n "$MAIN_CKOUT" ] && [ -f "$MAIN_CKOUT/tools/save_log.py" ]; then
    mkdir -p tools
    cp "$MAIN_CKOUT/tools/save_log.py" tools/save_log.py
    chmod +x tools/save_log.py
  else
    exit 0
  fi
fi

# Resolve plugin root. Prefer the runtime env var; fall back to a
# path-relative resolution (matches the slop-detector pattern).
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_ON="$PLUGIN_ROOT/skills/log/scripts/log-on.sh"

if [ ! -f "$LOG_ON" ]; then
  printf 'log-on-session-start: log-on.sh missing at %s\n' "$LOG_ON" >&2
  exit 0
fi

# Run log-on with TARGET_DIR=$PWD (its default too, but explicit so
# the script's behavior doesn't drift if defaults change) and capture
# stdout for the additionalContext block.
LOG_OUTPUT="$(TARGET_DIR="$PWD" bash "$LOG_ON" 2>&1)"
LOG_RC=$?

if [ "$LOG_RC" -ne 0 ]; then
  printf 'log-on-session-start: log-on.sh rc=%d\n%s\n' "$LOG_RC" "$LOG_OUTPUT" >&2
  exit 0
fi

# Build additionalContext from the captured log-on summary. Always
# includes a brief line so the assistant sees what got installed.
SUMMARY="$(printf '%s' "$LOG_OUTPUT" | grep -E '^(claude|codex):' | head -2)"
if [ -z "$SUMMARY" ]; then
  SUMMARY="log-on idempotent (no managed-entry delta)"
fi

CTX="loghooks: auto-installed at session start in $PWD
$SUMMARY"
jq -nc --arg ctx "$CTX" \
  '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
exit 0
