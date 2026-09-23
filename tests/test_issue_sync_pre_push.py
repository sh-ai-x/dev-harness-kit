"""End-to-end tests for `tools/issue_sync.py pre-push` subcommand.

Covers:
  * --strict + open ref   → exit 0, no errors
  * --strict + closed ref → exit 1, error emitted
  * --lenient + closed ref + close-keyword → exit 1 (HARD FAIL)
  * --lenient + closed ref + ref-keyword   → exit 0 (WARN)
  * --offline             → exit 0, warnings emitted (no gh api)
  * --from-log            → reads git log via subprocess (we fake git)
  * --pr-body mode        → uses explicit body text (workflow mode)
  * mutual exclusion      → --lenient + --strict → exit 2

``gh api`` is mocked at the subprocess level via ``unittest.mock.patch``
so the test is hermetic and runs in CI.
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import issue_sync  # type: ignore  # noqa: E402

SCRIPT = ROOT / "tools" / "issue_sync.py"


def _run_cli(argv: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``python3 tools/issue_sync.py <argv>`` and return the completed process."""
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN",)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=15,
    )


class StrictModeContractTests(unittest.TestCase):
    """--strict (default) → HARD FAIL on any closed ref."""

    def test_strict_default_with_open_ref_passes(self):
        """Default mode is strict; open ref → exit 0."""
        cp = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="open\n", stderr=""
        )
        with patch("subprocess.run", return_value=cp) as mock_run:
            rc = issue_sync.main(
                [
                    "pre-push",
                    "--pr-body",
                    "Closes #100",
                    "--repo",
                    "sh-ai-x/dev-harness-kit",
                ]
            )
        self.assertEqual(rc, 0)
        # Confirm we called gh api exactly once for the one ref.
        called_with = mock_run.call_args[0][0]
        # shutil.which() may resolve to an absolute path; only check the basename.
        self.assertEqual(Path(called_with[0]).name, "gh")
        self.assertEqual(called_with[1], "api")

    def test_strict_with_closed_ref_fails(self):
        """Strict + closed ref → exit 1, error recorded."""
        cp = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="closed\n", stderr=""
        )
        with patch("subprocess.run", return_value=cp):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = issue_sync.main(
                    [
                        "pre-push",
                        "--pr-body",
                        "Closes #200",
                        "--repo",
                        "sh-ai-x/dev-harness-kit",
                        "--json",
                    ]
                )
        self.assertEqual(rc, 1)
        result = json.loads(buf.getvalue())
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["ref"], "#200")
        self.assertTrue(result["errors"][0]["message"])

    def test_no_refs_passes(self):
        """Empty body → exit 0, skipped=True, no errors."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = issue_sync.main(["pre-push", "--pr-body", "no refs here", "--json"])
        self.assertEqual(rc, 0)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["skipped"])
        self.assertEqual(result["errors"], [])


class LenientModeContractTests(unittest.TestCase):
    """--lenient → WARN on ref-keywords, HARD FAIL on close-keywords + bare."""

    def test_lenient_close_keyword_still_fails(self):
        """``Closes #N`` against a closed issue → HARD FAIL even in lenient."""
        cp = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="closed\n", stderr=""
        )
        with patch("subprocess.run", return_value=cp):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = issue_sync.main(
                    [
                        "pre-push",
                        "--pr-body",
                        "Closes #300",
                        "--repo",
                        "sh-ai-x/dev-harness-kit",
                        "--lenient",
                        "--json",
                    ]
                )
        self.assertEqual(rc, 1)
        result = json.loads(buf.getvalue())
        self.assertEqual(len(result["errors"]), 1)

    def test_lenient_ref_keyword_warns_only(self):
        """``Issue #N`` against a closed issue → WARN (exit 0) in lenient."""
        cp = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="closed\n", stderr=""
        )
        with patch("subprocess.run", return_value=cp):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = issue_sync.main(
                    [
                        "pre-push",
                        "--pr-body",
                        "Issue #301 audit-trail ref",
                        "--repo",
                        "sh-ai-x/dev-harness-kit",
                        "--lenient",
                        "--json",
                    ]
                )
        self.assertEqual(rc, 0)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["ref"], "#301")

    def test_lenient_bare_ref_fails(self):
        """Bare `#N`` against a closed issue → HARD FAIL even in lenient
        (no keyword = ambiguous; default to closes semantics)."""
        cp = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="closed\n", stderr=""
        )
        with patch("subprocess.run", return_value=cp):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = issue_sync.main(
                    [
                        "pre-push",
                        "--pr-body",
                        "mentioned #400 in passing",
                        "--repo",
                        "sh-ai-x/dev-harness-kit",
                        "--lenient",
                        "--json",
                    ]
                )
        self.assertEqual(rc, 1)


class OfflineModeTests(unittest.TestCase):
    """--offline → skip gh api; warn per ref; never fail."""

    def test_offline_skips_gh_api(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = issue_sync.main(
                [
                    "pre-push",
                    "--pr-body",
                    "Closes #500\nIssue #501",
                    "--repo",
                    "sh-ai-x/dev-harness-kit",
                    "--offline",
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["skipped"])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(result["errors"], [])


class FromLogModeTests(unittest.TestCase):
    """--from-log → reads `git log <base>..HEAD` and parses commit bodies."""

    def test_from_log_parses_commit_message(self):
        # Fake `git log` to return a commit body containing a closed ref.
        cp_git = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Fix: stale ref\n\nCloses #600\n",
            stderr="",
        )
        cp_gh = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="open\n", stderr=""
        )

        # First subprocess.run is git log; subsequent are gh api.
        with patch(
            "subprocess.run",
            side_effect=[cp_git, cp_gh],
        ):
            rc = issue_sync.main(
                [
                    "pre-push",
                    "--from-log",
                    "--base",
                    "origin/main",
                    "--repo",
                    "sh-ai-x/dev-harness-kit",
                ]
            )
        self.assertEqual(rc, 0)


class MutualExclusionTests(unittest.TestCase):
    def test_lenient_and_strict_exits_2(self):
        rc = issue_sync.main(
            ["pre-push", "--pr-body", "Closes #700", "--lenient", "--strict"]
        )
        self.assertEqual(rc, 2)


class CliSubprocessIntegrationTests(unittest.TestCase):
    """End-to-end subprocess test (mirrors tests/test_issue_sync.py)."""

    def test_subprocess_pre_push_runs(self):
        cp = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "pre-push",
                "--pr-body",
                "no refs here",
                "--json",
            ],
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
            check=False,
            timeout=15,
        )
        self.assertEqual(
            cp.returncode, 0, msg=f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
        )
        result = json.loads(cp.stdout)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["refs"], [])


if __name__ == "__main__":
    unittest.main()
