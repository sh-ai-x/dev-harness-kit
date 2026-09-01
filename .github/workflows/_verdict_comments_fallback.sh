#!/usr/bin/env bash
# _verdict_comments_fallback.sh — PR-comments retry-loop fallback (issue #625).
#
# Single source of truth for the retry-loop that recovers the agent's
# verdict from a claude-prefixed PR comment when extract-verdict.py
# returned PARSE_FAILED. Previously this loop was copy-pasted between
# the review and security jobs (review.yml lines 419-441 and 749-771);
# the duplication was flagged by the #625 review (F1, retry-loop
# SSOT violation). Both jobs now call this script with different
# JOB_NAME labels so the ::notice:: / ::warning:: lines distinguish
# the call site.
#
# Contract:
#   in : $1 = PR_NUMBER
#        CUTOFF  (env, ISO-8601 timestamp — only newer comments count)
#        JOB_NAME (env, label for ::notice:: / ::warning:: lines)
#        WORKSPACE (env, default $GITHUB_WORKSPACE)
#   out: prints the recovered verdict to stdout (empty if no match)
#        ::notice:: / ::warning:: diagnostics go to STDERR so the
#        captured ``$(script "$PR_NUMBER")`` value is exactly the
#        verdict word — review.yml L418 / L727 write that value to
#        ``$GITHUB_OUTPUT`` and the severity gate parses it as a
#        single token. Mixing diagnostics into stdout would corrupt
#        the verdict variable with the diagnostic blob.
#
# The jq selector is intentionally minimal — `[.comments[] | {body,
# createdAt, author, user, login}]` — so the Python helper is the
# single source of truth for author matching. Previously the same
# `.author.login // .user.login // .login // ""` fallback was duplicated
# in jq (here) and in `_is_claude_author` (Python); drift risk is now
# eliminated because only the Python helper knows about author shape.
set -uo pipefail

PR_NUMBER="${1:?usage: $0 <PR_NUMBER>}"
CUTOFF="${CUTOFF:-}"
JOB_NAME="${JOB_NAME:-review}"
WORKSPACE="${WORKSPACE:-${GITHUB_WORKSPACE:-$(pwd)}}"

ATTEMPTS="${ATTEMPTS:-6}"
SLEEP_SECONDS="${SLEEP_SECONDS:-5}"
HELPER="${WORKSPACE}/.github/workflows/_verdict_from_comment.py"

comment_verdict=""
gh_err=""
for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "::notice::${JOB_NAME} PR-comments fallback attempt=${attempt}/${ATTEMPTS}" >&2
  # Capture gh stderr once and surface as ::warning:: so a transient
  # auth/permission/rate-limit incident is observable rather than
  # being silently discarded across all retry attempts.
  gh_err=$(gh pr view "$PR_NUMBER" --json comments 2>&1 >/dev/null) || true
  comments_json=$(gh pr view "$PR_NUMBER" --json comments \
    --jq '[.comments[] | {body, createdAt, author, user, login}]' \
    2>/dev/null || true)
  if [ -n "$gh_err" ]; then
    echo "::warning::${JOB_NAME} gh pr view stderr (attempt=${attempt}): ${gh_err}" >&2
  fi
  if [ -n "$comments_json" ]; then
    comment_verdict=$(printf '%s' "$comments_json" \
      | VERDICT_COMMENT_CUTOFF="$CUTOFF" python3 "$HELPER" 2>/dev/null \
      || true)
    if [ -n "$comment_verdict" ]; then
      break
    fi
  fi
  [ "$attempt" -lt "$ATTEMPTS" ] && sleep "$SLEEP_SECONDS"
done

if [ -n "$comment_verdict" ]; then
  echo "::notice::${JOB_NAME} verdict recovered from PR comments: ${comment_verdict}" >&2
else
  echo "::warning::${JOB_NAME} PR-comments fallback exhausted after ${ATTEMPTS} attempts" >&2
fi

# Stdout MUST carry only the verdict word (empty on exhaustion). The
# call sites use ``$(script "$PR_NUMBER")`` so any extra line on stdout
# corrupts the captured value.
printf '%s' "$comment_verdict"
