#!/usr/bin/env bash
# sync-version.sh — Advance local plugin manifests to a target version.
#
# Why: post-#439 the trunk workflow owns the version field. Feature PRs
# keep the version they were cut at. When a parallel PR merges first
# and advances origin/main's version past the current branch, manual
# rebase is friction (rebase can re-apply unrelated commits, the user
# can fat-finger a rebase flag, the editor pop-up is non-deterministic).
# This script does the *minimum* version-only sync: read the target
# version, set it in BOTH manifests, stage, return. The caller decides
# whether to commit, push, or stop.
#
# Important: this is SYNC, not BUMP. It never increments. The trunk
# `version-bump.yml` workflow is still the single source of truth for
# the next version number. This script only makes the local branch
# catch up to a version that origin/main already published, so:
#   1. Pre-push can pass without a manual rebase.
#   2. The post-merge bump is computed against the actual trunk tip
#      (the version-bump workflow's "queued-run safety" reset).
#
# Idempotent: if the local version is already >= the target, the
# script exits 0 with no changes. Safe to call from hooks and skills.
#
# Usage:
#   bin/sync-version.sh                      # target = origin/main version
#   bin/sync-version.sh --target v0.3.294    # explicit version
#   bin/sync-version.sh --from origin/main   # override source ref
#   bin/sync-version.sh --check              # exit 0 if local >= target, 1 otherwise
#   bin/sync-version.sh --help
#
# Exit codes:
#   0 — local already at-or-above target (or successfully synced)
#   1 — local is ahead of target (refuses to roll back)
#   2 — invalid arguments or missing dependencies
#   3 — git or jq failure during sync
#   4 — target version malformed (not MAJOR.MINOR.PATCH)

set -euo pipefail

CLAUDE_MANIFEST=".claude-plugin/plugin.json"
CODEX_MANIFEST=".codex-plugin/plugin.json"
TARGET=""
SOURCE_REF="origin/main"
CHECK_ONLY=0

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --from)   SOURCE_REF="${2:-}"; shift 2 ;;
    --check)  CHECK_ONLY=1; shift ;;
    --help|-h) usage 0 ;;
    *) echo "::error::unknown arg: $1" >&2; usage 2 ;;
  esac
done

if ! command -v jq >/dev/null 2>&1; then
  echo "::error::jq is required for version sync." >&2
  echo "::error::Install:  brew install jq   |   apt install jq" >&2
  exit 2
fi

# Resolve target: explicit > source ref > origin/main
if [ -z "$TARGET" ]; then
  if ! TARGET="$(git show "$SOURCE_REF:$CLAUDE_MANIFEST" 2>/dev/null | jq -r .version 2>/dev/null)"; then
    echo "::error::could not read $SOURCE_REF:$CLAUDE_MANIFEST" >&2
    exit 2
  fi
  if [ -z "$TARGET" ] || [ "$TARGET" = "null" ]; then
    echo "::error::$SOURCE_REF has no plugin.json:version" >&2
    exit 2
  fi
fi

# Validate target shape
if ! [[ "$TARGET" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "::error::target version $TARGET is not MAJOR.MINOR.PATCH" >&2
  exit 4
fi

# Read local version
if [ ! -f "$CLAUDE_MANIFEST" ]; then
  echo "::error::$CLAUDE_MANIFEST not found in working tree" >&2
  exit 2
fi
LOCAL="$(jq -r .version "$CLAUDE_MANIFEST")"
if [ -z "$LOCAL" ] || [ "$LOCAL" = "null" ]; then
  echo "::error::local $CLAUDE_MANIFEST has empty or null version" >&2
  exit 2
fi

# Compare: HIGHER = whichever is greater per sort -V
HIGHER="$(printf '%s\n%s\n' "$LOCAL" "$TARGET" | sort -V | tail -1)"

if [ "$HIGHER" = "$LOCAL" ] && [ "$LOCAL" != "$TARGET" ]; then
  echo "::error::local $LOCAL is AHEAD of target $TARGET; refusing to roll back" >&2
  exit 1
fi

if [ "$LOCAL" = "$TARGET" ]; then
  echo "sync-version: local already at $LOCAL; no changes needed"
  exit 0
fi

if [ "$CHECK_ONLY" = "1" ]; then
  # Caller asked us to *report* the gap, not fix it.
  echo "::error::local $LOCAL < target $TARGET (sync needed)" >&2
  exit 1
fi

# Sync both manifests. Use a tmpfile + mv so a partial write can't corrupt
# the live manifest. .codex may be absent (it's a separate plugin tree
# for the Codex runtime); skip it if missing rather than failing.
for f in "$CLAUDE_MANIFEST" "$CODEX_MANIFEST"; do
  if [ ! -f "$f" ]; then
    echo "sync-version: $f not present; skipping (single-runtime checkout?)"
    continue
  fi
  jq --arg v "$TARGET" '.version = $v' "$f" > "$f.tmp" || { rm -f "$f.tmp"; exit 3; }
  mv "$f.tmp" "$f"
done

echo "sync-version: $LOCAL -> $TARGET (both manifests updated)"
