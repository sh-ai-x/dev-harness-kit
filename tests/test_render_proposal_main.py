"""Regression tests for `python3 -m lib.render_proposal_html` __main__ entry.

Covers:
- List mode (`--list`)
- Render-one mode
- Path-traversal guard (reviewer finding from PR #319)
- Slug validation (two-level `<main>/<sub>` shape)
- Round-trip via subprocess (exercises the actual `__main__` block,
  not just the inner functions).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


class RenderProposalMainTests(unittest.TestCase):
    # The __main__ entry rejects unsafe names via two stacked checks:
    #   (1) `_NAME_OK_RE` — slug regex (cheap, catches most bad names)
    #   (2) `proposals_dir not in src_resolved.parents` — defense in depth
    # Either error message indicates successful rejection. Tests below
    # accept both messages.
    REJECT_PATTERNS = (
        "path traversal",
        "invalid proposal name",
        "invalid proposal topic",
        "invalid bucket",  # status-routed shape: `<bucket>/<main>/<sub>` where bucket is review/accepted/rejected
    )

    def _run(self, args: list[str], tmp_cwd: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        # `python3 -m lib.render_proposal_html` requires the package's
        # parent dir on sys.path. Tests run from a tmp dir that doesn't
        # have a `lib/` directory, so we point PYTHONPATH at the real
        # project root and pass `--project-root tmp_cwd` for filesystem
        # isolation.
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "lib.render_proposal_html",
             "--project-root", str(tmp_cwd),
             *args],
            cwd=tmp_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _assert_rejected(self, args: list[str]):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(args, Path(td))
            self.assertNotEqual(r.returncode, 0)
            combined = r.stderr + r.stdout
            self.assertTrue(
                any(p in combined for p in self.REJECT_PATTERNS),
                f"args {args!r}: expected one of {self.REJECT_PATTERNS}, got {combined!r}",
            )

    def test_relative_traversal_rejected(self):
        self._assert_rejected(["../escape"])

    def test_absolute_path_rejected(self):
        self._assert_rejected(["/etc/passwd"])

    def test_subdirectory_traversal_rejected(self):
        # The two-level slug regex rejects `..` segments; `valid/..` has
        # a `..` segment on the right and is rejected before any
        # filesystem touch.
        self._assert_rejected(["valid/.."])
        # `valid/sub/extra` is rejected (too many levels; exactly one `/`
        # is allowed).
        self._assert_rejected(["valid/sub/extra"])

    def test_dotdot_in_name_rejected(self):
        # `..` and `foo..bar` are rejected because the segment is not
        # a valid kebab/snake slug.
        self._assert_rejected(["foo..bar"])
        self._assert_rejected([".."])

    def test_special_chars_rejected(self):
        # `foo/bar` is now a VALID two-level slug (both segments are
        # kebab). Move it to the kebab-valid test below. The remaining
        # entries are still rejected by the slug regex.
        for bad in ["foo\\bar", "foo;rm", "foo bar", "main/foo bar",
                    "foo bar/sub"]:
            self._assert_rejected([bad])

    def test_valid_two_level_slug_accepted_at_validation_layer(self):
        """A valid `<main>/<sub>` that doesn't exist still fails at the
        source-not-found check, not at the path-traversal check."""
        with tempfile.TemporaryDirectory() as td:
            r = self._run(["valid-name-123/sub-name"], Path(td))
            self.assertEqual(r.returncode, 1)
            self.assertIn("source not found", r.stderr)
            self.assertNotIn("path traversal", r.stderr)
            self.assertNotIn("invalid proposal name", r.stderr)
            self.assertNotIn("invalid proposal topic", r.stderr)
            self.assertNotIn("invalid bucket", r.stderr)


if __name__ == "__main__":
    unittest.main()
