#!/usr/bin/env python3
"""test_reviewer.py — RED-first tests for lib/reviewer.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import reviewer  # noqa: E402


class TestReviewer(unittest.TestCase):
    def test_render_review_approve(self):
        scores = {"architecture": 9, "correctness": 9, "convention": 9, "safety": 9}
        out = reviewer.render_review(scores, "looks great")
        self.assertIn("APPROVE", out)
        self.assertIn("architecture", out)
        self.assertIn("looks great", out)

    def test_render_review_changes_requested(self):
        scores = {"architecture": 2, "correctness": 9, "convention": 9, "safety": 9}
        out = reviewer.render_review(scores, "needs fix")
        self.assertIn("CHANGES REQUESTED", out)

    def test_render_review_comment(self):
        scores = {"architecture": 9, "correctness": 9, "convention": 5, "safety": 9}
        out = reviewer.render_review(scores, "naming issues")
        self.assertIn("COMMENT", out)

    def test_review_prompt_format(self):
        md = reviewer.REVIEW_PROMPT.format(
            title="test", body="body", files="f1.py", diff="..."
        )
        self.assertIn("test", md)
        self.assertIn("body", md)
        self.assertIn("architecture", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
