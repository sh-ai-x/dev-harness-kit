"""test_babysit_pr_cli.py -- unit tests for lib/babysit_pr_cli.py.

Pins the contract for the `--operator-is-only-human` opt-out (issue #324):

  parse_babysit_args(argv)
    T1: empty argv -> namespace with operator_is_only_human=False, rationale=""
    T2: "--operator-is-only-human" -> operator_is_only_human=True
    T3: "--rationale=..." sets rationale (must accompany the flag)
    T4: unknown flag -> SystemExit (argparse)

  parse_codeowners(path)
    T5: missing file -> []
    T6: typical CODEOWNERS file -> unique handles (no @, no comments)
    T7: capture group with @org/team-name -> "org/team-name"
    T8: dedup across multiple rules
    T9: email handles are ignored (CODEOWNERS user@domain format)

  has_alternate_owners(operator_handle, codeowner_handles,
                        collaborator_handles=())
    T10: single owner that matches operator -> (False, [])
    T11: operator + one other owner -> (True, ["other"])
    T12: operator + team handle -> (True, ["org/team-name"])
    T13: operator absent entirely -> (False, []) (no alternates known)
    T14: multiple collaborators listed -> (True, [...])
    T15: CODEOWNERS + collaborators are unioned

  format_bot_approve_comment(operator, rationale, now_iso)
    T16: emits "/bot-approve by operator=<handle> at <iso>; rationale=<text>"
    T17: rationale with semicolons is preserved verbatim

  run_babysit_once(...)
    T18: flag absent -> returns EXIT_OK (0) with human-gate hand-off
         message; never posts a comment; never calls gh pr merge.
    T19: flag present + single-owner -> returns EXIT_OK (0), posts the
         /bot-approve comment, schedules `gh pr merge --auto --squash`.
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
    contract: the human-gate hand-off message, the audit-comment body,
    and the merge command invocation.
    """

    def __init__(self) -> None:
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.commented: list[tuple[str, str]] = []  # (pr_number, body)
        self.merged: list[tuple[str, list[str]]] = []  # (pr_number, argv)


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

    def test_unknown_flag_exits(self) -> None:
        with self.assertRaises(SystemExit):
            bpc.parse_babysit_args(["--no-such-flag"])


class TestParseCodeowners(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_missing_returns_empty(self) -> None:
        self.assertEqual(bpc.parse_codeowners(self.tmp / "absent"), [])

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

    def test_operator_not_in_codeowners_is_allowed(self) -> None:
        # If the operator is not declared anywhere in CODEOWNERS, the
        # caller treats this as "no alternates known via CODEOWNERS";
        # gate decision falls to collaborators. With no collaborators
        # either, bypass is permitted.
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


class TestFormatBotApproveComment(unittest.TestCase):
    def test_default_shape(self) -> None:
        body = bpc.format_bot_approve_comment(
            operator="sh-ai-x",
            rationale="trivial typo in docs",
            now_iso="2026-07-21T12:00:00Z",
        )
        self.assertEqual(
            body,
            "/bot-approve by operator=sh-ai-x at 2026-07-21T12:00:00Z; "
            "rationale=trivial typo in docs",
        )

    def test_rationale_with_semicolons(self) -> None:
        body = bpc.format_bot_approve_comment(
            operator="sh-ai-x",
            rationale="merge; do not; iterate",
            now_iso="2026-07-21T12:00:00Z",
        )
        # semicolons inside rationale are preserved verbatim -- the
        # audit reader parses only the first two separators.
        self.assertIn("rationale=merge; do not; iterate", body)


class TestRunBabysitOnce(unittest.TestCase):
    """Pins the orchestrator's exit branches end-to-end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.codeowners = _write_codeowners(self.tmp, "*  @sh-ai-x\n")

    def _patch_io(self, captured: _CliResult):
        """Return a context manager that redirects the side-effect shims
        onto `captured`. Used inline so the four tests stay
        independent.
        """
        return (
            patch.object(bpc, "_write_stdout", side_effect=captured.stdout.write),
            patch.object(bpc, "_write_stderr", side_effect=captured.stderr.write),
            patch.object(
                bpc,
                "_post_pr_comment",
                side_effect=lambda n, b: captured.commented.append((str(n), b)),
            ),
            patch.object(
                bpc,
                "_run_pr_merge",
                side_effect=lambda n, a: captured.merged.append((str(n), list(a))),
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
        p_stdout, p_stderr, p_comment, p_merge = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment, p_merge:
            rc = bpc.run_babysit_once(
                argv=[],
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=["sh-ai-x"],
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_OK)
        self.assertIn("human-gate", captured.stdout.getvalue())
        self.assertEqual(captured.commented, [])
        self.assertEqual(captured.merged, [])

    def test_flag_with_single_owner_approves_and_merges(self) -> None:
        captured = _CliResult()
        argv = [
            "--operator-is-only-human",
            "--rationale", "single-operator merge of trivial docs fix",
        ]
        p_stdout, p_stderr, p_comment, p_merge = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment, p_merge:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=["sh-ai-x"],
                pr_number=42,
                now_iso="2026-07-21T12:00:00Z",
            )
        self.assertEqual(rc, bpc.EXIT_OK)
        # The audit comment is posted with the canonical format.
        self.assertEqual(len(captured.commented), 1)
        pr_no, body = captured.commented[0]
        self.assertEqual(pr_no, "42")
        self.assertTrue(body.startswith("/bot-approve by operator=sh-ai-x"))
        self.assertIn("rationale=single-operator merge of trivial docs fix", body)
        # gh pr merge --auto --squash scheduled with a numeric PR id.
        self.assertEqual(len(captured.merged), 1)
        merge_pr, merge_argv = captured.merged[0]
        self.assertEqual(merge_pr, "42")
        self.assertEqual(merge_argv, ["pr", "merge", "42", "--auto", "--squash"])

    def test_flag_with_multiple_owners_refuses(self) -> None:
        captured = _CliResult()
        multi = _write_codeowners(self.tmp, "*  @sh-ai-x @alice\n")
        argv = ["--operator-is-only-human", "--rationale", "should be refused"]
        p_stdout, p_stderr, p_comment, p_merge = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment, p_merge:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=multi,
                collaborator_handles=["sh-ai-x"],
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_MULTI_OWNER)
        # Print the alternate-owner list to stdout + a pointer to the
        # human-gate path. NO comment posted, NO merge scheduled.
        out = captured.stdout.getvalue()
        self.assertIn("alice", out)
        self.assertIn("human-gate", out)
        self.assertEqual(captured.commented, [])
        self.assertEqual(captured.merged, [])

    def test_flag_without_rationale_is_rejected(self) -> None:
        # The rationale is the audit trail; without it the bypass must
        # be refused so the operator is forced to write a justification.
        captured = _CliResult()
        argv = ["--operator-is-only-human"]
        p_stdout, p_stderr, p_comment, p_merge = self._patch_io(captured)
        with p_stdout, p_stderr, p_comment, p_merge:
            rc = bpc.run_babysit_once(
                argv=argv,
                operator_handle="sh-ai-x",
                codeowners_path=self.codeowners,
                collaborator_handles=["sh-ai-x"],
                pr_number=42,
            )
        self.assertEqual(rc, bpc.EXIT_RATIONALE_REQUIRED)
        self.assertEqual(captured.commented, [])
        self.assertEqual(captured.merged, [])


if __name__ == "__main__":
    unittest.main()
