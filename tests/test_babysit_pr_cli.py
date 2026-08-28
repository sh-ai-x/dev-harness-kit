"""test_babysit_pr_cli.py -- unit tests for lib/babysit_pr_cli.py.

Pins the contract for the `--operator-is-only-human` opt-out (issue #324):

  parse_babysit_args(argv)
    T1: empty argv -> namespace with operator_is_only_human=False, rationale=""
    T2: "--operator-is-only-human" -> operator_is_only_human=True
    T3: "--rationale=..." sets rationale (must accompany the flag)
    T4: unknown flag -> SystemExit (argparse)

  parse_codeowners(path)
    T5: missing file -> raises FileNotFoundError (fail-closed contract;
        run_babysit_once catches and refuses the bypass)
    T6: typical CODEOWNERS file -> unique handles (no @, no comments)
    T7: capture group with @org/team-name -> "org/team-name"
    T8: dedup across multiple rules
    T9: email handles are ignored (CODEOWNERS user@domain format)
    T10 (run_babysit_once): missing CODEOWNERS -> EXIT_OWNERSHIP_UNKNOWN with
         "could not read CODEOWNERS" -- the bypass refuses to authorize
         auto-merge when ownership cannot be confirmed

  has_alternate_owners(operator_handle, codeowner_handles,
                        collaborator_handles=())
    T10: single owner that matches operator -> (False, [])
    T11: operator + one other owner -> (True, ["other"])
    T12: operator + team handle -> (True, ["org/team-name"])
    T13: operator absent entirely -> (False, []) (no alternates known)
    T14: multiple collaborators listed -> (True, [...])
    T15: CODEOWNERS + collaborators are unioned

  format_ownership_confirmed_comment(operator, rationale, now_iso)
    T16: emits "/ownership-confirmed by operator=<handle> at <iso>; rationale=<text>"
    T17: rationale with semicolons is preserved verbatim

  run_babysit_once(...)
    T18: flag absent -> returns EXIT_OK (0) with human-gate hand-off
         message; never posts a comment; never calls gh pr merge.
    T19: flag present + single-owner -> returns EXIT_OK (0), posts the
         /ownership-confirmed comment. Auto-merge is disabled by
         policy -- gh pr merge is never called; a human merges
         manually.
    T20: flag present + multi-owner -> returns EXIT_MULTI_OWNER (1)
         with the alternate-owner list + remediation pointer.
    T21: flag present but rationale missing -> EXIT_RATIONALE_REQUIRED
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import babysit_pr_cli as bpc  # noqa: E402


class _CliResult:
    """Captured CLI output for assertions.

    Mirrors the field set used by the babysit-pr skill's stdout/stderr
    contract: the human-gate hand-off message and the audit-comment
    body. There is no merge-command field -- the orchestrator never
    calls `gh pr merge`; merging into main is always a human action.
    """

    def __init__(self) -> None:
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.commented: list[tuple[str, str]] = []  # (pr_number, body)


def _write_codeowners(tmp: Path, body: str) -> Path:
    p = tmp / "CODEOWNERS"
    p.write_text(body, encoding="utf-8")
    return p


class TestParseBabysitArgs(unittest.TestCase):
    def test_empty_argv(self) -> None:
        ns = bpc.parse_babysit_args([])
        self.assertFalse(ns.operator_is_only_human)
        self.assertEqual(ns.rationale, "")

    def test_only_human_flag_set(self) -> None:
        ns = bpc.parse_babysit_args(["--operator-is-only-human"])
        self.assertTrue(ns.operator_is_only_human)
        self.assertEqual(ns.rationale, "")

    def test_rationale_pairs_with_flag(self) -> None:
        ns = bpc.parse_babysit_args([
            "--operator-is-only-human",
            "--rationale", "single-operator merge of trivial docs fix",
        ])
        self.assertTrue(ns.operator_is_only_human)
        self.assertEqual(
            ns.rationale,
            "single-operator merge of trivial docs fix",
        )

    def test_explicit_pr_target(self) -> None:
        ns = bpc.parse_babysit_args(["--pr", "522"])
        self.assertEqual(ns.pr, 522)

    def test_unknown_flag_exits(self) -> None:
        with self.assertRaises(SystemExit):
            bpc.parse_babysit_args(["--no-such-flag"])

    # --- T22-T24: --local-verify removed; --local-test-cmd is hidden --------
    # The opt-in `--local-verify` gate duplicated /dev-kit:babysit-pr-local,
    # which runs the same pytest gate unconditionally. The flag is gone;
    # `--local-test-cmd` survives as the hidden override used by the
    # local-mode skill (and by run_local_verify's callers).
    def test_local_verify_flag_is_rejected(self) -> None:
        """T22: --local-verify is no longer a registered flag -- argparse
        must treat it as unknown so a stale invocation fails loudly
        instead of silently no-opping."""
        with self.assertRaises(SystemExit):
            bpc.parse_babysit_args(["--local-verify"])

    def test_no_local_verify_namespace_field(self) -> None:
        """T22b: the Namespace no longer carries `local_verify`; callers
        that branched on it must be updated, not silently see False."""
        ns = bpc.parse_babysit_args([])
        self.assertFalse(hasattr(ns, "local_verify"))
        self.assertEqual(ns.local_test_cmd, "pytest -q")

    def test_local_test_cmd_override(self) -> None:
        """T24: --local-test-cmd captures the override verbatim without
        any companion flag. The skill body runs the command via
        subprocess; the helper only stores it (pure-function contract)."""
        ns = bpc.parse_babysit_args([
            "--local-test-cmd", "pytest -x tests/",
        ])
        self.assertEqual(ns.local_test_cmd, "pytest -x tests/")

    def test_local_test_cmd_compatible_with_other_flags(self) -> None:
        """--local-test-cmd coexists with --pr and --operator-is-only-human
        / --rationale; nothing in the parser mutually excludes them."""
        ns = bpc.parse_babysit_args([
            "--pr", "42",
            "--local-test-cmd", "make test",
        ])
        self.assertEqual(ns.pr, 42)
        self.assertEqual(ns.local_test_cmd, "make test")


class TestParseCodeowners(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_missing_raises_for_fail_closed_contract(self) -> None:
        """Missing file must raise -- the bypass cannot interpret
        "could not read" as "no alternate owners". An outage or
        permission glitch must not authorize the auto-merge.

        The previous behaviour returned ``[]`` and let the bypass
        authorize, which was a security-sensitive failure mode.
        """
        with self.assertRaises(FileNotFoundError):
            bpc.parse_codeowners(self.tmp / "absent")

    def test_unreadable_raises_for_fail_closed_contract(self) -> None:
        """IsADirectoryError + permission errors also raise. The
        orchestrator catches OSError broadly and refuses the bypass."""
        # An existing directory passed where a file is expected: most
        # platforms raise IsADirectoryError; some raise PermissionError.
        with self.assertRaises((IsADirectoryError, PermissionError, OSError)):
            bpc.parse_codeowners(self.tmp)

    def test_typical_file_dedupes(self) -> None:
        p = _write_codeowners(self.tmp, (
            "# top-level owner\n"
            "*                                          @sh-ai-x\n"
            "/rules/                                    @sh-ai-x\n"
            "/CLAUDE.md                                 @sh-ai-x\n"
        ))
        self.assertEqual(bpc.parse_codeowners(p), ["sh-ai-x"])

    def test_team_handles_preserved(self) -> None:
        p = _write_codeowners(self.tmp, (
            "*  @sh-ai-x @my-org/devs\n"
        ))
        self.assertEqual(
            sorted(bpc.parse_codeowners(p)),
            ["my-org/devs", "sh-ai-x"],
        )

    def test_dedup_across_rules(self) -> None:
        p = _write_codeowners(self.tmp, (
            "*                  @sh-ai-x @alice\n"
            "/some/path/        @alice @bob\n"
        ))
        self.assertEqual(
            sorted(bpc.parse_codeowners(p)),
            ["alice", "bob", "sh-ai-x"],
        )

    def test_email_handles_are_ignored(self) -> None:
        # GitHub CODEOWNERS supports `user@domain` style; the bypass
        # contract only matters for actionable reviewer handles. Email
        # entries do not constrain the human-review gate, so they are
        # stripped.
        p = _write_codeowners(self.tmp, (
            "*  @sh-ai-x someone@example.com\n"
        ))
        self.assertEqual(bpc.parse_codeowners(p), ["sh-ai-x"])


class TestHasAlternateOwners(unittest.TestCase):
    def test_single_owner_matches_operator(self) -> None:
        ok, alts = bpc.has_alternate_owners(
            operator_handle="sh-ai-x",
            codeowner_handles=["sh-ai-x"],
            collaborator_handles=["sh-ai-x"],
        )
        self.assertFalse(ok)
        self.assertEqual(alts, [])

    def test_one_other_owner_blocks(self) -> None:
        ok, alts = bpc.has_alternate_owners(
            operator_handle="sh-ai-x",
            codeowner_handles=["sh-ai-x", "alice"],
            collaborator_handles=["sh-ai-x", "alice"],
        )
        self.assertTrue(ok)
        self.assertEqual(alts, ["alice"])

    def test_team_handle_blocks(self) -> None:
        ok, alts = bpc.has_alternate_owners(
            operator_handle="sh-ai-x",
            codeowner_handles=["sh-ai-x", "my-org/devs"],
            collaborator_handles=["sh-ai-x"],
        )
        self.assertTrue(ok)
        self.assertEqual(alts, ["my-org/devs"])

    def test_operator_not_in_codeowners_returns_no_alternates(self) -> None:
        # `has_alternate_owners` itself returns (False, []) when the
        # operator is absent from both lists -- *no alternates known*
        # is not the same as *single-operator confirmed*. The
        # orchestrator layer (`run_babysit_once`) enforces the
        # positive-ownership check on top; this helper just stays
        # pure and reports what's in the lists.
        ok, alts = bpc.has_alternate_owners(
            operator_handle="sh-ai-x",
            codeowner_handles=[],
            collaborator_handles=[],
        )
        self.assertFalse(ok)
        self.assertEqual(alts, [])

    def test_multiple_collaborators_listed(self) -> None:
        ok, alts = bpc.has_alternate_owners(
            operator_handle="sh-ai-x",
            codeowner_handles=["sh-ai-x"],
            collaborator_handles=["sh-ai-x", "alice", "bob"],
        )
        self.assertTrue(ok)
        self.assertEqual(sorted(alts), ["alice", "bob"])

    def test_codeowner_and_collaborator_lists_are_unioned(self) -> None:
        # CODEOWNERS says alice; collaborators say bob. Both are
        # alternates.
        ok, alts = bpc.has_alternate_owners(
            operator_handle="sh-ai-x",
            codeowner_handles=["sh-ai-x", "alice"],
            collaborator_handles=["sh-ai-x", "bob"],
        )
        self.assertTrue(ok)
        self.assertEqual(sorted(alts), ["alice", "bob"])


class TestFormatOwnershipConfirmedComment(unittest.TestCase):
    def test_default_shape(self) -> None:
        body = bpc.format_ownership_confirmed_comment(
            operator="sh-ai-x",
            rationale="trivial typo in docs",
            now_iso="2026-07-21T12:00:00Z",
        )
        self.assertEqual(
            body,
            "/ownership-confirmed by operator=sh-ai-x at 2026-07-21T12:00:00Z; "
            "rationale=trivial typo in docs",
        )

    def test_rationale_with_semicolons(self) -> None:
        body = bpc.format_ownership_confirmed_comment(
            operator="sh-ai-x",
            rationale="merge; do not; iterate",
            now_iso="2026-07-21T12:00:00Z",
        )
        # semicolons inside rationale are preserved verbatim -- the
        # audit reader parses only the first two separators.
        self.assertIn("rationale=merge; do not; iterate", body)


class TestLintLocalTestCmd(unittest.TestCase):
    """T26-T29: caller-side shell-meta lint for --local-test-cmd."""

    def test_empty_cmd_no_warnings(self) -> None:
        self.assertEqual(bpc.lint_local_test_cmd(""), [])

    def test_simple_cmd_no_warnings(self) -> None:
        self.assertEqual(bpc.lint_local_test_cmd("pytest -q"), [])

    def test_shell_meta_triggers_warning(self) -> None:
        # Each metachar yields its own one-line warning so the operator
        # sees the exact char, not a generic "looks dangerous".
        warnings = bpc.lint_local_test_cmd("pytest -q && rm -rf /tmp/foo")
        # `&` is in the metachar set; it surfaces as one warning per char.
        self.assertTrue(any("&" in w for w in warnings), warnings)

    def test_backtick_and_dollar_paren(self) -> None:
        warnings = bpc.lint_local_test_cmd("echo `date` $(whoami)")
        self.assertTrue(any("`" in w for w in warnings), warnings)
        self.assertTrue(any("$" in w for w in warnings), warnings)
        self.assertTrue(any("(" in w for w in warnings), warnings)
        self.assertTrue(any(")" in w for w in warnings), warnings)


class TestRunLocalVerify(unittest.TestCase):
    """T30-T34: run_local_verify enforcement (issue: --local-verify was
    prose-only; the gate is now real).

    Each test runs the helper in a fresh tmpdir so subprocess cwd is
    hermetic. The pytest-tail-line regex is the contract: exit 0 + no
    tail line = passed=False (MUST-L3 enforcement).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_empty_cmd_refuses(self) -> None:
        r = bpc.run_local_verify(cmd="", cwd=self.tmp)
        self.assertFalse(r.passed)
        self.assertEqual(r.exit_code, None)
        self.assertIn("empty", r.reason)

    def test_passing_command_with_tail_line_passes(self) -> None:
        # Bash one-liner that exits 0 and prints a pytest tail line.
        cmd = (
            'printf "%s\\n" "tests/test_x.py::test_a PASSED" "47 passed in 1.23s"; '
            "exit 0"
        )
        r = bpc.run_local_verify(cmd=cmd, cwd=self.tmp)
        self.assertTrue(r.passed, f"reason={r.reason!r} stderr={r.stderr!r}")
        self.assertEqual(r.exit_code, 0)
        self.assertIsNotNone(r.tail_line)
        self.assertIn("passed in", r.tail_line)

    def test_failing_command_refuses(self) -> None:
        cmd = 'printf "%s\\n" "1 failed in 0.50s"; exit 1'
        r = bpc.run_local_verify(cmd=cmd, cwd=self.tmp)
        self.assertFalse(r.passed)
        self.assertEqual(r.exit_code, 1)
        self.assertIn("exit 1", r.reason)

    def test_exit_zero_without_tail_line_refuses(self) -> None:
        """MUST-L3: a green command that does NOT actually run the test
        suite must NOT pass the gate. This is the regression guard for
        a forged tail line.
        """
        cmd = 'printf "%s\\n" "all good"; exit 0'
        r = bpc.run_local_verify(cmd=cmd, cwd=self.tmp)
        self.assertFalse(r.passed, "exit 0 + no tail line must refuse (MUST-L3)")
        self.assertEqual(r.exit_code, 0)
        self.assertIsNone(r.tail_line)
        self.assertIn("MUST-L3", r.reason)

    def test_timeout_refuses(self) -> None:
        # Sleep > exec_timeout_seconds with a no-output command.
        cmd = "sleep 5"
        r = bpc.run_local_verify(
            cmd=cmd,
            cwd=self.tmp,
            exec_timeout_seconds=1,
        )
        self.assertFalse(r.passed)
        self.assertTrue(r.timed_out)
        self.assertIn("timeout", r.reason)


