#!/usr/bin/env bash
# sync-version.sh — DEPRECATED no-op compat shim.
#
# History: this script used to read the version from origin/main and
# write it into the local .claude-plugin/plugin.json and
# .codex-plugin/plugin.json so a feature PR could rebase without a
# one-line conflict against the trunk. It was called from the pre-push
# hook automatically and exposed via /dev-kit:sync-version.
#
# Post-merge-queue (2026-08-30, see
# docs/proposals/release/plugin-version-bump-via-merge-queue.yaml):
# the GitHub Merge Queue now rebases every PR onto the latest main
# -- which already includes the version-bump.yml bump from any
# previously-merged PR -- immediately before merge. That eliminates
# the conflict this script existed to resolve, so the script is now
# dead code.
#
# This shim is preserved (a) so callers that still reference the path
# don't see "No such file", and (b) so the next operator who hits the
# old "local < origin/main" pre-push guard can find a pointer to the
# new mechanism without spelunking git history. The actual version
# sync is now done by .github/workflows/version-bump.yml firing on
# the merge_group event; the merge queue applies the result.
#
# If you reached this script because of a "version drift" error, the
# fix is: rebase your branch onto origin/main (the queue will do this
# automatically, but doing it locally lets you catch it before CI).
# No file edits are needed -- origin/main's manifest is the SSOT.
#
# Exit codes (preserved for any caller that still checks):
#   0 — no-op success
#   1 — local is ahead of trunk (impossible under merge queue; kept
#       only so legacy callers don't see a new exit code)
#   2 — invalid arguments or missing dependencies
#   3 — git or jq failure during sync
#   4 — target version malformed (not MAJOR.MINOR.PATCH)

set -euo pipefail

# Honor the same CLI surface as the previous version so anything that
# calls us with --check / --target / --from gets a meaningful answer
# (still 0 / "no-op" because the queue owns the sync now).
TARGET=""
SOURCE_REF="origin/main"
CHECK_ONLY=0
HELP=0

usage() {
  cat <<'EOF'
sync-version.sh — DEPRECATED no-op compat shim.

The GitHub Merge Queue now handles version sync at merge time; this
script does nothing. See:

  docs/proposals/release/plugin-version-bump-via-merge-queue.yaml
  .github/workflows/version-bump.yml  (trigger: merge_group)

If your PR failed the pre-push version-freshness check, rebase onto
origin/main and retry -- the queue will do the same thing
automatically when you mark the PR ready-for-review.
EOF
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --from)   SOURCE_REF="${2:-}"; shift 2 ;;
    --check)  CHECK_ONLY=1; shift ;;
    --help|-h) HELP=1; shift ;;
    *) echo "::error::unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$HELP" = "1" ]; then
  usage 0
fi

# Quiet success path: print one line explaining the no-op and exit 0
# so any caller that still invokes us (e.g. .githooks/pre-push during
# a migration window, or a docstring reference) sees a sane response.
echo "sync-version: deprecated no-op (merge queue handles version sync; see docs/proposals/release/plugin-version-bump-via-merge-queue.yaml)"

# Preserve the legacy --check semantics: if the caller asked us to
# report the gap, fall through to the actual git diff so the answer is
# still useful for operators debugging "why is my pre-push guard
# failing?". We don't mutate the working tree either way.
if [ "$CHECK_ONLY" = "1" ]; then
  if ! command -v jq >/dev/null 2>&1; then
    echo "::error::jq is required for --check; install with brew/apt" >&2
    exit 2
  fi
  if [ -z "$TARGET" ]; then
    if ! TARGET="$(git show "$SOURCE_REF:.claude-plugin/plugin.json" 2>/dev/null | jq -r .version 2>/dev/null)"; then
      echo "::error::could not read $SOURCE_REF:.claude-plugin/plugin.json" >&2
      exit 2
    fi
  fi
  if [ -z "$TARGET" ] || [ "$TARGET" = "null" ]; then
    echo "::error::$SOURCE_REF has no plugin.json:version" >&2
    exit 2
  fi
  LOCAL="$(jq -r .version .claude-plugin/plugin.json)"
  if [ "$LOCAL" != "$TARGET" ]; then
    echo "::notice::local=$LOCAL origin/main=$TARGET (merge queue will rebase before merge)"
    exit 1
  fi
  echo "sync-version --check: local=$LOCAL matches origin/main; no drift"
  exit 0
fi

# If a target was explicitly passed (legacy caller that hasn't
# migrated), still don't write -- just report drift.
if [ -n "$TARGET" ]; then
  echo "::notice::sync-version is a no-op; target=$TARGET ignored. Rebase onto origin/main instead."
fi

exit 0
