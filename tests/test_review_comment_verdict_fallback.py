"""test_review_comment_verdict_fallback.py — regression tests for the
comment-derived verdict fallback helper.

When extract-verdict.py returns PARSE_FAILED (the agent's output file
exists but no recognizable `Verdict:` line -- provider=minimax returns
a wrapper-format envelope that the parser cannot read -- issue #625),
review.yml's extract_verdict_comments step pipes the PR's claude-prefixed
comments into `_verdict_from_comment.py` and uses the recovered verdict.

These tests pin the helper's contract: it MUST
- pick the FIRST matching claude-prefixed comment's verdict
- ignore non-claude authors (humans, third-party bots)
- respect the createdAt cutoff filter (issue #244 root-cause)
- return empty string when no comment matches
- exit 2 on bad usage (no stdin / invalid JSON / non-array)

Run via subprocess because the helper is a standalone script (not a
library import). The script lives at
templates/ci/.github/workflows/_verdict_from_comment.py and reads
JSON from stdin; the workflow step pipes `gh pr view ... --json
comments --jq ...` output into it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "templates"
    / "ci"
    / ".github"
    / "workflows"
    / "_verdict_from_comment.py"
)


def _run_helper(stdin_payload: str, *, cutoff: str | None = None, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run the helper with the given JSON payload on stdin.

    Returns the CompletedProcess so test assertions can check
    stdout, stderr, and returncode.
    """
    env = os.environ.copy()
    if cutoff is not None:
        env["VERDICT_COMMENT_CUTOFF"] = cutoff
    else:
        env.pop("VERDICT_COMMENT_CUTOFF", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _comment(author_login: str, body: str, created_at: str = "2024-01-01T00:00:00Z") -> dict:
    """Build a single comment dict matching the gh CLI JSON shape."""
    return {
        "author": {"login": author_login},
        "body": body,
        "createdAt": created_at,
    }


class TestFallbackParsesClaudeVerdict(unittest.TestCase):
    """Happy path: comment from a claude-prefixed author with a Verdict line."""

    def test_fallback_parses_claude_prefixed_approve(self) -> None:
        """Plain `Verdict: Approve` from a claude-prefixed author (the prompt
explicitly forbids the bold-wrapped form because the regex doesn't
match it)."""
        comments = [_comment("claude[bot]", "Verdict: Approve\n")]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Approve")

    def test_fallback_parses_plain_text_verdict(self) -> None:
        """Plain `Verdict: Blocked` (no bold) from a claude-prefixed author."""
        comments = [_comment("claude[bot]", "Verdict: Blocked\n")]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Blocked")

    def test_fallback_parses_changes_requested(self) -> None:
        """`Verdict: Changes Requested` (multi-word verdict)."""
        comments = [_comment("claude[bot]", "Verdict: Changes Requested\n")]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Changes Requested")

    def test_fallback_parses_bold_wrapped_verdict(self) -> None:
        """Bold-wrapped `**Verdict:** <Word>` (LLM-judge Markdown form).

        The LLM judges (review, security, maintenance) post their
        verdict summary as a PR comment in Markdown with bold-wrapped
        labels (`**Verdict:** Changes Requested`). The fallback helper
        MUST recover the verdict from this form -- otherwise the
        severity gate keeps hard-failing on PARSE_FAILED for every
        LLM-judge PR (#625 follow-up).
        """
        body = (
            "## Review verdict\n\n"
            "**Verdict:** Changes Requested\n\n"
            "Findings inline below.\n"
        )
        comments = [_comment("claude[bot]", body)]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Changes Requested")

    def test_fallback_parses_bold_wrapped_approve(self) -> None:
        """Bold-wrapped `**Verdict:** Approve` should also be recognized."""
        body = "**Verdict:** Approve\n"
        comments = [_comment("claude[bot]", body)]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Approve")


class TestFallbackFilterRules(unittest.TestCase):
    """Reject non-claude authors + stale comments."""

    def test_fallback_skips_non_claude_authors(self) -> None:
        """Comments from humans are ignored even if they contain Verdict."""
        comments = [
            _comment("human-reviewer", "Verdict: Blocked\n"),
            _comment("dependabot", "Verdict: Approve\n"),
        ]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")

    def test_fallback_returns_empty_on_empty_input(self) -> None:
        """Empty array -> empty stdout, exit 0."""
        cp = _run_helper(json.dumps([]))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")

    def test_fallback_filters_stale_comments_by_cutoff(self) -> None:
        """Old createdAt + fresh Verdict -> cutoff excludes old; returns fresh."""
        old = _comment(
            "claude[bot]",
            "Verdict: Approve\n",
            created_at="2024-01-01T00:00:00Z",
        )
        fresh = _comment(
            "claude[bot]",
            "Verdict: Blocked\n",
            created_at="2024-06-01T00:00:00Z",
        )
        comments = [old, fresh]
        cp = _run_helper(json.dumps(comments), cutoff="2024-05-01T00:00:00Z")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Blocked")

    def test_fallback_accepts_no_cutoff(self) -> None:
        """No VERDICT_COMMENT_CUTOFF env set -> all comments pass the filter."""
        comments = [_comment("claude[bot]", "Verdict: Approve\n")]
        cp = _run_helper(json.dumps(comments), cutoff=None)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "Approve")

    def test_fallback_handles_missing_author_field(self) -> None:
        """Comment without author field -> skip (treated as non-claude)."""
        comments = [{"body": "Verdict: Approve\n", "createdAt": "2024-01-01T00:00:00Z"}]
        cp = _run_helper(json.dumps(comments))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")


class TestFallbackUsageErrors(unittest.TestCase):
    """Bad usage -> exit 2, no stdout."""

    def test_fallback_handles_invalid_json(self) -> None:
        """Bad JSON -> exit 2, no stdout."""
        cp = _run_helper("{not valid json")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout.strip(), "")
        self.assertIn("invalid JSON", cp.stderr)

    def test_fallback_handles_non_array_payload(self) -> None:
        """Top-level object (not array) -> exit 2."""
        cp = _run_helper(json.dumps({"comments": []}))
        self.assertEqual(cp.returncode, 2)
        self.assertIn("array", cp.stderr)

    def test_fallback_handles_empty_payload(self) -> None:
        """Empty stdin -> exit 0, empty stdout (no-op, not an error)."""
        cp = _run_helper("")
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "")


class TestFallbackHelperScript(unittest.TestCase):
    """Sanity: the script exists, is executable, and has the right shape."""

    def test_helper_script_exists(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), f"missing: {SCRIPT_PATH}")

    def test_helper_script_has_verdict_re(self) -> None:
        """The script must export / use VERDICT_RE matching extract-verdict.py:70.

        Pin the regex (not just the substring) so a docstring containing
        "Approve|Blocked|Changes Requested" without `re.compile(...)`
        cannot satisfy the assertion.
        """
        text = SCRIPT_PATH.read_text()
        self.assertIn("VERDICT_RE", text)
        self.assertRegex(
            text,
            r"VERDICT_RE\s*=\s*re\.compile\(\s*r['\"][^'\"]*\(Approve\|Blocked\|Changes Requested\)",
        )

    def test_helper_script_reads_verb_from_stdin(self) -> None:
        """The script must read comments from stdin (per the workflow step)."""
        text = SCRIPT_PATH.read_text()
        self.assertIn("sys.stdin", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
