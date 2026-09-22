#!/usr/bin/env python3
"""Truth-table tests for scoped DEV_KIT_GUARDS resolution."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
POLICY = ROOT / "hooks" / "lib" / "guard-policy.sh"


def _repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True,
                   capture_output=True)
    (root / ".claude").mkdir()


def _resolve(root: Path, env_value: str | None = None) -> tuple[str, str]:
    env = os.environ.copy()
    env["DEV_KIT_GUARD_ROOT"] = str(root)
    if env_value is None:
        env.pop("DEV_KIT_GUARDS", None)
    else:
        env["DEV_KIT_GUARDS"] = env_value
    cmd = (
        f'source "{POLICY}"; '
        'dev_kit_guards_resolve; '
        'printf "%s\\t%s\\n" "$DEV_KIT_GUARDS" "$DEV_KIT_GUARDS_SOURCE"'
    )
    result = subprocess.run(["bash", "-c", cmd], cwd=root, env=env,
                            capture_output=True, text=True, check=True)
    value, source = result.stdout.strip().split("\t")
    return value, source


class TestGuardPolicyResolution(unittest.TestCase):
    def test_default_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo(root)
            self.assertEqual(_resolve(root), ("off", "default"))

    def test_project_on_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo(root)
            (root / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "on"}})
            )
            self.assertEqual(_resolve(root), ("on", "project"))

    def test_local_wins_over_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo(root)
            (root / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "on"}})
            )
            (root / ".claude" / "settings.local.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "off"}})
            )
            self.assertEqual(_resolve(root), ("off", "local"))

    def test_shell_wins_over_local_and_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo(root)
            (root / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "off"}})
            )
            (root / ".claude" / "settings.local.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "off"}})
            )
            self.assertEqual(_resolve(root, "on"), ("on", "shell"))

    def test_invalid_local_falls_through_to_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo(root)
            (root / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "on"}})
            )
            (root / ".claude" / "settings.local.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "maybe"}})
            )
            result = _resolve(root)
            self.assertEqual(result, ("on", "project"))


if __name__ == "__main__":
    unittest.main()
