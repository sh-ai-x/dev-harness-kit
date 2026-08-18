"""test_review_verdict_comments_fallback_sh.py — smoke tests for the
shared PR-comments retry-loop script.

Issue #625 review (F1): the retry-loop was copy-pasted between the
review and security jobs (review.yml lines 419-441 and 749-771). The
extraction to `_verdict_comments_fallback.sh` is the SSOT for both
jobs; these tests pin the script's shape so a future edit cannot
silently regress the loop (e.g. reduce attempts back to 3, drop the
gh-stderr capture, drop the exhaustion warning, re-introduce the
author-fallback jq filter).

Behavioral tests at the bottom of this file actually EXECUTE the
script against a stub `gh` so a stdout/stderr split regression (which
the static pattern tests cannot catch) is caught at CI time.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
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
HELPER_PATH = (
    REPO_ROOT
    / "templates"
    / "ci"
    / ".github"
    / "workflows"
    / "_verdict_from_comment.py"
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

    def test_diagnostic_echoes_redirect_to_stderr(self) -> None:
        """F1-regression fix: every ``echo ::notice::...`` / ``echo
        ::warning::...`` in the script MUST redirect to ``>&2``.

        The pre-fix bug emitted those diagnostics on STDOUT, which the
        ``$(script "$PR_NUMBER")`` call sites captured into the verdict
        variable — producing a multi-line blob like
        ``::notice::...attempt=1/6\\\\n::notice::...verdict recovered...\\\\nApprove``
        that broke ``GITHUB_OUTPUT`` YAML parsing and the severity
        gate's rank() switch.
        """
        text = SCRIPT_PATH.read_text()
        # Match `echo "::notice::...` or `echo "::warning::...` lines that
        # are NOT followed by `>&2` on the same line. A regression
        # pattern is an unredirected diagnostic echo.
        unredirected = []
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not (stripped.startswith('echo "::notice::')
                    or stripped.startswith('echo "::warning::')):
                continue
            if ">&2" not in line:
                unredirected.append((i, line))
        self.assertEqual(
            unredirected, [],
            "diagnostic echo missing >&2 redirect (F1 stdout-pollution "
            "regression risk): " + repr(unredirected),
        )


class TestFallbackScriptBehavior(unittest.TestCase):
    """Behavioral tests — actually EXECUTE the script with a stub `gh`.

    The static pattern tests above cannot catch a stdout/stderr split
    regression because they only inspect the script text. These tests
    pin the actual runtime contract: captured stdout MUST equal the
    verdict word exactly, with no embedded diagnostic lines.
    """

    def _make_stub_gh(self, tmpdir: Path, comment_body: str,
                      created_at: str = "2030-01-01T00:00:00Z") -> Path:
        """Drop a stub ``gh`` on PATH that returns a fixed comment payload."""
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        # The script calls `gh pr view --json comments` (full payload)
        # and `gh pr view --json comments --jq '[...]'` (jq-filtered).
        # The stub bypasses `jq` by emitting the pre-filtered array
        # directly on the --jq branch so the Python helper sees exactly
        # the shape it expects on stdin.
        comment_obj = {
            "body": comment_body,
            "createdAt": created_at,
            "author": {"login": "claude[bot]"},
        }
        full_payload = json.dumps({"comments": [comment_obj]})
        jq_payload = json.dumps([comment_obj])
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == *\"--jq\"* ]]; then\n"
            "  printf '%s' " + repr(jq_payload) + "\n"
            "else\n"
            "  printf '%s' " + repr(full_payload) + "\n"
            "fi\n"
            "exit 0\n"
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return bin_dir

    def test_stdout_is_exactly_the_verdict_word(self) -> None:
        """Run the script with a stub gh that emits a Claude verdict
        comment. Captured stdout MUST equal the verdict word exactly —
        no leading whitespace, no embedded ``::notice::`` lines, no
        trailing newline. Pre-fix this returned a multi-line blob."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = self._make_stub_gh(
                tmp_path,
                comment_body="**Verdict:** Approve\n\n## review\n\nLooks good.",
            )
            # The script looks up HELPER at $WORKSPACE/.github/workflows/
            # _verdict_from_comment.py. In the dev source tree the
            # helper lives at templates/ci/.github/workflows/, so
            # WORKSPACE = $REPO_ROOT/templates/ci. In a consumer repo
            # the templates are installed at the repo root and
            # WORKSPACE = github.workspace.
            helper_workspace = REPO_ROOT / "templates" / "ci"
            self.assertTrue(
                (helper_workspace / ".github" / "workflows" / "_verdict_from_comment.py").is_file(),
                f"helper not found at {helper_workspace}/.github/workflows/_verdict_from_comment.py",
            )
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "WORKSPACE": str(helper_workspace),
                "JOB_NAME": "review",
                "CUTOFF": "",
                "ATTEMPTS": "1",
                "SLEEP_SECONDS": "0",
            }
            result = subprocess.run(
                [str(SCRIPT_PATH), "123"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0,
                f"script failed: rc={result.returncode}\n"
                f"stderr={result.stderr}",
            )
            # Captured stdout must be EXACTLY the verdict word.
            self.assertEqual(
                result.stdout, "Approve",
                f"stdout polluted by diagnostics (F1 regression). "
                f"got: {result.stdout!r}",
            )
            # The diagnostic ::notice:: line must be on STDERR, not stdout.
            self.assertIn("::notice::", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
