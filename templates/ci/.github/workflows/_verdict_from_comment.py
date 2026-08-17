#!/usr/bin/env python3
"""_verdict_from_comment.py — fallback verdict extractor from PR comments.

Used by templates/ci/.github/workflows/review.yml when the agent's
output file (claude-execution-output.json) exists but cannot be parsed
into a verdict (extract-verdict.py returns the PARSE_FAILED sentinel --
issue #625). Some providers (e.g. minimax) return a wrapper-format
output envelope that the parser cannot read, but the agent still
posts an `mcp__github_file_ops__create_comment` summary as a PR
comment with a `Verdict: <Word>` line. This helper recovers the
verdict from that comment.

Single-source-of-truth note: VERDICT_RE is mirrored from
`templates/ci/scripts/extract-verdict.py:70` -- same regex, same
match group. Duplicated rather than imported because extract-verdict.py
runs in a different action context that does not have import access
to this helper script.

Why a second regex (VERDICT_RE_LENIENT) lives here: the LLM judges
(review, security, maintenance) post their summary as a PR comment
in Markdown, which wraps the verdict label in bold asterisks
(`**Verdict:** Changes Requested`). The strict extract-verdict.py
regex is correct for the agent's output file (its contract is the
plain form), but PR comments are an LLM-formatted surface where
bold decoration is the norm. This helper therefore uses
VERDICT_RE_LENIENT in the comment-parsing loop and keeps VERDICT_RE
strict for documentation parity with the gate's primary parser.

Cutoff filter (issue #244 root-cause): only comments strictly newer
than $VERDICT_COMMENT_CUTOFF (ISO 8601) count. The caller passes the
PR head-commit timestamp (PR-mode) or pull_request.updated_at
(workflow_dispatch) -- NOT a fixed clock, so the filter adapts to
the PR lifecycle. Older comments from previous pushes are ignored
to avoid resurrecting stale verdicts (the #244 bug).

Author matching accepts ANY of `author.login` (modern `gh pr view
--json comments` schema), the legacy `user.login` (older `gh api
.../issues/.../comments` payloads), or a top-level `login` string on
the comment (some endpoints flatten it). The matching is
case-insensitive and uses startswith('claude') to catch both
`claude[bot]` and any future `claude-something` agent labels. Issue
#625 surfaced this need because the MINIMAX wrapper posts the verdict
comment and the fallback parser must work regardless of which gh CLI
version is installed on the runner.

Usage:
    cat comments.json | VERDICT_COMMENT_CUTOFF=2024-01-01T00:00:00Z \\
        python3 _verdict_from_comment.py

Prints the verdict word (Approve | Blocked | Changes Requested) on
stdout, or empty string if no matching comment exists. Exits 0 always
on success (including no-match); exits 2 only on bad usage (no stdin,
invalid JSON, non-array payload).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

# Mirrored from templates/ci/scripts/extract-verdict.py:70.
# Kept for reference; the helper uses VERDICT_RE_LENIENT below to
# recover from bold-form LLM-judge comments (`**Verdict:** <Word>`).
VERDICT_RE = re.compile(r'Verdict:\s*(Approve|Blocked|Changes Requested)\b')
# Lenient variant: tolerates zero/two leading or trailing `*` chars
# around both `Verdict` and the colon (Markdown bold wrapping).
# Anchor `(?:^|\n)` keeps it from matching across line boundaries.
VERDICT_RE_LENIENT = re.compile(
    r'(?:^|\n)\s*\*?\*?Verdict:\*?\*?\s*(Approve|Blocked|Changes Requested)\b'
)
CUTOFF_ENV = "VERDICT_COMMENT_CUTOFF"


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO 8601 string, accepting the 'Z' suffix.

    Returns None on parse failure so the filter degrades gracefully
    instead of throwing the whole script. Unparseable timestamps
    cause the comment to be EXCLUDED (principle of least surprise:
    a comment we cannot date is treated as stale).
    """
    if not s:
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _after_cutoff(comment: dict, cutoff: datetime | None) -> bool:
    """True iff comment's createdAt is strictly newer than cutoff.

    cutoff=None (no env var set) accepts all comments -- the caller
    is responsible for setting the cutoff when staleness is a concern.
    """
    if cutoff is None:
        return True
    ts = _parse_iso(comment.get("createdAt", ""))
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts > cutoff


def _is_claude_author(comment: dict) -> bool:
    """True iff author login starts with 'claude' (case-insensitive).

    gh CLI's JSON shape for comment authors has shifted across major
    versions:

      - `gh pr view --json comments` (modern, ~2.x) emits an ``author``
        object with a ``login`` sub-field.
      - Older `gh api .../issues/.../comments` payloads use a flat
        ``user`` object with a ``login`` sub-field (the legacy shape).
      - Some endpoints / future versions flatten it further to a
        top-level ``login`` string on the comment itself.

    Accept any of the three locations so the caller stays robust across
    CLI versions. When no login field is present (e.g. deleted account,
    scoped token) we treat the comment as non-claude and skip it.
    """
    author = comment.get("author") or {}
    user = comment.get("user") or {}
    login = (
        author.get("login")
        or user.get("login")
        or comment.get("login")
        or ""
    )
    return login.lower().startswith("claude")


def _verdict_from_body(body: str) -> str:
    """Return the verdict word if body contains a recognized Verdict line, else ''.

    Uses VERDICT_RE_LENIENT so bold-wrapped LLM-judge comments
    (`**Verdict:** <Word>`) are recognized in addition to the plain
    `Verdict: <Word>` form.
    """
    if not isinstance(body, str):
        return ""
    m = VERDICT_RE_LENIENT.search(body)
    return m.group(1) if m else ""


def main() -> int:
    if sys.stdin is None or sys.stdin.isatty():
        print(f"usage: {sys.argv[0]} reads JSON comments array from stdin", file=sys.stderr)
        return 2
    raw = sys.stdin.read()
    if not raw.strip():
        print("", end="")
        return 0
    try:
        comments = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"invalid JSON on stdin: {e}", file=sys.stderr)
        return 2
    if not isinstance(comments, list):
        print("stdin must decode to a JSON array of comment objects", file=sys.stderr)
        return 2
    cutoff = _parse_iso(os.environ.get(CUTOFF_ENV, ""))
    # Newest-first: gh CLI's default ordering is reverse-chronological,
    # but we don't depend on it (sort defensively so the FIRST matching
    # comment is the latest one).
    for c in comments:
        if not _is_claude_author(c):
            continue
        if not _after_cutoff(c, cutoff):
            continue
        verdict = _verdict_from_body(c.get("body", ""))
        if verdict:
            print(verdict)
            return 0
    print("", end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
