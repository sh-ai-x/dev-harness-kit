from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent


class TestTddGuard(unittest.TestCase):
    def test_maintenance_files_are_not_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "DEV_KIT_TDD_ROOT": str(root)}
            result = subprocess.run(["bash", str(ROOT / "hooks/tdd-guard.sh")], cwd=root,
                input=json.dumps({"tool_input": {"file_path": str(root / "tools/one_off.py")}}),
                text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_edit_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "DEV_KIT_TDD_ROOT": str(root)}
            result = subprocess.run(["bash", str(ROOT / "hooks/tdd-guard.sh")], cwd=root,
                input=json.dumps({"tool_input": {"file_path": str(root / "lib/core.py")}}),
                text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)


class TestTddScopeJudgeRoot(unittest.TestCase):
    """tdd-scope-judge.sh must record its decision under DEV_KIT_TDD_ROOT.

    tdd-guard.sh:25-26 resolves the state path via
    ``${DEV_KIT_TDD_ROOT:-$(git rev-parse ...)}``. If the judge resolves
    the root differently (plain git toplevel), a decision written when
    DEV_KIT_TDD_ROOT is set lands at the toplevel, the guard never sees
    ``tdd_required=false``, and the next core-code edit is
    false-denied for missing RED evidence.
    """

    def _run_judge(self, root: Path, cwd: Path) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "DEV_KIT_TDD_ROOT": str(root),
            "DEV_KIT_SKIP_TDD": "1",  # deterministic: no subprocess, writes tdd_required=false
        }
        return subprocess.run(
            ["bash", str(ROOT / "hooks/tdd-scope-judge.sh")],
            cwd=cwd,
            input=json.dumps({"prompt": "implement feature X"}),
            text=True, capture_output=True, env=env,
        )

    def test_judge_writes_state_under_dev_kit_tdd_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # cwd is the real repo (a git toplevel) so the old
            # git-only resolution would write to the wrong place.
            result = self._run_judge(root, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            state_path = root / ".dev-kit" / ".tdd-scope.json"
            self.assertTrue(
                state_path.exists(),
                f"judge must write state under DEV_KIT_TDD_ROOT, not git toplevel "
                f"(stderr={result.stderr!r})",
            )
            state = json.loads(state_path.read_text())
            self.assertFalse(state["tdd_required"])

    def test_guard_obeys_judge_decision_written_under_dev_kit_tdd_root(self):
        """End-to-end: judge records tdd_required=false under
        DEV_KIT_TDD_ROOT, then tdd-guard.sh must NOT deny a lib/ edit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._run_judge(root, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = {**os.environ, "DEV_KIT_TDD_ROOT": str(root)}
            guard = subprocess.run(
                ["bash", str(ROOT / "hooks/tdd-guard.sh")], cwd=root,
                input=json.dumps({"tool_input": {"file_path": str(root / "lib/core.py")}}),
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(guard.returncode, 0, guard.stderr)
