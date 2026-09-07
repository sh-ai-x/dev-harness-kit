#!/usr/bin/env bash
# plugin-cache-refresh.sh — SessionStart hook.
#
# Detects marketplace → versioned cache drift and auto-rsyncs the cache.
# Closes the same-version-update gap that /reload-plugins leaves open:
# Claude Code keys the cache by plugin.json:version, so a new commit at
# the same version (e.g. PR #808 adding skills/ralph/ at 0.3.350) leaves
# the cache stale until `claude plugin install dev-kit --force` is run.
#
# Drift is detected by comparing `git -C $MARKETPLACE rev-parse --short
# HEAD` against a marker file inside the cache dir. When SHA matches,
# the hook exits silently. When SHA differs, rsync runs + the marker is
# updated. Marker is stored at <cache-dir>/.devkit-refresh-head so it
# stays inside the destination (never copied by rsync on subsequent
# runs) and never collides with anything Claude Code writes.
#
# Fail-open (exit 0 always): a stale cache is recoverable by running
# bin/devkit-refresh.sh manually; a blocked session start is not.
#
# Environment overrides:
#   DEV_KIT_MARKETPLACE_DIR  default: $HOME/.claude/plugins/marketplaces/dev-kit
#   DEV_KIT_CACHE_ROOT       default: $HOME/.claude/plugins/cache/dev-kit/dev-kit
#
# Codex twin at .codex-plugin/hooks/plugin-cache-refresh.sh uses
#   CODEX_MARKETPLACE_DIR / CODEX_CACHE_ROOT defaults (this comment block
#   and the body are identical; only the env-var names change).

set -uo pipefail

MARKETPLACE_DIR="${DEV_KIT_MARKETPLACE_DIR:-$HOME/.claude/plugins/marketplaces/dev-kit}"
CACHE_ROOT="${DEV_KIT_CACHE_ROOT:-$HOME/.claude/plugins/cache/dev-kit/dev-kit}"

# Source the shared rsync + marker helper. Path resolution uses
# ${BASH_SOURCE[0]} so the hook fires identically whether installed
# from a marketplace clone or a plugin cache copy.
# shellcheck source=../lib/plugin_cache_refresh.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/plugin_cache_refresh.sh"

# Early-exit if the marketplace is not present or not a git clone.
[ -d "$MARKETPLACE_DIR" ] || exit 0
[ -d "$MARKETPLACE_DIR/.git" ] || exit 0

# Early-exit if cache root is not present. mkdir -p would also work,
# but a session that creates the cache dir for the first time should
# be a deliberate action (run bin/devkit-refresh.sh), not silent hook
# magic — so we let it stay absent and bail.
[ -d "$CACHE_ROOT" ] || exit 0

plugin_cache_sync "$MARKETPLACE_DIR" "$CACHE_ROOT" .claude-plugin 0
exit 0
