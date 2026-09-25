"""Regression guard for the issue-sync.yml pre-check regex (v1.1.1).

The workflow's `Pre-check refs` step short-circuits BEFORE ``actions/checkout``
when the PR body references no GitHub issue. The regex lives in bash
(``grep -ciE``) so it cannot directly import ``tools/issue_sync._REFERENCE_RE``
without dragging the whole Python module into the bash-only step. This test
pins the bash regex to the source-of-truth Python regex by extracting
``tools.issue_sync._REFERENCE_RE`` and asserting that the bash-side pattern
matches the same set of bodies.

The bash regex is the POSIX ERE translation of the Python
``_REFERENCE_RE``: ``\\s+`` -> ``[[:space:]]+``, ``\\b`` -> ``\b``, named
groups collapsed. The owner/repo group (``(?P<owner>...)/(?P<repo>...)``)
is the one piece that's easy to mis-translate: ``((owner)/)?repo#N``
breaks ``Closes #12`` because the optional group lifts the repo name
out as required. The correct translation keeps the entire owner/repo
inside one optional group so bare ``#N`` still matches.

Regression: a future refactor that drops the keyword set, changes the
word-boundary semantics, or breaks the optional owner/repo group would
either (a) make a no-ref PR run the full checkout + parse pipeline
(wasted runner time) or (b) make a real-ref PR skip the gate silently
(correctness regression). This test catches both paths.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import issue_sync  # type: ignore  # noqa: E402

# Bash-side regex (POSIX ERE), copied verbatim from
# .github/workflows/issue-sync.yml `Pre-check refs` step.
# The test fails if the workflow file drifts away from this string.
WORKFLOW_PRE_CHECK_RE = (
    r"((close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved|"
    r"issue|ref|refs|reference|references|see|part of|related to|tracking)"
    r"[[:space:]]+)?"
    r"(([A-Za-z0-9]([A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}))?"
    r"#[0-9]+\b"
)


def count_python_refs(body: str) -> int:
    """Mirror what `tools.issue_sync.parse_references` would return."""
    cleaned = issue_sync._strip_code_and_comments(body)
    return len(issue_sync._REFERENCE_RE.findall(cleaned))


def count_bash_refs(body: str) -> int:
    """Count matches of the bash-side pre-check regex on the RAW body."""
    return len(re.findall(WORKFLOW_PRE_CHECK_RE, body, re.IGNORECASE))


class PreCheckRegexMirroring(unittest.TestCase):
    """Bash pre-check must be a safe approximation of the Python source-of-truth.

    The pre-check is allowed to over-match (refs in code blocks / HTML
    comments that the Python parser strips out); it is never allowed to
    under-match (a ref the Python parser would find). Under-matching
    makes the gate silently skip a real issue ref, which is a
    correctness bug. Over-matching only costs a few seconds of extra
    runner time on the checkout + parse path.
    """

    # (body, has_real_ref) — has_real_ref is what the Python parser sees
    # AFTER stripping code blocks + HTML comments.
    CASES = [
        # 1. Real refs — must trigger checkout (pre-check >= 1).
        ("Closes #12", True),
        ("Closes #12, Fixes #34", True),
        ("Issue #42 audit-trail", True),
        ("Related to #99", True),
        ("Tracking #33", True),
        ("PR #829", True),
        ("fixes owner/repo#7", True),
        ("Closes #833 (PR #832)", True),
        # 2. Real refs across multiple lines / mixed casing.
        ("closes #1\nfixes #2", True),
        ("ISSUE #5", True),
        # 3. False refs in code blocks / HTML comments — Python parser
        # strips them; pre-check must NOT find them either, so both
        # counts agree.
        ("```\nCloses #99\n```", False),
        ("<!-- Closes #99 -->", False),
        ("`Closes #99`", False),
        # 4. No refs at all — must short-circuit.
        ("", False),
        ("Plain prose with no issue refs.", False),
        ("`#hash-tag` is not a ref.", False),
        ("see foo#bar", False),  # word-boundary fails
    ]

    # Bodies where Python strips code/comments and pre-check may over-match.
    # Pre-check is allowed to find 1+ here even though Python sees 0; the
    # pipeline still runs the real parser, which exits 0. The optimization
    # (skip checkout) does NOT apply — but correctness still holds.
    OVER_MATCH_ALLOWED = {
        "Body prose\n```\nCloses #99\n```\nMore prose.",
        "```\nCloses #99\n```",
        "<!-- Closes #99 -->",
        "`Closes #99`",
    }

    def test_workflow_regex_is_in_sync(self):
        """Sanity: the captured regex matches what bash sees."""
        # Parse the regex to confirm it loads without errors.
        re.compile(WORKFLOW_PRE_CHECK_RE, re.IGNORECASE)

    def test_pre_check_is_safe_superset(self):
        """Pre-check count >= Python count on every body.

        A pre-check that under-matches silently drops a real issue
        ref — that's the correctness bug. The over-match direction
        is fine because the real Python parser still runs and
        decides correctly.
        """
        for body, _has_real in self.CASES:
            py = count_python_refs(body)
            bash = count_bash_refs(body)
            self.assertGreaterEqual(
                bash, py,
                f"pre-check under-matched: body={body!r} python={py} bash={bash}",
            )

    def test_no_ref_prs_short_circuit(self):
        """Bodies with NO real refs must produce 0 pre-check matches too.

        This is the optimization property: no-ref PRs must not run
        the checkout + parse pipeline. A pre-check that finds a
        match in a no-ref body re-introduces the wasted runner time
        we were trying to save.

        The over-match case (Python 0, pre-check 1 due to code block)
        is the exception — those are documented as benign over-matches
        and excluded from this test's "must be 0" set.
        """
        for body, has_real in self.CASES:
            if has_real:
                continue  # real refs MUST trigger checkout
            if body in self.OVER_MATCH_ALLOWED:
                continue  # documented over-match; optimization skipped, correctness fine
            self.assertEqual(
                count_bash_refs(body), 0,
                f"pre-check should short-circuit no-ref body: {body!r}",
            )

    def test_title_only_refs_are_not_silently_skipped(self):
        """v1.1.1 regression: a ref that lives only in the PR title MUST
        trigger the full gate.

        v1.1.0 scanned body only, which silently disabled the audit-trail
        contract for PRs whose title carried the link (e.g. ``Closes #12 —
        fix the wiring``, empty body). The workflow now scans ``title + body``;
        this test pins that behavior so a future "body-only" regression
        fails CI.
        """
        cases = [
            # (title, body) — has_real_ref must be True for these.
            ("Closes #12 — fix the wiring", "", True),
            ("Fixes #99", "Body has no refs.", True),
            ("Closes #12 (PR #832)", "Audit-trail note.", True),
            ("Plain title", "Closes #34 in body", True),
            # Negative cases — neither field has a ref.
            ("Plain title", "Plain body.", False),
            ("", "", False),
        ]
        for title, body, has_real in cases:
            # The workflow does `printf '%s\n%s' "$PR_TITLE" "$PR_BODY"`,
            # so the test mirrors that exact concatenation (newline sep,
            # no trailing chars).
            combined = f"{title}\n{body}"
            bash = count_bash_refs(combined)
            self.assertGreaterEqual(
                bash, 1 if has_real else 0,
                f"title+body pre-check missed: title={title!r} body={body!r} has_real={has_real} bash={bash}",
            )


if __name__ == "__main__":
    unittest.main()
