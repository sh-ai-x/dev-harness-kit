#!/usr/bin/env python3
"""test_dev_kit_mode_cli.py — regression tests for bin/dev_kit_mode.py.

The Python CLI must agree with the bash resolver (hooks/lib/mode-resolve.sh)
on every case. The bash side is tested in test_mode_resolution.py; this
file pins the Python side to the same 15 outcomes so any drift in
bin/dev_kit_mode.py (or its delegates) is caught by CI.

Test matrix mirrors test_mode_resolution.py:
  - 3 layer-1 (shell-env wins)
  - 3 layer-2 (project wins)
  - 3 layer-3 (local kicks in when project unset)
  - 2 precedence (project > local)
  - 3 conditional default
  - 1 outside-git
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CLI = REPO_ROOT / "bin" / "dev_kit_mode.py"


def _make_proj(tmp: Path, *, project_mode: str | None, local_mode: str | None,
              enabled_plugins: dict | None) -> Path:
    proj = tmp / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    if project_mode is not None or enabled_plugins is not None:
        body: dict = {}
        if enabled_plugins is not None:
            body["enabledPlugins"] = enabled_plugins
        if project_mode is not None:
            body["env"] = {"DEV_KIT_MODE": project_mode}
        (proj / ".claude" / "settings.json").write_text(json.dumps(body))
    if local_mode is not None:
        (proj / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"DEV_KIT_MODE": local_mode}})
        )
    return proj


def _run_cli(proj: Path, *, env_override: dict | None = None) -> str:
    """Run `dev_kit_mode.py resolve` and return the printed mode."""
    env = os.environ.copy()
    env.pop("DEV_KIT_MODE", None)  # baseline
    if env_override:
        env.update(env_override)
    result = subprocess.run(
        [sys.executable, str(CLI), "resolve"],
        capture_output=True, text=True, timeout=10,
        cwd=str(proj), env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"CLI exited {result.returncode}\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout.strip()


class TestDevKitModeCLI(unittest.TestCase):
    """The Python CLI must agree with mode-resolve.sh on every case."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- Layer 1: shell env wins -----

    def test_shell_env_full_overrides_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_MODE": "full"}), "full")

    def test_shell_env_lite_overrides_project_full(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_MODE": "lite"}), "lite")

    def test_shell_env_undev_overrides_project_full_and_local_lite(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode="lite", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_MODE": "undev"}), "undev")

    # ----- Layer 2: project-scope wins when shell env is unset -----

    def test_project_full_when_plugin_enabled(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "full")

    def test_project_lite_when_plugin_enabled(self):
        proj = _make_proj(Path(self.tmp), project_mode="lite",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "lite")

    def test_project_undev_when_plugin_enabled(self):
        proj = _make_proj(Path(self.tmp), project_mode="undev",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "undev")

    # ----- Layer 3: local-scope kicks in when project-scope is unset -----

    def test_local_full_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode="full", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "full")

    def test_local_lite_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode="lite", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "lite")

    def test_local_undev_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode="undev", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "undev")

    # ----- Layer 2 precedence over Layer 3: project-scope wins when set -----

    def test_project_full_wins_over_local_lite(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode="lite", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "full")

    def test_project_undev_wins_over_local_full(self):
        proj = _make_proj(Path(self.tmp), project_mode="undev",
                          local_mode="full", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "undev")

    # ----- Layer 4: conditional default -----

    def test_default_full_when_plugin_enabled_no_mode_set(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_run_cli(proj), "full")

    def test_default_undev_when_plugin_not_enabled_no_mode_set(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode=None, enabled_plugins={})
        self.assertEqual(_run_cli(proj), "undev")

    def test_default_undev_when_no_settings_file_at_all(self):
        proj = Path(self.tmp) / "bare"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
        self.assertEqual(_run_cli(proj), "undev")

    # ----- Outside any git repo -----

    def test_outside_git_repo_returns_undev(self):
        non_git = Path(self.tmp) / "no-git"
        non_git.mkdir()
        self.assertEqual(_run_cli(non_git), "undev")


if __name__ == "__main__":
    unittest.main()
