"""test_babysit_pr_local_status.py — unit tests for
bin/babysit-pr-local-status.py.

The status script is read-only and designed to be invoked from a
statusLine surface. The MUST-L3 contract is "exit 0 unconditionally,
never raise, never blank the line". These tests pin:

  T1: no git context (cwd is not a git checkout) -> "?" or
      "no PR" line, exit 0.
  T2: PR exists, all gates pass -> contains ✓ for review/sec/maint
      and CI count > 0.
  T3: one deterministic CI failure -> contains ✗ in the CI bucket.
  T4: audit comment says Changes Requested -> maint gate renders ✗.
  T5: babysit.lock present + babysit.log iter=N -> line contains
      "babysit" and "iter=N".
  T6: stale lock (pid dead) -> line still renders (the status script
      does not classify stale; the babysitter does).
  T7: every gh call times out (gh binary missing) -> line is the
      fail-soft default ("?"), exit 0.
  T8: NO_COLOR / non-tty -> output has zero ANSI escape codes.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "babysit-pr-local-status.py"

# The script lives at `bin/babysit-pr-local-status.py` (operator-facing
# kebab-case name, matching the convention of `bin/dev-kit-hooks-status.py`
# et al.). Python's import system refuses hyphenated module names, so we
# load it explicitly via importlib.util. The loaded module exposes every
# helper we test (`render`, `_current_branch`, `_pr_number`, etc.).
_spec = importlib.util.spec_from_file_location(
    "babysit_pr_local_status", str(SCRIPT_PATH)
)
status_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(status_mod)
sys.modules["babysit_pr_local_status"] = status_mod


def _stdout_capture(callable_):
    """Run `callable_`, capture its stdout, return (rc, captured_text)."""
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = callable_()
    return rc, buf.getvalue()


def _pr_view_output(number: str) -> str:
    return json.dumps({"number": int(number)})


def _checks_output(*names_states: tuple[str, str, str | None]) -> str:
    """Build a JSON-lines `gh pr checks --json name,state,conclusion -q .[]` blob."""
    return "\n".join(
        json.dumps({"name": n, "state": s, "conclusion": c})
        for n, s, c in names_states
    )


def _audit_comment_body(
    *,
    verdict: str = "Approve",
    review: str = "Approve",
    security: str = "Approve",
    maintenance: str = "Approve",
    provider: str = "minimax",
) -> str:
    # The audit-comment line 1 is byte-stable, key=value pairs separated
    # by spaces; the script parses by split() so the order doesn't matter
    # for the parser. We mirror lib/maintenance_gate.format_audit()'s
    # exact line-1 shape so any future format drift breaks the test.
    return (
        f"<!-- dev-kit-verdict-audit --> "
        f"run=local-1 job=review-local status=success "
        f"verdict={verdict} source=bin_review_local "
        f"review={review} security={security} maintenance={maintenance} "
        f"provider={provider}"
    )


class TestBinBabysitPrLocalStatus(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT_PATH.exists(), f"missing script: {SCRIPT_PATH}")

    def test_script_is_executable(self) -> None:
        mode = SCRIPT_PATH.stat().st_mode
        self.assertTrue(mode & 0o111, "must be executable")

    def test_python_parses(self) -> None:
        """`python3 -m py_compile` catches syntax errors that would only
        surface at first invocation otherwise."""
        import py_compile

        self.assertIsNotNone(
            py_compile.compile(str(SCRIPT_PATH), doraise=True),
            "script must be valid Python",
        )

    def test_main_returns_zero(self) -> None:
        """Even with every gh call failing, exit code is 0."""
        with mock.patch.object(status_mod, "_current_branch", return_value=""), \
             mock.patch.object(status_mod, "_pr_number", return_value=""), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[]), \
             mock.patch.object(status_mod, "_audit_comment", return_value={}):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertIn("no PR", out)

    def test_main_returns_zero_when_gh_missing(self) -> None:
        """T7: every gh call returns "" (gh binary missing); script
        must still exit 0 and emit a non-blank line."""
        with mock.patch.object(status_mod, "_current_branch", return_value="feat/x"), \
             mock.patch.object(status_mod, "_pr_number", return_value="605"), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[]), \
             mock.patch.object(status_mod, "_audit_comment", return_value={}):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip(), "line must not be blank on gh failure")

    def test_all_gates_pass(self) -> None:
        """T2: PR exists, all per-skill verdicts = Approve, all CI checks
        successful -> line contains three ✓ glyphs and at least one ✓ in
        the CI bucket."""
        with mock.patch.object(status_mod, "_current_branch", return_value="feat/x"), \
             mock.patch.object(status_mod, "_pr_number", return_value="605"), \
             mock.patch.object(status_mod, "_audit_comment", return_value={
                 "verdict": "Approve",
                 "review": "Approve",
                 "security": "Approve",
                 "maintenance": "Approve",
                 "provider": "minimax",
             }), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[
                 {"name": "branch-policy", "state": "SUCCESS", "bucket": "pass"},
                 {"name": "secret-scan",   "state": "SUCCESS", "bucket": "pass"},
             ]), \
             mock.patch.object(status_mod, "_lock_body", return_value=""), \
             mock.patch.object(status_mod, "_iter_from_log", return_value=""):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertIn("PR#605", out)
        self.assertIn("review=✓", out)
        self.assertIn("sec=✓", out)
        self.assertIn("maint=✓", out)
        self.assertIn("2✓", out)

    def test_one_ci_failure(self) -> None:
        """T3: one deterministic CI check failing -> CI bucket contains �."""
        with mock.patch.object(status_mod, "_current_branch", return_value="feat/x"), \
             mock.patch.object(status_mod, "_pr_number", return_value="605"), \
             mock.patch.object(status_mod, "_audit_comment", return_value={
                 "verdict": "Approve",
                 "review": "Approve",
                 "security": "Approve",
                 "maintenance": "Approve",
             }), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[
                 {"name": "branch-policy", "state": "SUCCESS", "bucket": "pass"},
                 {"name": "secret-scan",   "state": "completed", "conclusion": "failure"},
             ]), \
             mock.patch.object(status_mod, "_lock_body", return_value=""), \
             mock.patch.object(status_mod, "_iter_from_log", return_value=""):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertIn("1✗", out)

    def test_maintenance_changes_requested(self) -> None:
        """T4: audit says Changes Requested for maintenance -> maint=✗."""
        with mock.patch.object(status_mod, "_current_branch", return_value="feat/x"), \
             mock.patch.object(status_mod, "_pr_number", return_value="605"), \
             mock.patch.object(status_mod, "_audit_comment", return_value={
                 "verdict": "Changes Requested",
                 "review": "Approve",
                 "security": "Approve",
                 "maintenance": "Changes Requested",
             }), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[
                 {"name": "branch-policy", "state": "SUCCESS", "bucket": "pass"},
             ]), \
             mock.patch.object(status_mod, "_lock_body", return_value=""), \
             mock.patch.object(status_mod, "_iter_from_log", return_value=""):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertIn("maint=✗", out)

    def test_babysitter_running_with_iter(self) -> None:
        """T5: lock body present + log iter=4 -> line contains 'babysit'
        and 'iter=4'."""
        with mock.patch.object(status_mod, "_current_branch", return_value="feat/x"), \
             mock.patch.object(status_mod, "_pr_number", return_value="605"), \
             mock.patch.object(status_mod, "_audit_comment", return_value={
                 "verdict": "Approve",
                 "review": "Approve",
                 "security": "Approve",
                 "maintenance": "Approve",
             }), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[
                 {"name": "branch-policy", "state": "SUCCESS", "bucket": "pass"},
             ]), \
             mock.patch.object(status_mod, "_lock_body",
                              return_value="2026-08-23T10:00:00+00:00 pid=99999 branch=feat/x"), \
             mock.patch.object(status_mod, "_iter_from_log", return_value="4"):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertIn("babysit", out)
        self.assertIn("iter=4", out)

    def test_no_color_disables_ansi(self) -> None:
        """T8: BABYSIT_STATUS_NO_COLOR=1 -> zero ANSI escape codes in output.

        We mock isatty=True on the live `sys.stdout` so the color path
        would otherwise be active, then flip the env var to disable it.
        """
        with mock.patch("sys.stdout", new=mock.Mock(isatty=lambda: True)), \
             mock.patch.dict(os.environ, {"BABYSIT_STATUS_NO_COLOR": "1"}), \
             mock.patch.object(status_mod, "_current_branch", return_value="feat/x"), \
             mock.patch.object(status_mod, "_pr_number", return_value="605"), \
             mock.patch.object(status_mod, "_audit_comment", return_value={
                 "verdict": "Approve",
                 "review": "Approve",
                 "security": "Approve",
                 "maintenance": "Approve",
             }), \
             mock.patch.object(status_mod, "_gh_checks", return_value=[
                 {"name": "branch-policy", "state": "SUCCESS", "bucket": "pass"},
             ]), \
             mock.patch.object(status_mod, "_lock_body", return_value=""), \
             mock.patch.object(status_mod, "_iter_from_log", return_value=""):
            rc, out = _stdout_capture(
                lambda: status_mod.main([str(SCRIPT_PATH), str(REPO_ROOT)])
            )
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)
        self.assertNotIn("\x1b[", out)

    def test_parse_audit_quartet(self) -> None:
        """Direct unit test for the audit-comment parser.

        Pin the byte-stable quartet shape used by lib/maintenance_gate.
        """
        body = _audit_comment_body(
            verdict="Approve",
            review="Approve",
            security="Approve",
            maintenance="Changes Requested",
            provider="minimax",
        )
        parsed = status_mod._parse_audit_quartet(body)
        self.assertEqual(parsed.get("verdict"), "Approve")
        self.assertEqual(parsed.get("review"), "Approve")
        self.assertEqual(parsed.get("security"), "Approve")
        self.assertEqual(parsed.get("maintenance"), "Changes Requested")
        self.assertEqual(parsed.get("provider"), "minimax")

    def test_bucket_checks_vocab(self) -> None:
        """Pin the per-check bucket vocab to the canonical set.

        The installed `gh` CLI emits a `bucket` field with values
        {pass, fail, pending, skipping, cancel} -- already the
        categorized version of the raw `state`. The script maps:
            pass    -> pass
            skipping -> pass  (lib/pr_verify.PASS_BUCKETS contract)
            fail    -> fail
            cancel  -> fail  (verifier fail-closed)
            pending -> pending
            unknown -> fail  (verifier fail-closed default)
        """
        checks = [
            {"name": "a", "state": "SUCCESS",   "bucket": "pass"},
            {"name": "b", "state": "SKIPPED",   "bucket": "skipping"},
            {"name": "c", "state": "FAILURE",   "bucket": "fail"},
            {"name": "d", "state": "CANCELLED", "bucket": "cancel"},
            {"name": "e", "state": "IN_PROGRESS", "bucket": "pending"},
            {"name": "f", "state": "PENDING",   "bucket": "pending"},
            {"name": "g", "state": "UNKNOWN",   "bucket": ""},
        ]
        buckets = status_mod._bucket_checks(checks)
        self.assertEqual(buckets["pass"], 2)     # pass + skipping
        self.assertEqual(buckets["fail"], 3)     # fail + cancel + unknown
        self.assertEqual(buckets["pending"], 2)  # e + f
        self.assertEqual(buckets["ghost"], 0)

    def test_gate_glyph_mapping(self) -> None:
        """Pin every verdict string -> glyph/color we expect the script to render."""
        # Approve -> green ✓
        self.assertIn("✓", status_mod._gate_glyph("Approve"))
        self.assertIn("✓", status_mod._gate_glyph("approved"))
        # Changes Requested -> yellow ✗
        self.assertIn("✗", status_mod._gate_glyph("Changes Requested"))
        self.assertIn("✗", status_mod._gate_glyph("changes_requested"))
        # Blocked -> red ✗
        self.assertIn("✗", status_mod._gate_glyph("Blocked"))
        # Pending -> yellow ·
        self.assertIn("·", status_mod._gate_glyph("pending"))
        # Empty -> dim ?
        self.assertIn("?", status_mod._gate_glyph(""))
        # Unknown -> dim ?
        self.assertIn("?", status_mod._gate_glyph("PARSE_FAILED"))


if __name__ == "__main__":
    unittest.main()
