#!/usr/bin/env python3
"""test_dev_kit_team_cli.py — regression tests for bin/dev_kit_team.py.

The Python CLI must agree with the bash resolver (hooks/lib/team-resolve.sh)
on every case. The bash side is tested in test_team_resolution.py; this
file pins the Python side to the same outcomes so any drift in
bin/dev_kit_team.py (or its delegates) is caught by CI.

Test matrix mirrors test_team_resolution.py:
  - 3 layer-1 (shell-env wins)
  - 3 layer-2 (project wins, including truthy normalization)
  - 3 layer-3 (local kicks in when project unset)
  - 2 precedence (project > local)
  - 2 conditional default (silent off)
  - 1 outside-git
  - 2 invalid-value fallthrough
  - write --on / --off round-trip
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
CLI = REPO_ROOT / "bin" / "dev_kit_team.py"


def _make_proj(tmp: Path, *, project_team: str | None,
               local_team: str | None) -> Path:
    proj = tmp / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    if project_team is not None:
        body = {"env": {"DEV_KIT_TEAM": project_team}}
        (proj / ".claude" / "settings.json").write_text(json.dumps(body))
    if local_team is not None:
        (proj / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"DEV_KIT_TEAM": local_team}})
        )
    return proj


def _run_cli(proj: Path, *, env_override: dict | None = None) -> str:
    """Run `dev_kit_team.py resolve` and return the printed value."""
    env = os.environ.copy()
    env.pop("DEV_KIT_TEAM", None)  # baseline
    if env_override:
        env.update(env_override)
    result = subprocess.run(
        [sys.executable, str(CLI), "--target", str(proj), "resolve"],
        capture_output=True, text=True, timeout=10,
        cwd=str(proj), env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"CLI exited {result.returncode}\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout.strip()


def _run_cli_show(proj: Path, *, env_override: dict | None = None) -> str:
    env = os.environ.copy()
    env.pop("DEV_KIT_TEAM", None)
    if env_override:
        env.update(env_override)
    result = subprocess.run(
        [sys.executable, str(CLI), "--target", str(proj), "show"],
        capture_output=True, text=True, timeout=10,
        cwd=str(proj), env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"CLI exited {result.returncode}\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout.strip()


def _run_cli_write(proj: Path, *, state: str, scope: str = "project"):
    """Run `dev_kit_team.py write --on|--off` and return CompletedProcess."""
    env = os.environ.copy()
    env.pop("DEV_KIT_TEAM", None)
    flag = "--on" if state == "on" else "--off"
    return subprocess.run(
        [sys.executable, str(CLI), "--target", str(proj), "write",
         flag, "--scope", scope],
        capture_output=True, text=True, env=env, timeout=10,
    )


class TestDevKitTeamCLI(unittest.TestCase):
    """The Python CLI must agree with team-resolve.sh on every case."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- Layer 1: shell env wins -----

    def test_shell_env_on_overrides_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_TEAM": "on"}), "on")

    def test_shell_env_off_overrides_project_on(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_TEAM": "off"}), "off")

    def test_shell_env_on_overrides_project_on_and_local_off(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team="off")
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_TEAM": "on"}), "on")

    # ----- Layer 2: project-scope wins when shell env is unset -----

    def test_project_on_when_unset(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        self.assertEqual(_run_cli(proj), "on")

    def test_project_off_when_unset(self):
        proj = _make_proj(Path(self.tmp), project_team="off", local_team=None)
        self.assertEqual(_run_cli(proj), "off")

    def test_project_truthy_normalizes_to_on(self):
        proj = _make_proj(Path(self.tmp), project_team="1", local_team=None)
        self.assertEqual(_run_cli(proj), "on")

    # ----- Layer 3: local-scope kicks in when project-scope is unset -----

    def test_local_on_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team="on")
        self.assertEqual(_run_cli(proj), "on")

    def test_local_off_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team="off")
        self.assertEqual(_run_cli(proj), "off")

    def test_local_true_normalizes_to_on(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team="true")
        self.assertEqual(_run_cli(proj), "on")

    # ----- Layer 2 precedence over Layer 3 -----

    def test_project_on_wins_over_local_off(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team="off")
        self.assertEqual(_run_cli(proj), "on")

    def test_project_off_wins_over_local_on(self):
        proj = _make_proj(Path(self.tmp), project_team="off", local_team="on")
        self.assertEqual(_run_cli(proj), "off")

    # ----- Layer 4: silent default = off -----

    def test_default_off_when_no_settings_at_all(self):
        proj = Path(self.tmp) / "bare"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
        self.assertEqual(_run_cli(proj), "off")

    def test_default_off_when_settings_have_no_team_key(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        self.assertEqual(_run_cli(proj), "off")

    # ----- Outside any git repo -----

    def test_outside_git_repo_returns_off(self):
        non_git = Path(self.tmp) / "no-git"
        non_git.mkdir()
        self.assertEqual(_run_cli(non_git), "off")

    # ----- Value validation -----

    def test_invalid_shell_value_falls_through(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        # Python CLI fallback whitelists {on,1,true,off,0,false}; "maybe"
        # falls through to bash, which warns + ignores it.
        self.assertEqual(_run_cli(proj, env_override={"DEV_KIT_TEAM": "maybe"}), "on")

    # ----- write --on / --off round-trip -----

    def test_cli_write_on_writes_settings(self):
        """`bin/dev_kit_team.py write --on` writes DEV_KIT_TEAM=1 to
        settings.json (project scope)."""
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        result = _run_cli_write(proj, state="on")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        body = json.loads((proj / ".claude" / "settings.json").read_text())
        self.assertEqual(body["env"]["DEV_KIT_TEAM"], "1")

    def test_cli_write_off_removes_settings_key(self):
        """`bin/dev_kit_team.py write --off` removes DEV_KIT_TEAM entirely
        (silent default = off)."""
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        result = _run_cli_write(proj, state="off")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        body = json.loads((proj / ".claude" / "settings.json").read_text())
        self.assertNotIn("DEV_KIT_TEAM", body.get("env", {}))

    def test_cli_write_on_local_scope_writes_settings_local(self):
        """`--scope local` writes to settings.local.json (gitignored)."""
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        result = _run_cli_write(proj, state="on", scope="local")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        local_body = json.loads((proj / ".claude" / "settings.local.json").read_text())
        self.assertEqual(local_body["env"]["DEV_KIT_TEAM"], "1")
        # Project-scope file should NOT carry the key.
        if (proj / ".claude" / "settings.json").exists():
            proj_body = json.loads((proj / ".claude" / "settings.json").read_text())
            self.assertNotIn("DEV_KIT_TEAM", proj_body.get("env", {}))

    def test_cli_show_reports_off_by_default(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        out = _run_cli_show(proj)
        self.assertIn("OFF", out)
        self.assertIn("default", out)

    def test_cli_show_reports_on_with_source(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        out = _run_cli_show(proj)
        self.assertIn("ON", out)
        self.assertIn("project", out)

    # ----- Orthogonality: team CLI is independent of mode -----

    def test_team_write_does_not_disturb_mode_key(self):
        """Writing team=on must not delete or alter an existing
        DEV_KIT_MODE=full in the same env block."""
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        # Pre-populate the mode key.
        (proj / ".claude" / "settings.json").write_text(json.dumps({
            "env": {"DEV_KIT_MODE": "full"},
        }))
        result = _run_cli_write(proj, state="on")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        body = json.loads((proj / ".claude" / "settings.json").read_text())
        self.assertEqual(body["env"]["DEV_KIT_MODE"], "full")
        self.assertEqual(body["env"]["DEV_KIT_TEAM"], "1")


if __name__ == "__main__":
    unittest.main()
