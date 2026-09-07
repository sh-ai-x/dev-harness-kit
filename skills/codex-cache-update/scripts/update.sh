#!/usr/bin/env bash
# Synchronize the dev-kit Codex marketplace checkout into its versioned cache.
set -euo pipefail

MARKETPLACE_DIR="${CODEX_MARKETPLACE_DIR:-$HOME/.codex/.tmp/marketplaces/dev-kit}"
CACHE_ROOT="${CODEX_CACHE_ROOT:-$HOME/.codex/plugins/cache/dev-kit/dev-kit}"
DRY_RUN=0

usage() {
    sed -n '2,7p' "$0" | sed 's/^# //'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

command -v codex >/dev/null 2>&1 || {
    echo "error: codex CLI not found" >&2
    exit 3
}
command -v jq >/dev/null 2>&1 || {
    echo "error: jq is required" >&2
    exit 3
}
command -v rsync >/dev/null 2>&1 || {
    echo "error: rsync is required" >&2
    exit 3
}

codex plugin marketplace upgrade dev-kit

# Source the shared rsync + marker helper (extracted from the SessionStart
# hook so this script and the auto-sync path cannot drift).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../lib/plugin_cache_refresh.sh
source "$SCRIPT_DIR/../../../lib/plugin_cache_refresh.sh"

PLUGIN_JSON="$MARKETPLACE_DIR/.codex-plugin/plugin.json"
[[ -f "$PLUGIN_JSON" ]] || {
    echo "error: marketplace plugin manifest not found: $PLUGIN_JSON" >&2
    exit 4
}

VERSION="$(jq -er '.version // empty' "$PLUGIN_JSON")" || {
    echo "error: marketplace plugin version is missing" >&2
    exit 4
}
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || {
    echo "error: invalid plugin version: $VERSION" >&2
    exit 4
}

CACHE_DIR="$CACHE_ROOT/$VERSION"

echo "marketplace: $MARKETPLACE_DIR"
echo "version:     $VERSION"
echo "cache:       $CACHE_DIR"

if [[ "$DRY_RUN" -eq 1 ]]; then
    mkdir -p "$CACHE_DIR"
    plugin_cache_sync "$MARKETPLACE_DIR" "$CACHE_ROOT" .codex-plugin 1
    echo "cache dry-run complete"
else
    mkdir -p "$CACHE_DIR"
    plugin_cache_sync "$MARKETPLACE_DIR" "$CACHE_ROOT" .codex-plugin 0
    echo "cache synchronized"
fi
