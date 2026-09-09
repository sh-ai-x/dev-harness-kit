"""Tests for tools/issue_sync.py — pure parser logic (no network).

Mirrors ``tests/test_linear_pr_sync.py``'s shape: import the module
under test from the ``tools/`` dir via ``sys.path`` injection, drive
the public ``parse_references`` and CLI entry point, and assert on
structured records (JSON-friendly tuples of dicts). No subprocess
calls, no network — the gate runner does the network step.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import issue_sync  # type: ignore  # noqa: E402


class KeywordTests(unittest.TestCase):
    """All close-keyword variants must be normalized to lowercase."""

    def test_close_keywords(self):
        body = "Close #1. closes #2. closed #3. Fix #4. fixes #5. fixed #6."
        refs = issue_sync.parse_references(body)
        keywords = {r["number"]: r["keyword"] for r in refs}
        self.assertEqual(
            keywords,
            {1: "close", 2: "closes", 3: "closed", 4: "fix", 5: "fixes", 6: "fixed"},
        )

    def test_resolve_keywords(self):
        body = "Resolve #10. resolves #11. resolved #12."
        refs = issue_sync.parse_references(body)
        keywords = {r["number"]: r["keyword"] for r in refs}
        self.assertEqual(keywords, {10: "resolve", 11: "resolves", 12: "resolved"})

    def test_reference_only_keywords(self):
        body = "ref #20. refs #21. reference #22. references #23. see #24."
        refs = issue_sync.parse_references(body)
        keywords = {r["number"]: r["keyword"] for r in refs}
        self.assertEqual(
            keywords,
            {20: "ref", 21: "refs", 22: "reference", 23: "references", 24: "see"},
        )

    def test_part_of_and_related_to(self):
        body = "Part of #30. part of #31. related to #32. tracking #33."
        refs = issue_sync.parse_references(body)
        keywords = {r["number"]: r["keyword"] for r in refs}
        self.assertEqual(
            keywords,
            {30: "part of", 31: "part of", 32: "related to", 33: "tracking"},
        )

    def test_bare_reference_has_null_keyword(self):
        body = "I mentioned #50 in the channel."
        refs = issue_sync.parse_references(body)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["ref"], "#50")
        self.assertIsNone(refs[0]["keyword"])


class CrossRepoTests(unittest.TestCase):
    """`owner/repo#N` references must populate owner / repo / number."""

    def test_cross_repo_owner_repo_number(self):
        body = "Fixes sh-ai-x/dev-harness-kit#42"
        refs = issue_sync.parse_references(body)
        self.assertEqual(len(refs), 1)
        r = refs[0]
        self.assertEqual(r["ref"], "sh-ai-x/dev-harness-kit#42")
        self.assertEqual(r["owner"], "sh-ai-x")
        self.assertEqual(r["repo"], "dev-harness-kit")
        self.assertEqual(r["number"], 42)
        self.assertEqual(r["keyword"], "fixes")

    def test_same_repo_refs_have_null_owner(self):
        body = "Closes #7"
        refs = issue_sync.parse_references(body)
        r = refs[0]
        self.assertEqual(r["ref"], "#7")
        self.assertIsNone(r["owner"])
        self.assertIsNone(r["repo"])
        self.assertEqual(r["number"], 7)


class StrippingTests(unittest.TestCase):
    """Code fences, inline code, and HTML comments must not leak refs."""

    def test_fenced_code_block_ignored(self):
        body = "Real: Closes #1\n```\nFake: closes #2\n```"
        refs = issue_sync.parse_references(body)
        numbers = {r["number"] for r in refs}
        self.assertEqual(numbers, {1})

    def test_inline_code_ignored(self):
        body = "Real: Closes #3. Example: `closes #4` in a template."
        refs = issue_sync.parse_references(body)
        numbers = {r["number"] for r in refs}
        self.assertEqual(numbers, {3})

    def test_html_comment_ignored(self):
        body = "Real: Closes #5\n<!-- example: closes #6 -->\nCloses #7"
        refs = issue_sync.parse_references(body)
        numbers = {r["number"] for r in refs}
        self.assertEqual(numbers, {5, 7})

    def test_tilde_fence_ignored(self):
        body = "Real: Closes #8\n~~~\nFake: closes #9\n~~~\nCloses #10"
        refs = issue_sync.parse_references(body)
        numbers = {r["number"] for r in refs}
        self.assertEqual(numbers, {8, 10})


class BoundaryTests(unittest.TestCase):
    """Word-boundary and edge cases."""

    def test_slug_with_hash_does_not_match(self):
        body = "Compare sh-ai-x/dev-harness-kit#description vs. Closes #11."
        refs = issue_sync.parse_references(body)
        refs_by_number = {r["number"]: r["ref"] for r in refs}
        self.assertEqual(refs_by_number, {11: "#11"})

    def test_title_refs_come_first(self):
        body = "Closes #20"
        title = "Tracking #21"
        refs = issue_sync.parse_references(body, title)
        sources = [(r["ref"], r["source"]) for r in refs]
        self.assertEqual(sources, [("#21", "title"), ("#20", "body")])

    def test_empty_inputs_return_empty_list(self):
        self.assertEqual(issue_sync.parse_references(""), [])
        self.assertEqual(issue_sync.parse_references("", ""), [])
        self.assertEqual(issue_sync.parse_references("", "no refs here"), [])

    def test_duplicate_refs_are_deduplicated(self):
        body = "Closes #30\ncloses #30 (twice)"
        refs = issue_sync.parse_references(body)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["number"], 30)


class CliTests(unittest.TestCase):
    """Drive ``main()`` directly with argv, no subprocess."""

    def test_parse_subcommand_returns_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = issue_sync.main(["parse", "--body", "Closes #40", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["number"], 40)

    def test_quiet_exits_nonzero_when_no_refs(self):
        rc = issue_sync.main(["parse", "--body", "no refs here", "--quiet"])
        self.assertEqual(rc, 1)

    def test_quiet_exits_zero_when_refs_found(self):
        rc = issue_sync.main(["parse", "--body", "Closes #41", "--quiet"])
        self.assertEqual(rc, 0)

    def test_version_flag(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = issue_sync.main(["--version"])
        self.assertEqual(rc, 0)
        self.assertTrue(buf.getvalue().startswith("issue_sync "))


class CliSubprocessTests(unittest.TestCase):
    """End-to-end CLI test (matches the CI sparse-checkout shape).

    The runner only checks out ``tools/issue_sync.py``; the script must
    be runnable as a plain ``python3 tools/issue_sync.py`` invocation
    with no environment variables and no extra PYTHONPATH.
    """

    def test_cli_runs_end_to_end(self):
        script = ROOT / "tools" / "issue_sync.py"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [sys.executable, str(script), "parse", "--body", "Closes #42", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(
            proc.returncode, 0, msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload[0]["number"], 42)
        self.assertEqual(payload[0]["keyword"], "closes")


if __name__ == "__main__":
    unittest.main()
