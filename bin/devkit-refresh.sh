#!/usr/bin/env bash
# devkit-refresh.sh — pull the latest dev-kit plugin into the local cache.
#
# Run this after a PR is merged to origin/main to refresh the plugin
# cache that Claude Code actually loads. Equivalent to:
#
#   cd ~/.claude/plugins/marketplaces/dev-kit && git pull origin main --ff-only
#   rsync -a --delete --exclude=.git ... ~/.claude/plugins/marketplaces/dev-kit/ \
#       ~/.claude/plugins/cache/dev-kit/dev-kit/<version>/
#
# Why this script and not `claude plugin install`:
#   - `claude plugin install` works in a regular shell, but throws a
#     Node TypeError when invoked from inside a Claude Code session
#     (cli.js:384 — pre-existing CLI bug, not ours).
#   - This script does the same job with git + rsync, both of which
#     are stable across all environments.
#
# Usage:
#   bin/devkit-refresh.sh                   # refresh dev-kit
#   bin/devkit-refresh.sh --dry-run         # show what would change
#   bin/devkit-refresh.sh --marketplace P   # override marketplace path
#   bin/devkit-refresh.sh --cache P         # override cache root
#   bin/devkit-refresh.sh --help
#
# Environment overrides:
#   DEV_KIT_MARKETPLACE_DIR  default: $HOME/.claude/plugins/marketplaces/dev-kit
#   DEV_KIT_CACHE_ROOT       default: $HOME/.claude/plugins/cache/dev-kit/dev-kit

set -euo pipefail

MARKETPLACE_DIR="${DEV_KIT_MARKETPLACE_DIR:-$HOME/.claude/plugins/marketplaces/dev-kit}"
CACHE_ROOT="${DEV_KIT_CACHE_ROOT:-$HOME/.claude/plugins/cache/dev-kit/dev-kit}"
DRY_RUN=0

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --marketplace)
      [ $# -ge 2 ] || die "--marketplace requires a path argument"
      MARKETPLACE_DIR="$2"; shift 2 ;;
    --cache)
      [ $# -ge 2 ] || die "--cache requires a path argument"
      CACHE_ROOT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) die "unknown arg: $1" ;;
  esac
done

[ -d "$MARKETPLACE_DIR/.git" ] || die "marketplace clone not found at $MARKETPLACE_DIR"
[ -d "$CACHE_ROOT" ] || die "cache root not found at $CACHE_ROOT"

# Cache-dir segment: prefer plugin.json's `version` field; fall back to the
# marketplace clone's current short SHA when the field is absent (PR #31
# dropped the field; pre-this-feature checkouts have no version). The
# fallback is the same shape Claude Code uses for commit-SHA-pinned plugins.
# Version resolution + rsync + marker write are extracted to
# lib/plugin_cache_refresh.sh so the SessionStart auto-sync can share them.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/plugin_cache_refresh.sh
source "$SCRIPT_DIR/../lib/plugin_cache_refresh.sh"

VERSION="$(plugin_cache_resolve_version "$MARKETPLACE_DIR" .claude-plugin)"
if [ -z "$VERSION" ]; then
  # No version field in plugin.json AND git rev-parse failed.
  plugin_cache_resolve_version "$MARKETPLACE_DIR" .claude-plugin >/dev/null
  # The helper falls back to short SHA when the version field is missing;
  # only print this message if that fallback also failed.
  if ! git -C "$MARKETPLACE_DIR" rev-parse --short HEAD >/dev/null 2>&1; then
    echo "  (no version field in plugin.json; git rev-parse also failed)"
  fi
fi
[ -n "$VERSION" ] || die "could not determine cache version (no version in plugin.json and git rev-parse failed)"
CACHE_DIR="$CACHE_ROOT/$VERSION"

echo "marketplace: $MARKETPLACE_DIR (version $VERSION)"
echo "cache:       $CACHE_DIR"

echo
echo "→ git pull origin main"
if [ "$DRY_RUN" = "1" ]; then
  cd "$MARKETPLACE_DIR" && git fetch origin main --quiet
  LOCAL="$(git rev-parse HEAD)"
  REMOTE="$(git rev-parse origin/main)"
  if [ "$LOCAL" = "$REMOTE" ]; then
    echo "  up to date ($LOCAL)"
  else
    echo "  $LOCAL → $REMOTE (would fast-forward)"
  fi
else
  cd "$MARKETPLACE_DIR" && git pull origin main --ff-only
fi

echo
if [ "$DRY_RUN" = "1" ]; then
  echo "→ rsync $MARKETPLACE_DIR/ → $CACHE_DIR/  (DRY RUN)"
  # Capture rsync output FIRST, then truncate. Avoids the pipefail/SIGPIPE
  # issue where `head` closing the pipe makes rsync exit 141 and abort the
  # script under `set -euo pipefail`.
  RSYNC_OUT="$(rsync -a --delete --dry-run --itemize-changes \
                --exclude='.git' --exclude='.worktrees' --exclude='.dev-kit' \
                --exclude='.eval-cache' --exclude='*.pyc' --exclude='__pycache__' \
                "$MARKETPLACE_DIR/" "$CACHE_DIR/" 2>/dev/null || true)"
  if [ -z "$RSYNC_OUT" ]; then
    echo "  (no changes)"
  else
    LINE_COUNT="$(printf '%s\n' "$RSYNC_OUT" | wc -l | tr -d ' ')"
    if [ "$LINE_COUNT" -le 30 ]; then
      printf '%s\n' "$RSYNC_OUT"
    else
      printf '%s\n' "$RSYNC_OUT" | head -30
      echo "  (truncated; $LINE_COUNT total diff lines — rerun without --dry-run to apply)"
    fi
  fi
else
  echo "→ rsync $MARKETPLACE_DIR/ → $CACHE_DIR/"
  plugin_cache_sync "$MARKETPLACE_DIR" "$CACHE_ROOT" .claude-plugin 0
  echo "  done."
fi
