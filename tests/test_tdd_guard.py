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
            # Strip harness-controlled vars so the hook starts from the
            # unconfigured default; the user's shell may have
            # DEV_KIT_GUARDS=on set globally.
            clean = {
                k: v for k, v in os.environ.items()
                if k not in {"DEV_KIT_GUARDS", "DEV_KIT_GUARDS_SOURCE",
                             "DEV_KIT_GUARD_ROOT"}
                and not k.startswith("ANTHROPIC_")
            }
            env = {**clean, "DEV_KIT_TDD_ROOT": str(root)}
            result = subprocess.run(["bash", str(ROOT / "hooks/tdd-guard.sh")], cwd=root,
                input=json.dumps({"tool_input": {"file_path": str(root / "lib/core.py")}}),
                text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)


class TestTddGuardUnknownPaths(unittest.TestCase):
    def _run(self, root: Path) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "DEV_KIT_GUARDS": "on",
            "DEV_KIT_TDD_ROOT": str(root),
            "CLAUDE_PROJECT_DIR": str(root),
        }
        return subprocess.run(
            ["bash", str(ROOT / "hooks/tdd-guard.sh")],
            cwd=root,
            input=json.dumps({"tool_input": {"file_path": str(root / "src/feature.py")}}),
            text=True,
            capture_output=True,
            env=env,
        )

    def test_unknown_code_path_requires_red_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp))
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_explicit_scope_exemption_allows_unknown_code_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".dev-kit" / ".tdd-scope.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"tdd_required": False}), encoding="utf-8")
            result = self._run(root)
            self.assertEqual(result.returncode, 0, result.stderr)
