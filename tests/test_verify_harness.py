#!/usr/bin/env python3
"""
test_verify_harness.py — RED-first tests for lib/verify_harness.py (Tier 0).

Covers:
- parse_verification: index.json field precedence over step.md fenced block
- parse_verification: fenced-block extraction, empty-when-undeclared
- run_verification: no shell=True, exit code capture, pytest count regex
- verification_signature: stability + change-on-different-failure
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import verify_harness  # noqa: E402


class TestParseVerification(unittest.TestCase):
    def test_index_json_field_wins_over_fenced_block_string(self):
        step_meta = {"step": 1, "verification": "pytest tests/test_foo.py -q"}
        step_md = (
            "## Verification & Status Update\n\n"
            "```bash\npytest tests/test_bar.py -q\n```\n"
        )
        self.assertEqual(
            verify_harness.parse_verification(step_meta, step_md),
            ["pytest tests/test_foo.py -q"],
        )

    def test_index_json_field_list_returned_as_is(self):
        step_meta = {"step": 1, "verification": ["pytest tests/test_foo.py -q", "ruff check lib/foo.py"]}
        self.assertEqual(
            verify_harness.parse_verification(step_meta, ""),
            ["pytest tests/test_foo.py -q", "ruff check lib/foo.py"],
        )

    def test_fenced_block_extraction_when_no_index_field(self):
        step_meta = {"step": 1}
        step_md = (
            "# Step 1\n\nSome prose.\n\n"
            "## Verification & Status Update\n\n"
            "```bash\n"
            "pytest tests/test_foo.py -q\n"
            "ruff check lib/foo.py\n"
            "```\n"
        )
        self.assertEqual(
            verify_harness.parse_verification(step_meta, step_md),
            ["pytest tests/test_foo.py -q", "ruff check lib/foo.py"],
        )

    def test_empty_when_undeclared(self):
        step_meta = {"step": 1}
        step_md = "# Step 1\n\nNo verification section here.\n"
        self.assertEqual(verify_harness.parse_verification(step_meta, step_md), [])

    def test_empty_verification_field_falls_back_to_fenced_block(self):
        step_meta = {"step": 1, "verification": ""}
        step_md = (
            "## Verification & Status Update\n\n```bash\npytest -q\n```\n"
        )
        self.assertEqual(verify_harness.parse_verification(step_meta, step_md), ["pytest -q"])

    def test_fenced_block_ignores_blank_and_comment_lines(self):
        step_meta = {"step": 1}
        step_md = (
            "## Verification & Status Update\n\n"
            "```bash\n"
            "# run the suite\n"
            "\n"
            "pytest -q\n"
            "```\n"
        )
        self.assertEqual(verify_harness.parse_verification(step_meta, step_md), ["pytest -q"])

    def test_fenced_block_does_not_leak_into_a_later_unrelated_section(self):
        # F1: the Verification section itself has no fence; a LATER section
        # ("Don't", per the step.md template order) does. The search must
        # not wander past the section boundary and adopt that unrelated
        # fence's contents as verification commands.
        step_meta = {"step": 1}
        step_md = (
            "## Verification & Status Update\n\n"
            "<!-- status: pending -->\n\n"
            "## Don't\n\n"
            "Never run a destructive command, e.g.:\n\n"
            "```bash\nrm -rf /\necho pwned\n```\n"
        )
        self.assertEqual(verify_harness.parse_verification(step_meta, step_md), [])

    def test_list_field_filters_blank_entries(self):
        # F3: an authoring mistake (stray blank entry) must not survive
        # into a command list that run_verification will execute.
        step_meta = {"step": 1, "verification": ["", "pytest -q", "  "]}
        self.assertEqual(verify_harness.parse_verification(step_meta, ""), ["pytest -q"])

    def test_fenced_block_strips_inline_trailing_comment(self):
        # F4: an inline "# comment" after a real command must not become
        # bogus extra argv tokens once shlex.split runs on it.
        step_meta = {"step": 1}
        step_md = (
            "## Verification & Status Update\n\n"
            "```bash\npytest -q  # smoke test\n```\n"
        )
        self.assertEqual(verify_harness.parse_verification(step_meta, step_md), ["pytest -q"])


class TestRunVerification(unittest.TestCase):
    def test_all_pass_ok_true(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=0, stdout="2 passed in 0.05s", stderr="")
        result = verify_harness.run_verification(
            ["pytest tests/test_foo.py -q"], cwd=Path("/tmp"), runner=fake
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.results[0].exit_code, 0)
        self.assertEqual(result.results[0].tests_passed, 2)
        self.assertIsNone(result.results[0].tests_failed)

    def test_failure_sets_ok_false_and_extracts_counts(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=1, stdout="2 passed, 1 failed in 0.05s", stderr="")
        result = verify_harness.run_verification(
            ["pytest tests/test_foo.py -q"], cwd=Path("/tmp"), runner=fake
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.results[0].exit_code, 1)
        self.assertEqual(result.results[0].tests_passed, 2)
        self.assertEqual(result.results[0].tests_failed, 1)

    def test_runs_every_command_no_shell_true(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        verify_harness.run_verification(
            ["pytest -q", "ruff check lib/foo.py"], cwd=Path("/tmp"), runner=fake
        )
        self.assertEqual(fake.call_count, 2)
        for call in fake.call_args_list:
            args, kwargs = call
            # argv form (list), never a shell string, and shell is never True.
            self.assertIsInstance(args[0], list)
            self.assertNotIn("shell", kwargs)

    def test_one_failure_among_many_sets_ok_false(self):
        fake = MagicMock()
        fake.side_effect = [
            MagicMock(returncode=0, stdout="1 passed", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="boom"),
        ]
        result = verify_harness.run_verification(
            ["pytest -q", "ruff check lib/foo.py"], cwd=Path("/tmp"), runner=fake
        )
        self.assertFalse(result.ok)
        self.assertEqual(len(result.results), 2)

    def test_empty_commands_ok_true_no_calls(self):
        fake = MagicMock()
        result = verify_harness.run_verification([], cwd=Path("/tmp"), runner=fake)
        self.assertTrue(result.ok)
        self.assertEqual(result.results, ())
        fake.assert_not_called()

    def test_missing_executable_produces_failed_result_not_crash(self):
        # F2: a missing/misspelled binary must not abort the whole run —
        # every declared command still gets a CommandResult, per the
        # function's own "no early exit" contract.
        fake = MagicMock()
        fake.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'nope'")
        result = verify_harness.run_verification(["nope --flag"], cwd=Path("/tmp"), runner=fake)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.results), 1)
        self.assertNotEqual(result.results[0].exit_code, 0)
        self.assertIn("nope", result.results[0].tail)

    def test_missing_executable_does_not_abort_remaining_commands(self):
        fake = MagicMock()
        fake.side_effect = [
            FileNotFoundError("[Errno 2] No such file or directory: 'nope'"),
            MagicMock(returncode=0, stdout="1 passed", stderr=""),
        ]
        result = verify_harness.run_verification(
            ["nope --flag", "pytest -q"], cwd=Path("/tmp"), runner=fake
        )
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.results[1].exit_code, 0)

    def test_timeout_produces_failed_result_with_sentinel_exit_code(self):
        # F5: a hanging command must not block the harness forever —
        # subprocess.TimeoutExpired is caught and surfaced as evidence.
        fake = MagicMock()
        fake.side_effect = subprocess.TimeoutExpired(cmd=["pytest", "-q"], timeout=1)
        result = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        self.assertFalse(result.ok)
        self.assertEqual(result.results[0].exit_code, 124)
        self.assertIn("timed out", result.results[0].tail.lower())

    def test_timeout_kwarg_is_passed_to_runner(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        _args, kwargs = fake.call_args
        self.assertIn("timeout", kwargs)
        self.assertIsInstance(kwargs["timeout"], (int, float))

    def test_tail_redacts_known_secret_patterns(self):
        # F6: raw command output must not carry secret-shaped strings
        # verbatim into evidence that later gets persisted/rendered.
        fake = MagicMock()
        fake.return_value = MagicMock(
            returncode=1,
            stdout="leaked token sk-ant-" + "abcdefghijklmnopqrstuvwxyzABCDEFGH12345",
            stderr="",
        )
        result = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        self.assertNotIn("sk-ant-" + "abcdefghijklmnopqrstuvwxyzABCDEFGH12345", result.results[0].tail)
        self.assertIn("[REDACTED]", result.results[0].tail)


class TestVerificationSignature(unittest.TestCase):
    def test_stable_across_calls(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        result = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        sig1 = verify_harness.verification_signature(result)
        sig2 = verify_harness.verification_signature(result)
        self.assertEqual(sig1, sig2)

    def test_changes_on_different_failing_command(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        result_a = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        result_b = verify_harness.run_verification(["ruff check lib/foo.py"], cwd=Path("/tmp"), runner=fake)
        self.assertNotEqual(
            verify_harness.verification_signature(result_a),
            verify_harness.verification_signature(result_b),
        )

    def test_empty_signature_for_ok_result(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
        result = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        self.assertTrue(result.ok)
        # Deterministic even though there's nothing to sign — no crash, stable value.
        sig1 = verify_harness.verification_signature(result)
        sig2 = verify_harness.verification_signature(result)
        self.assertEqual(sig1, sig2)

    def test_changes_with_different_failure_counts_same_command(self):
        # F8: real progress (3 failed -> 1 failed on the same command,
        # same nonzero exit code) must not hash to the same signature, or
        # PR 3's no-progress guard would misjudge convergence as stuck.
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=1, stdout="2 passed, 3 failed", stderr="")
        result_a = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        fake.return_value = MagicMock(returncode=1, stdout="4 passed, 1 failed", stderr="")
        result_b = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        self.assertNotEqual(
            verify_harness.verification_signature(result_a),
            verify_harness.verification_signature(result_b),
        )


class TestSuccessfulChecks(unittest.TestCase):
    def test_counts_passing_commands(self):
        fake = MagicMock()
        fake.side_effect = [
            MagicMock(returncode=0, stdout="1 passed", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="boom"),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        result = verify_harness.run_verification(
            ["pytest -q", "ruff check lib/foo.py", "mypy lib/foo.py"],
            cwd=Path("/tmp"),
            runner=fake,
        )
        self.assertEqual(verify_harness.successful_checks(result), 2)

    def test_zero_for_all_failing(self):
        fake = MagicMock()
        fake.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        result = verify_harness.run_verification(["pytest -q"], cwd=Path("/tmp"), runner=fake)
        self.assertEqual(verify_harness.successful_checks(result), 0)


if __name__ == "__main__":
    unittest.main()
