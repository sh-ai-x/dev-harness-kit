#!/usr/bin/env bash
# plugin_cache_refresh.sh — pure-bash helpers sourced by both
# bin/devkit-refresh.sh (manual cache sync) and
# hooks/plugin-cache-refresh.sh (SessionStart auto-sync).
#
# The rsync exclusion list, chmod pass, marker file, and version
# resolver all live here so the manual and auto paths cannot drift.
#
# Functions:
#
#   plugin_cache_resolve_version <marketplace_dir> [plugin_subdir]
#       Print the cache version segment. Reads plugin.json:version from
#       the marketplace clone, falling back to `git rev-parse --short
#       HEAD` when the version field is absent. Output is empty on
#       total failure (caller decides how to react).
#       `plugin_subdir` defaults to `.claude-plugin`; Codex callers
#       pass `.codex-plugin`.
#
#   plugin_cache_sync <marketplace_dir> <cache_root> [plugin_subdir] [dry_run]
#       Sync marketplace -> <cache_root>/<version>/ if marketplace HEAD
#       differs from the cached marker. Idempotent -- marker file at
#       <cache_dir>/.devkit-refresh-head records the SHA that was last
#       synced. Dry-run prints the planned diff without writing.
#       Returns 0 always (caller treats total failure as soft-fail per
#       SessionStart fail-open convention).

# Bail if executed directly -- this file is meant to be sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  printf 'plugin_cache_refresh.sh must be sourced, not executed.\n' >&2
  exit 1
fi

plugin_cache_resolve_version() {
  local marketplace="$1"
  local plugin_subdir="${2:-.claude-plugin}"
  local v
  v="$(grep -m1 '"version"' "$marketplace/$plugin_subdir/plugin.json" 2>/dev/null \
       | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' || true)"
  if [ -z "$v" ]; then
    v="$(git -C "$marketplace" rev-parse --short HEAD 2>/dev/null || true)"
  fi
  printf '%s\n' "$v"
}

plugin_cache_sync() {
  local marketplace="$1"
  local cache_root="$2"
  local plugin_subdir="${3:-.claude-plugin}"
  local dry_run="${4:-0}"

  local version
  version="$(plugin_cache_resolve_version "$marketplace" "$plugin_subdir")"
  if [ -z "$version" ]; then
    echo "plugin_cache_sync: cannot determine version from $marketplace" >&2
    return 0  # soft-fail
  fi
  local cache_dir="$cache_root/$version"
  local marker="$cache_dir/.devkit-refresh-head"

  local sha
  sha="$(git -C "$marketplace" rev-parse --short HEAD 2>/dev/null || true)"

  local cached_sha=""
  if [ -f "$marker" ]; then
    cached_sha="$(cat "$marker" 2>/dev/null || true)"
  fi

  # Drift detection only fires when we can read marketplace HEAD. If git
  # is missing (test env, broken checkout), fall through to the rsync so
  # the helper stays usable in non-git scenarios; we just skip writing
  # the marker to avoid poisoning it with an empty SHA.
  if [ -n "$sha" ] && [ "$sha" = "$cached_sha" ]; then
    return 0  # in sync, silent
  fi

  local excludes=(
    --exclude='.git'
    --exclude='.worktrees'
    --exclude='.dev-kit'
    --exclude='.eval-cache'
    --exclude='*.pyc'
    --exclude='__pycache__'
  )

  mkdir -p "$cache_dir"
  if [ "$dry_run" = "1" ]; then
    rsync -a --delete --dry-run --itemize-changes "${excludes[@]}" \
      "$marketplace/" "$cache_dir/" 2>&1 || true
  else
    rsync -a --delete "${excludes[@]}" "$marketplace/" "$cache_dir/"
    # Preserve +x on hook + script files. rsync -a mirrors source bits,
    # but the destination may have been created by an earlier install
    # that left mode 0o644.
    find "$cache_dir/hooks" "$cache_dir/templates" -type f -name '*.sh' \
      -exec chmod +x {} + 2>/dev/null || true
    if [ -n "$sha" ]; then
      printf '%s\n' "$sha" > "$marker"
    fi
    echo "plugin_cache_sync: ${cached_sha:-?} -> ${sha:-?} ($marketplace -> $cache_dir)" >&2
  fi
  return 0
}
