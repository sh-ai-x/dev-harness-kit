#!/usr/bin/env python3
"""RED-first tests for lib/tdd_scope_judge (issue #647).

Covers:
- DEV_KIT_SKIP_TDD=1 short-circuits the judge subprocess
- DEV_KIT_BUILD_AGENT=codex routes the judge through `codex exec`
- Unknown agent fails closed
- Subprocess timeout still falls through to a recorded decision
- State file is written on every code path
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import tdd_scope_judge  # noqa: E402


class TestTddScopeJudgeSkipFlag(unittest.TestCase):
    """Issue #647 Option B: DEV_KIT_SKIP_TDD=1 escape hatch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_skip_tdd_short_circuits_subprocess(self):
        """DEV_KIT_SKIP_TDD=1 must NOT spawn a subprocess at all."""
        with patch.dict(os.environ, {"DEV_KIT_SKIP_TDD": "1"}):
            with patch.object(tdd_scope_judge.subprocess, "run") as mock_run:
                decision = tdd_scope_judge.evaluate("implement feature X", self.root)
        mock_run.assert_not_called()
        self.assertEqual(decision["tdd_required"], False)
        self.assertIn("DEV_KIT_SKIP_TDD", decision["reason"])

    def test_skip_tdd_truthy_values(self):
        """Any truthy string should bypass the judge."""
        for value in ("1", "true", "TRUE", "True", "yes"):
            with patch.dict(os.environ, {"DEV_KIT_SKIP_TDD": value}):
                with patch.object(tdd_scope_judge.subprocess, "run") as mock_run:
                    decision = tdd_scope_judge.evaluate("test", self.root)
                mock_run.assert_not_called()
                self.assertEqual(decision["tdd_required"], False)

    def test_skip_tdd_writes_state_file(self):
        """Bypass path must still write the state file so downstream readers see it."""
        with patch.dict(os.environ, {"DEV_KIT_SKIP_TDD": "1"}):
            tdd_scope_judge.evaluate("test", self.root)
        state = json.loads((self.root / ".dev-kit" / ".tdd-scope.json").read_text())
        self.assertEqual(state["tdd_required"], False)
        self.assertIn("DEV_KIT_SKIP_TDD", state["reason"])

    def test_skip_tdd_unset_invokes_subprocess(self):
        """Without DEV_KIT_SKIP_TDD, the subprocess must run as before."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEV_KIT_SKIP_TDD", None)
            with patch.object(tdd_scope_judge.subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout='{"tdd_required": true, "confidence": 0.9, "reason": "ok"}',
                    stderr="",
                )
                decision = tdd_scope_judge.evaluate("test", self.root)
        mock_run.assert_called_once()
        self.assertEqual(decision["tdd_required"], True)


class TestTddScopeJudgeAgentSelection(unittest.TestCase):
    """Issue #647 Option A: DEV_KIT_BUILD_AGENT=codex must route the judge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_with_mock(self, env: dict, stdout: str = '{"tdd_required": true, "confidence": 0.9, "reason": "ok"}'):
        with patch.dict(os.environ, env):
            with patch.object(tdd_scope_judge.subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
                tdd_scope_judge.evaluate("test", self.root)
            return mock_run.call_args[0][0]

    def test_default_agent_is_claude(self):
        env = {k: v for k, v in os.environ.items() if k != "DEV_KIT_BUILD_AGENT"}
        env.pop("DEV_KIT_BUILD_AGENT", None)
        cmd = self._run_with_mock(env)
        self.assertEqual(cmd[0], "claude")
        self.assertIn("-p", cmd)

    def test_codex_agent_routes_through_codex_exec(self):
        cmd = self._run_with_mock({"DEV_KIT_BUILD_AGENT": "codex"})
        self.assertEqual(cmd[0], "codex")
        self.assertEqual(cmd[1], "exec")
        self.assertNotIn("-p", cmd)

    def test_unknown_agent_fails_closed(self):
        """An unrecognized agent must raise rather than silently defaulting to claude."""
        with patch.dict(os.environ, {"DEV_KIT_BUILD_AGENT": "unknown"}):
            with self.assertRaises(ValueError) as ctx:
                tdd_scope_judge.evaluate("test", self.root)
        self.assertIn("DEV_KIT_BUILD_AGENT", str(ctx.exception))


class TestTddScopeJudgeFallback(unittest.TestCase):
    """Subprocess failure paths must still record a decision (existing contract)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_subprocess_timeout_records_decision(self):
        """TimeoutExpired must not crash the judge — the state file must still be written."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEV_KIT_SKIP_TDD", None)
            with patch.object(tdd_scope_judge.subprocess, "run") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=45)
                decision = tdd_scope_judge.evaluate("test", self.root)
        self.assertIn("judge unavailable", decision["reason"])
        # Decision file should exist regardless of timeout path.
        state = json.loads((self.root / ".dev-kit" / ".tdd-scope.json").read_text())
        self.assertEqual(state["reason"], decision["reason"])

    def test_skip_tdd_does_not_call_subprocess_on_timeout_path(self):
        """DEV_KIT_SKIP_TDD must short-circuit BEFORE subprocess.run is called."""
        with patch.dict(os.environ, {"DEV_KIT_SKIP_TDD": "1"}):
            with patch.object(tdd_scope_judge.subprocess, "run") as mock_run:
                # If subprocess.run were called, this side_effect would raise.
                mock_run.side_effect = AssertionError("subprocess must not run when SKIP_TDD is set")
                tdd_scope_judge.evaluate("test", self.root)
            mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
