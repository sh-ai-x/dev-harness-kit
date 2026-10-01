#!/usr/bin/env bash
# hook-preamble.sh — shared boilerplate sourced by every PreToolUse +
# SessionStart + UserPromptSubmit + PostToolUse hook.
#
# The header does:
#   - set -uo pipefail              (NOTE: NOT -e — many hooks tolerate
#                                    non-zero grep returns; those that
#                                    need -e can opt in after sourcing)
#   - INPUT=$(cat)                  (capture stdin payload)
#   - source lib/worktree-detect.sh
#   - emit a `::warning::jq missing` marker if jq is absent
#     (informational only — the hook body decides fail-open vs
#     fail-closed, see worktree-guard.sh for the fail-closed variant)
#   - run worktree_detect            (populates $WORKTREE_DETECT)
#
# Do NOT change the payload contract on the wire. The harness reads
# `cwd` / `hook_event_name` / `tool_name` / `tool_input` exactly as
# before; this file only deduplicates the boilerplate so the per-hook
# source files stay focused on their rule logic.
#
# Usage:
#   #!/usr/bin/env bash
#   source "$(dirname "$0")/lib/hook-preamble.sh"
#   # $INPUT is now the raw stdin payload
#   # $WORKTREE_DETECT is now "worktree" | "main" | "outside" | ""
#
# Hooks that need fail-closed on jq-missing MUST source payload-parse.sh
# and re-check `command -v jq` after the preamble. See worktree-guard.sh
# for the canonical pattern.

# Bail if executed directly — this file is meant to be sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  printf 'hook-preamble.sh must be sourced, not executed.\n' >&2
  exit 1
fi

set -uo pipefail
INPUT="$(cat)"

# Source the shared worktree-detection helper. `$WORKTREE_DETECT` will
# be set by the worktree_detect call below; a "" value means jq is
# missing (the hook body decides how to react). Use the POSIX-safe
# `${BASH_SOURCE[0]%/*}` expansion so this works on hosts where
# `dirname` is not on PATH (jq-less test envs strip dirname along
# with jq — same pattern as bash-guard.sh / slop-detector.sh).
# shellcheck source=lib/worktree-detect.sh
source "${BASH_SOURCE[0]%/*}/worktree-detect.sh"

# Emit a GitHub-Actions-style warning marker when jq is missing. The
# hook body is free to short-circuit on jq absence (`if ! command -v
# jq >/dev/null 2>&1; then ...; fi`) — this warning is informational
# so the user can tell why a rule went silent.
if ! command -v jq >/dev/null 2>&1; then
  echo "::warning::jq missing"
fi

# Populate $WORKTREE_DETECT. When jq is missing this returns 1 and
# leaves $WORKTREE_DETECT="" — the hook body's existing `case "$WORKTREE_DETECT" in`
# statements already handle "" as "silent / no-op", so this is safe
# for advisory hooks. The hard-block hook (worktree-guard.sh) MUST
# still check `command -v jq` and fail closed before relying on the
# discriminator.
worktree_detect