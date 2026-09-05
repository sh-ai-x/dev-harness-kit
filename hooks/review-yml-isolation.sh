#!/usr/bin/env bash
# review-yml-isolation.sh — PreToolUse hook for Bash. Enforces review.yml
# PR isolation at commit time.
#
# Rule (see rules/git-workflow.md §'Review.yml PR isolation'):
#   A commit that modifies any file named `review.yml` (basename match —
#   catches both `.github/workflows/review.yml` and the template mirror
#   under `templates/ci/.github/workflows/review.yml`) must contain
#   ONLY review.yml. No source code, no other workflow files, no
#   unrelated edits.
#
# Rationale: review.yml runs `/dev-kit:review` and `/dev-kit:security`
# on every PR. Mixing it with unrelated changes makes the gate's verdict
# unreadable (you cannot tell which finding belongs to which intent) and
# blocks targeted revert when only the workflow is broken.
#
# Denies (exit 2 with deny JSON):
#   `git commit` when `git diff --cached --name-only` lists a file named
#   review.yml AND lists at least one other path.
#
# Allows (exit 0):
#   - `git commit` with review.yml alone (the one-file case).
#   - `git commit` with no review.yml in the staged set.
#   - All non-`git-commit` bash commands (the matcher filters by verb).
#   - Empty stdin payloads (probe calls).
#   - Non-git working directories.
#
# Fails closed (exit 2 with deny JSON) when `jq` is missing — without
# jq we cannot parse the PreToolUse payload, so silent fail-open would
# disable the rule. Mirrors the contract documented at
# hooks/lib/payload-parse.sh:18.

set -uo pipefail
# Source the shared stdin + jq helpers. Use %/* expansion so the source
# line still works when dirname is missing from PATH.
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
require_jq review-yml-isolation
read_stdin_json review-yml-isolation
[ -z "$INPUT_JSON" ] && exit 0

# Extract the bash command. Empty command → no commit to gate.
CMD="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // ""' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# Only gate `git commit` invocations. `git -c foo=bar commit ...` is
# also matched (case-insensitive, allowing optional global options
# between `git` and `commit`). Anything else exits 0.
shopt -s nocasematch
case "$CMD" in
  *"git commit"*|*"git-commit"*) ;;
  *) exit 0 ;;
esac

# Get the staged file list. `git diff --cached --name-only` returns
# every path in the index vs HEAD, one per line. Suppress stderr so a
# missing/non-git cwd (the hook may fire from outside any repo) does
# not pollute Claude's tool stderr.
STAGED="$(git diff --cached --name-only 2>/dev/null || true)"
[ -z "$STAGED" ] && exit 0

# Count staged files (lines, even if some names contain spaces — git
# names do not, so wc -l is fine here).
STAGED_COUNT="$(printf '%s\n' "$STAGED" | wc -l | tr -d '[:space:]')"

# Basename-match review.yml. The hook is intentionally permissive on
# path: both `.github/workflows/review.yml` (the installed workflow)
# and `templates/ci/.github/workflows/review.yml` (the template mirror)
# trip the rule because they are both 'review.yml' the user almost
# certainly means when they say 'review.yml'.
STAGED_HAS_REVIEW_YML="$(printf '%s\n' "$STAGED" | awk -F/ '{print $NF}' | grep -Fx 'review.yml' || true)"

# review.yml not staged → no isolation needed.
[ -z "$STAGED_HAS_REVIEW_YML" ] && exit 0

# review.yml staged alone → the only allowed shape.
[ "$STAGED_COUNT" = "1" ] && exit 0

# Otherwise: review.yml staged alongside other files → deny.
# Build a short, human-readable list of the co-staged paths for the
# reason text so the user sees exactly which files are colliding.
OTHERS="$(printf '%s\n' "$STAGED" | grep -vF '/review.yml' | grep -vFx 'review.yml' | tr '\n' ' ' | sed 's/ $//')"
REASON="REVIEW-YML ISOLATION: review.yml must be the ONLY file in this commit (currently staged alongside: ${OTHERS}). review.yml is the PR-review CI workflow — mixing it with unrelated changes makes the review/security gate verdict unreadable and blocks targeted revert. Either (a) split into two commits on the same branch (one review.yml-only + one for the others) so the PR groups them naturally, or (b) put the other changes on a separate branch and PR. See rules/git-workflow.md §'Review.yml PR isolation'."

  deny "REVIEW-YML ISOLATION" "$REASON"
