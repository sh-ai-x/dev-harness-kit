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

    def test_core_edit_requires_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "DEV_KIT_TDD_ROOT": str(root)}
            result = subprocess.run(["bash", str(ROOT / "hooks/tdd-guard.sh")], cwd=root,
                input=json.dumps({"tool_input": {"file_path": str(root / "lib/core.py")}}),
                text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
