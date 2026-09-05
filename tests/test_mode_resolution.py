#!/usr/bin/env python3
"""test_mode_resolution.py — DEV_KIT_MODE resolution order regression tests.

Pins the resolution order documented in docs/scopes/modes.md:

  1. $DEV_KIT_MODE shell env var      — wins over everything (per-session)
  2. <proj>/.claude/settings.json    — committed project choice
  3. <proj>/.claude/settings.local.json — personal override (this checkout)
  4. Default = "full" ONLY when enabledPlugins.dev-kit@dev-kit: true;
     otherwise "undev" (silent — plugin not loaded)

Nine cases (3 explicit × 3 scope levels). Each runs in a temp git repo
with synthetic `.claude/settings*.json` so we can assert precedence
without depending on the host filesystem.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MODE_LIB = REPO_ROOT / "hooks" / "lib" / "mode-resolve.sh"


def _resolve(cwd: Path, env_override: dict | None = None) -> str:
    """Run dev_kit_mode_resolve in a child shell with the given env."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    env.pop("DEV_KIT_MODE", None)  # ensure clean baseline unless overridden
    if env_override:
        env.update(env_override)
    script = f"""
      source "{MODE_LIB}"
      dev_kit_mode_resolve
      printf '%s' "$DEV_KIT_MODE"
    """
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=10,
        cwd=str(cwd), env=env,
    )
    return result.stdout.strip()


def _make_proj(tmp: Path, *, project_mode: str | None, local_mode: str | None,
               enabled_plugins: dict | None) -> Path:
    """Build a synthetic project root with .git and .claude/."""
    proj = tmp / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    if project_mode is not None or enabled_plugins is not None:
        body = {}
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


class TestModeResolution(unittest.TestCase):
    """3 explicit-mode sources × 3 project-scope configs = 9 cases."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- Layer 1: shell env wins -----

    def test_shell_env_full_overrides_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj, {"DEV_KIT_MODE": "full"}), "full")

    def test_shell_env_lite_overrides_project_full(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj, {"DEV_KIT_MODE": "lite"}), "lite")

    def test_shell_env_undev_overrides_project_full_and_local_lite(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode="lite", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj, {"DEV_KIT_MODE": "undev"}), "undev")

    # ----- Layer 2: project-scope wins when shell env is unset -----

    def test_project_full_when_plugin_enabled(self):
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "full")

    def test_project_lite_when_plugin_enabled(self):
        proj = _make_proj(Path(self.tmp), project_mode="lite",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "lite")

    def test_project_undev_when_plugin_enabled(self):
        proj = _make_proj(Path(self.tmp), project_mode="undev",
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "undev")

    # ----- Layer 3: local-scope kicks in when project-scope is unset -----

    def test_local_full_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode="full", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "full")

    def test_local_lite_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode="lite", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "lite")

    def test_local_undev_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode="undev", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "undev")

    # ----- Layer 2 precedence over Layer 3: project-scope wins when set -----

    def test_project_full_wins_over_local_lite(self):
        """Project scope is team-committed; personal local override does
        NOT override a team decision. The user can still pin mode via the
        shell env var (Layer 1) for a one-session override."""
        proj = _make_proj(Path(self.tmp), project_mode="full",
                          local_mode="lite", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "full")

    def test_project_undev_wins_over_local_full(self):
        proj = _make_proj(Path(self.tmp), project_mode="undev",
                          local_mode="full", enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "undev")

    # ----- Layer 4: conditional default -----

    def test_default_full_when_plugin_enabled_no_mode_set(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode=None, enabled_plugins={"dev-kit@dev-kit": True})
        self.assertEqual(_resolve(proj), "full")

    def test_default_undev_when_plugin_not_enabled_no_mode_set(self):
        proj = _make_proj(Path(self.tmp), project_mode=None,
                          local_mode=None, enabled_plugins={})
        self.assertEqual(_resolve(proj), "undev")

    def test_default_undev_when_no_settings_file_at_all(self):
        proj = Path(self.tmp) / "bare"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
        self.assertEqual(_resolve(proj), "undev")

    # ----- Outside any git repo -----

    def test_outside_git_repo_returns_undev(self):
        non_git = Path(self.tmp) / "no-git"
        non_git.mkdir()
        self.assertEqual(_resolve(non_git), "undev")


if __name__ == "__main__":
    unittest.main()