class TestRunBabysitOnce(unittest.TestCase):
    """Pins the orchestrator's exit branches end-to-end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.codeowners = _write_codeowners(self.tmp, "*  @sh-ai-x\n")

    def _patch_io(self, captured: _CliResult):
        """Return a context manager that redirects the side-effect shims
        onto `captured`. Used inline so the tests stay independent.
        """
        return (
            patch.object(bpc, "_write_stdout", side_effect=captured.stdout.write),
            patch.object(bpc, "_write_stderr", side_effect=captured.stderr.write),
            patch.object(
                bpc,
                "_post_pr_comment",
                side_effect=lambda n, b: captured.commented.append((str(n), b)),
            ),
        )

    def _enter_io(self, captured: _CliResult):
        for cm in self._patch_io(captured):
            cm.start()
        self.addCleanup(lambda: tuple(cm.stop() for cm in self._patch_io(captured)))

    def test_default_behavior_is_human_gate(self) -> None:
        # No flag set -> the babysit must exit 0 with the existing
        # human-gate hand-off, no audit comment, no merge.
        captured = _CliResult()
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=[],
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=["sh-ai-x"],
                collaborator_lookup_ok=True,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OK)
        self.assertIn("human-gate", captured.stdout.getvalue())
        self.assertEqual(captured.commented, [])

    def test_flag_with_single_owner_confirms_ownership_no_merge(self) -> None:
        captured = _CliResult()
        argv = [
            "--operator-is-only-human",
            "--rationale", "single-operator merge of trivial docs fix",
        ]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=["sh-ai-x"],
                collaborator_lookup_ok=True,
                pr_number=42,
                now_iso="2026-07-21T12:00:00Z",
            )
        self.assertEqual(rc, bpc.EXIT_OK)
        # The audit comment is posted with the canonical format.
        self.assertEqual(len(captured.commented), 1)
        pr_no, body = captured.commented[0]
        self.assertEqual(pr_no, "42")
        self.assertTrue(body.startswith("/ownership-confirmed by operator=sh-ai-x"))
        self.assertIn("rationale=single-operator merge of trivial docs fix", body)
        # Auto-merge is disabled by policy -- there is no merge shim to
        # assert against; the hand-off message tells the operator to
        # merge manually.
        self.assertIn("merge this PR manually", captured.stdout.getvalue())

    def test_collaborator_lookup_failure_refuses_with_distinct_exit(self) -> None:
        """Fail-closed on the collaborators endpoint.

        The collaborators API call can fail (404, rate limit,
        permission error, empty stdout). When the skill reports
        collaborator_lookup_ok=False, the bypass refuses with
        EXIT_OWNERSHIP_UNKNOWN -- distinct from EXIT_MULTI_OWNER so
        the wrapper can distinguish 'no alternates found' from
        'endpoint unreachable / unknown ownership'.
        """
        captured = _CliResult()
        argv = ["--operator-is-only-human", "--rationale",
                "must refuse on collaborator outage"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=[],   # even if empty, lookup_ok=False refuses
                collaborator_lookup_ok=False,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OWNERSHIP_UNKNOWN,
                         "collaborator outage must refuse with "
                         "EXIT_OWNERSHIP_UNKNOWN")
        out = captured.stdout.getvalue()
        self.assertIn("did not return a confirmed success", out)
        self.assertIn("human-gate", out)
        # No comment posted -- unknown ownership never authorizes.
        self.assertEqual(captured.commented, [])

    def test_invalid_utf8_codeowners_fails_closed(self) -> None:
        """Invalid-UTF-8 CODEOWNERS must refuse the bypass, not
        raise UnicodeDecodeError out of the orchestrator.

        `parse_codeowners` reads the file with encoding='utf-8' which
        raises UnicodeDecodeError on invalid bytes. The helper
        converts that to OSError so the orchestrator's fail-closed
        handler catches it. Without this test, the UnicodeDecodeError
        would escape and crash the skill body.
        """
        captured = _CliResult()
        invalid = self.tmp / "BAD_UTF8"
        invalid.write_bytes(b"*  @sh-ai-x\n\xff\xfe invalid bytes")
        argv = ["--operator-is-only-human", "--rationale",
                "must refuse on invalid UTF-8 CODEOWNERS"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=invalid,
                collaborator_handles=["sh-ai-x"],
                collaborator_lookup_ok=True,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OWNERSHIP_UNKNOWN,
                         "invalid UTF-8 CODEOWNERS must refuse the "
                         "bypass with EXIT_OWNERSHIP_UNKNOWN")
        out = captured.stdout.getvalue()
        self.assertIn("could not read CODEOWNERS", out)
        self.assertEqual(captured.commented, [])

    def test_flag_with_multiple_owners_refuses(self) -> None:
        captured = _CliResult()
        multi = _write_codeowners(self.tmp, "*  @sh-ai-x @alice\n")
        argv = ["--operator-is-only-human", "--rationale", "should be refused"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=multi,
                collaborator_handles=["sh-ai-x"],
                collaborator_lookup_ok=True,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_MULTI_OWNER)
        # Print the alternate-owner list to stdout + a pointer to the
        # human-gate path. NO comment posted.
        out = captured.stdout.getvalue()
        self.assertIn("alice", out)
        self.assertIn("human-gate", out)
        self.assertEqual(captured.commented, [])

    def test_operator_absent_from_codeowners_fails_closed(self) -> None:
        """Positive-ownership confirmation: even with no alternates
        found, the operator must be explicitly listed in CODEOWNERS.
        An empty CODEOWNERS + empty collaborators list is *unknown*
        ownership, not *single* ownership.

        Regression for the security-sensitive fail-open: the
        collaborator-endpoint being down or returning an empty page
        must not authorize the bypass on a multi-operator repo.
        """
        captured = _CliResult()
        empty = self.tmp / "EMPTY"
        empty.write_text("", encoding="utf-8")
        argv = ["--operator-is-only-human", "--rationale",
                "operator not in CODEOWNERS - must refuse"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=empty,  # empty but readable
                collaborator_handles=[],  # endpoint succeeded, empty list
                collaborator_lookup_ok=True,  # positive signal that
                                              # the lookup succeeded
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OWNERSHIP_UNKNOWN,
                         "operator absent from CODEOWNERS + empty "
                         "collaborators must refuse the bypass")
        out = captured.stdout.getvalue()
        self.assertIn("not listed in CODEOWNERS", out)
        self.assertIn("human-gate", out)
        self.assertEqual(captured.commented, [])

    def test_empty_codeowners_fails_closed(self) -> None:
        """Empty-but-readable CODEOWNERS (file exists, zero rules)
        is the third axis of the ownership contract: distinct from
        unreadable (IO error, fail-closed via OSError) and from
        operator-absent (fail-closed via positive-ownership check).
        The orchestrator must refuse the bypass here too -- an empty
        CODEOWNERS file is not proof of single-operator ownership.
        """
        captured = _CliResult()
        empty = self.tmp / "EMPTY"
        empty.write_text("", encoding="utf-8")
        argv = ["--operator-is-only-human", "--rationale",
                "empty CODEOWNERS - must refuse"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=empty,
                collaborator_handles=[],
                collaborator_lookup_ok=False,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OWNERSHIP_UNKNOWN,
                         "empty CODEOWNERS + empty collaborators "
                         "must refuse the bypass")
        self.assertEqual(captured.commented, [])

    def test_missing_codeowners_fails_closed(self) -> None:
        """Fail-closed contract: an unreadable CODEOWNERS file must
        refuse the bypass rather than authorize the ownership
        confirmation.

        Regression for the security-sensitive bypass that the LLM
        review surfaced. An outage, permission error, or truncated
        first page CANNOT be interpreted as proof that no alternate
        human exists -- the bypass must always confirm ownership.
        """
        captured = _CliResult()
        argv = ["--operator-is-only-human", "--rationale",
                "must refuse on missing CODEOWNERS"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=self.tmp / "absent",  # never created
                collaborator_handles=["sh-ai-x"],
                collaborator_lookup_ok=True,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OWNERSHIP_UNKNOWN,
                         "missing CODEOWNERS must refuse the bypass")
        out = captured.stdout.getvalue()
        self.assertIn("could not read CODEOWNERS", out)
        self.assertIn("human-gate", out)
        # No comment posted -- the unknown-ownership state never
        # authorizes the bypass side-effects.
        self.assertEqual(captured.commented, [])

    def test_flag_without_rationale_is_rejected(self) -> None:
        # The rationale is the audit trail; without it the bypass must
        # be refused so the operator is forced to write a justification.
        captured = _CliResult()
        argv = ["--operator-is-only-human"]
        p_stdout, p_stderr, p_comment = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=["sh-ai-x"],
                collaborator_lookup_ok=True,
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_RATIONALE_REQUIRED)
        self.assertEqual(captured.commented, [])


if __name__ == "__main__":
    unittest.main()
