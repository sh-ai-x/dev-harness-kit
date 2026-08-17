"""test_review_verdict_comments_fallback_sh.py — smoke tests for the
shared PR-comments retry-loop script.

Issue #625 review (F1): the retry-loop was copy-pasted between the
review and security jobs (review.yml lines 419-441 and 749-771). The
extraction to `_verdict_comments_fallback.sh` is the SSOT for both
jobs; these tests pin the script's shape so a future edit cannot
silently regress the loop (e.g. reduce attempts back to 3, drop the
gh-stderr capture, drop the exhaustion warning, re-introduce the
author-fallback jq filter).
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "templates"
    / "ci"
    / ".github"
    / "workflows"
    / "_verdict_comments_fallback.sh"
)


class TestFallbackScriptShape(unittest.TestCase):
    """Static checks on the script — pin the contract, not the syntax."""

    def test_script_exists_and_is_executable(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), f"missing: {SCRIPT_PATH}")
        self.assertTrue(
            SCRIPT_PATH.stat().st_mode & 0o111,
            f"not executable: {SCRIPT_PATH}",
        )

    def test_script_uses_six_attempts(self) -> None:
        """F1 contract: 6 attempts × 5s = 30s, spans the documented race window.

        Pre-extraction both review + security jobs used the same 6-attempt
        loop. The script's ATTEMPTS default pins this so the SSOT cannot
        regress to 3 (the pre-F1 original).
        """
        text = SCRIPT_PATH.read_text()
        self.assertRegex(text, r'ATTEMPTS=.*-6\b')
        self.assertIn('SLEEP_SECONDS', text)

    def test_script_captures_gh_stderr(self) -> None:
        """F1 contract: capture gh stderr once and surface as ::warning::.

        Pre-extraction the inline loop used `2>/dev/null || true` which
        discarded all gh error context. The extracted script must keep
        the stderr-capture-then-warn path.
        """
        text = SCRIPT_PATH.read_text()
        self.assertRegex(text, r"gh_err=\$\(gh pr view")
        self.assertIn("::warning::", text)
        self.assertIn("gh_err", text)

    def test_script_emits_exhaustion_warning(self) -> None:
        """F1 contract: emit ::warning:: when the loop ends without a verdict."""
        text = SCRIPT_PATH.read_text()
        self.assertRegex(
            text,
            r"::warning::\$\{?JOB_NAME\}? PR-comments fallback exhausted",
        )

    def test_script_does_not_duplicate_author_fallback(self) -> None:
        """F2 contract: the jq selector MUST NOT encode the author fallback.

        The author fallback `.author.login // .user.login // .login`
        was duplicated between the inline jq selector and the Python
        `_is_claude_author` helper — drift risk. The extracted script
        emits the raw `{body, createdAt, author, user, login}` shape
        and delegates author matching to the Python helper (the SSOT).

        Scoped to the actual `gh pr view --jq` invocation lines (the
        only place the selector lives); the docstring deliberately
        mentions the old selector in prose as a historical note and
        is excluded from the assertion.
        """
        text = SCRIPT_PATH.read_text()
        jq_lines = [
            line for line in text.splitlines()
            if "--jq" in line or ".comments[]" in line
        ]
        jq_blob = "\n".join(jq_lines)
        self.assertNotRegex(
            jq_blob,
            r"\.author\.login\s*//\s*\.user\.login",
            "jq selector still duplicates author-fallback; F2 not fixed",
        )
        self.assertRegex(
            jq_blob,
            r"\{\s*body,\s*createdAt,\s*author,\s*user,\s*login\s*\}",
        )

    def test_script_wraps_jq_output_in_array(self) -> None:
        """F1 contract: jq selector wrapped in `[ ... ]` so the helper
        receives a JSON ARRAY (matches stdin contract), not NDJSON."""
        text = SCRIPT_PATH.read_text()
        self.assertRegex(text, r"--jq\s*'?\[\.comments")

    def test_script_calls_python_helper(self) -> None:
        """F1 contract: the script delegates to _verdict_from_comment.py."""
        text = SCRIPT_PATH.read_text()
        self.assertIn("_verdict_from_comment.py", text)

    def test_script_requires_pr_number(self) -> None:
        """F1 contract: PR_NUMBER is required (positional arg, validated)."""
        text = SCRIPT_PATH.read_text()
        self.assertRegex(text, r'PR_NUMBER="\$\{1:\?')

    def test_script_passes_job_name_label(self) -> None:
        """F1 contract: JOB_NAME env var distinguishes review vs security
        in the ::notice:: / ::warning:: lines."""
        text = SCRIPT_PATH.read_text()
        self.assertIn("JOB_NAME", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
