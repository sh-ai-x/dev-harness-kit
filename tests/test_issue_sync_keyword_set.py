"""Tests for the v1.0.0 → v1.1.0 keyword-set expansion in tools/issue_sync.py.

Issue #833 + the recovered PR #832 design: ``Issue #N`` (case-insensitive)
must be recognized as a reference keyword so PR #829's body — which used
``Issue #N`` to preserve audit-trail refs — no longer trips the gate's
strict branch. The lenient mode then demotes closed ``Issue #N`` refs to
warnings; strict mode keeps HARD FAIL behavior unchanged.

The test is a regression guard: a future change that removes ``issue``
from ``_REF_KEYWORDS`` (or moves it to ``_CLOSE_KEYWORDS``) breaks PR
#829's audit-trail pattern and re-introduces the bug.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import issue_sync  # type: ignore  # noqa: E402


class V110KeywordExpansionTests(unittest.TestCase):
    """v1.1.0 must accept ``issue`` (case-insensitive) as a ref keyword."""

    def test_issue_keyword_recognized(self):
        """`Issue #N` must produce a recognized reference with `issue` keyword."""
        refs = issue_sync.parse_references("Issue #123 is the audit-trail ref.")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["ref"], "#123")
        self.assertEqual(refs[0]["keyword"], "issue")
        self.assertEqual(refs[0]["number"], 123)

    def test_issue_keyword_case_insensitive(self):
        """`ISSUE #N`, `Issue #N`, `issue #N` all normalize to `issue`."""
        for variant in ("Issue #1", "ISSUE #1", "issue #1", "IsSuE #1"):
            refs = issue_sync.parse_references(f"{variant} goes here.")
            self.assertEqual(len(refs), 1, msg=f"variant={variant!r}")
            self.assertEqual(refs[0]["keyword"], "issue", msg=f"variant={variant!r}")
            self.assertEqual(refs[0]["number"], 1, msg=f"variant={variant!r}")

    def test_issue_in_keyword_set(self):
        """`issue` must be in the module's `_REF_KEYWORDS` tuple."""
        # The keyword set is the SSOT — guard against future renames.
        self.assertIn("issue", issue_sync._REF_KEYWORDS)
        # And must NOT be in close-keywords (Issue ≠ Closes).
        self.assertNotIn("issue", issue_sync._CLOSE_KEYWORDS)

    def test_v100_keywords_still_recognized(self):
        """Backwards compatibility: every v1.0.0 keyword still parses."""
        # Closes / Resolves / Ref / See / Part of / Related to / Tracking
        body = (
            "Closes #1. fixes #2. resolved #3. "
            "ref #4. see #5. part of #6. related to #7. tracking #8. "
            "Issue #9."
        )
        refs = issue_sync.parse_references(body)
        keywords = {r["number"]: r["keyword"] for r in refs}
        self.assertEqual(
            keywords,
            {
                1: "closes", 2: "fixes", 3: "resolved",
                4: "ref", 5: "see", 6: "part of", 7: "related to",
                8: "tracking", 9: "issue",
            },
        )


class KeywordClassificationTests(unittest.TestCase):
    """`_keyword_class` decides strict/lenient branching — guard its truth."""

    def test_close_keywords_classified_as_close(self):
        for kw in ("close", "closes", "closed", "fix", "fixes",
                   "fixed", "resolve", "resolves", "resolved"):
            self.assertEqual(issue_sync._keyword_class(kw), "close", msg=kw)

    def test_ref_keywords_classified_as_ref(self):
        for kw in ("issue", "ref", "refs", "reference", "references",
                   "see", "part of", "related to", "tracking"):
            self.assertEqual(issue_sync._keyword_class(kw), "ref", msg=kw)

    def test_bare_is_bare(self):
        self.assertEqual(issue_sync._keyword_class(None), "bare")
        self.assertEqual(issue_sync._keyword_class(""), "bare")


if __name__ == "__main__":
    unittest.main()
