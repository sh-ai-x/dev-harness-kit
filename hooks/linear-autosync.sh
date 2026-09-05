#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full
# hooks/linear-autosync.sh — PreToolUse Edit|Write|MultiEdit hook.
#
# Calls tools/linear_sync.py so that every Claude Code edit is
# reflected in the user's Linear workspace without a manual
# `/dev-kit:linear` invocation. The Python script is the
# authoritative gate (config + non-blocking). This wrapper exists
# to:
#   1. Pull CLAUDE_PROJECT_DIR from the hook payload.
#   2. Skip when Linear is clearly not configured across ANY of
#      the supported sources (env var, .env.linear, per-worktree
#      linear-config.json, legacy .enabled.json).
#   3. Always exit 0 (per #539: "Linear failures are non-blocking
#      for implicit workflow calls.").
#
# The fast-path is a deliberate micro-optimization. It MUST mirror
# every activation source the Python script supports; if the user
# configured Linear via user-scope `~/.config/dev-kit/.env` (Option B
# in the skill) or per-worktree `.dev-kit/.env.linear` (Option C), the
# gate is wide open and we still need to fork Python to read the key.
# Failing to check this is the single most common way auto-sync
# silently stops working.

set -uo pipefail

INPUT=$(cat)
PROJECT_DIR=$(printf '%s' "$INPUT" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
if [ -z "${PROJECT_DIR:-}" ]; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
fi

cd "$PROJECT_DIR" 2>/dev/null || exit 0

# PROJECT_DIR is reachable but doesn't look like a dev-harness-kit
# checkout (no tools/linear_sync.py). Bail silently — other Claude Code
# projects may share this hook and would otherwise emit "No such file".
if [ ! -f "$PROJECT_DIR/tools/linear_sync.py" ]; then
  exit 0
fi

# Fast-path: bail before forking Python only if NO activation
# source is present. Mirrors `_load_env_file()` + `_enabled()` in
# tools/linear_sync.py. Sources (priority order, first match wins):
#   1. $LINEAR_API_KEY env var (untouched by files)
#   2. user-scope:  $XDG_CONFIG_HOME/dev-kit/.env  (or $HOME/.config/dev-kit/.env)
#   3. per-worktree: <repo>/.dev-kit/.env.linear  (Linear-only file)
#   4. per-worktree config: <repo>/.dev-kit/linear-config.json
#   5. legacy:      <repo>/.dev-kit/.enabled.json  (mcp.linear == auto|on)
USER_ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
USER_ENV="$USER_ENV_DIR/dev-kit/.env"
if [ -z "${LINEAR_API_KEY:-}" ] && \
   [ ! -f "$USER_ENV" ] && \
   [ ! -f "$PROJECT_DIR/.dev-kit/.env.linear" ] && \
   [ ! -f "$PROJECT_DIR/.dev-kit/linear-config.json" ] && \
   [ ! -f "$PROJECT_DIR/.dev-kit/.enabled.json" ]; then
  exit 0
fi

# Disable-model-invocation users have no `python3` alias guaranteed.
# `auto-sync` (not bare `sync`) is the entry point that applies the
# repo-owner gate — see tools/linear_sync.py::auto_sync. The CLI's
# `sync` subcommand remains ungated so a non-owner can still register
# work explicitly via `/dev-kit:linear`.
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1; then
    "$py" "$PROJECT_DIR/tools/linear_sync.py" auto-sync || true
    exit 0
  fi
done

exit 0
