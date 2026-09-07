#!/usr/bin/env python3
"""test_team_resolution.py — DEV_KIT_TEAM resolution order regression tests.

Pins the resolution order documented in docs/scopes/modes.md and
skills/team/SKILL.md:

  1. $DEV_KIT_TEAM shell env var          — wins over everything (per-session)
  2. <proj>/.claude/settings.json        — committed project choice
  3. <proj>/.claude/settings.local.json  — personal override (this checkout)
  4. Default = "off" — silent (team toggle is opt-in)

Covers all 4 resolution layers × on/off × 2 scope layers plus invalid-
value fallthrough and outside-git. Each runs in a temp git repo with
synthetic `.claude/settings*.json` so we can assert precedence without
depending on the host filesystem.
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
TEAM_LIB = REPO_ROOT / "hooks" / "lib" / "team-resolve.sh"


def _resolve(cwd: Path, env_override: dict | None = None) -> str:
    """Run dev_kit_team_resolve in a child shell with the given env."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    env.pop("DEV_KIT_TEAM", None)  # ensure clean baseline unless overridden
    if env_override:
        env.update(env_override)
    script = f"""
      source "{TEAM_LIB}"
      dev_kit_team_resolve
      printf '%s' "$DEV_KIT_TEAM"
    """
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=10,
        cwd=str(cwd), env=env,
    )
    return result.stdout.strip()


def _make_proj(tmp: Path, *, project_team: str | None,
               local_team: str | None) -> Path:
    """Build a synthetic project root with .git and .claude/.

    Note: team-toggle resolution is independent of enabledPlugins; the
    plugin-enabled check that gates DEV_KIT_MODE Layer 4 does not apply
    here. team=on/off works regardless of whether dev-kit@dev-kit is
    enabled at project scope.
    """
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


class TestTeamResolution(unittest.TestCase):
    """4 layers × on/off × 2 scopes + invalid + outside-git."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- Layer 1: shell env wins -----

    def test_shell_env_on_overrides_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        self.assertEqual(_resolve(proj, {"DEV_KIT_TEAM": "on"}), "on")

    def test_shell_env_off_overrides_project_on(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        self.assertEqual(_resolve(proj, {"DEV_KIT_TEAM": "off"}), "off")

    def test_shell_env_on_overrides_project_on_and_local_off(self):
        proj = _make_proj(Path(self.tmp), project_team="on",
                          local_team="off")
        self.assertEqual(_resolve(proj, {"DEV_KIT_TEAM": "on"}), "on")

    # ----- Layer 2: project-scope wins when shell env is unset -----

    def test_project_on_when_unset(self):
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        self.assertEqual(_resolve(proj), "on")

    def test_project_off_when_unset(self):
        proj = _make_proj(Path(self.tmp), project_team="off", local_team=None)
        self.assertEqual(_resolve(proj), "off")

    def test_project_truthy_normalizes_to_on(self):
        """Layer 2 must normalize `1`, `true` to `on` (Layer 3 + CLI write
        uses `1` as the on-form)."""
        proj = _make_proj(Path(self.tmp), project_team="1", local_team=None)
        self.assertEqual(_resolve(proj), "on")

    # ----- Layer 3: local-scope kicks in when project-scope is unset -----

    def test_local_on_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team="on")
        self.assertEqual(_resolve(proj), "on")

    def test_local_off_used_when_project_unset(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team="off")
        self.assertEqual(_resolve(proj), "off")

    def test_local_true_normalizes_to_on(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team="true")
        self.assertEqual(_resolve(proj), "on")

    # ----- Layer 2 precedence over Layer 3: project-scope wins when set -----

    def test_project_on_wins_over_local_off(self):
        """Project scope is team-committed; personal local override does
        NOT override a team decision. The operator can still pin team via
        the shell env var (Layer 1) for a one-session override."""
        proj = _make_proj(Path(self.tmp), project_team="on", local_team="off")
        self.assertEqual(_resolve(proj), "on")

    def test_project_off_wins_over_local_on(self):
        proj = _make_proj(Path(self.tmp), project_team="off", local_team="on")
        self.assertEqual(_resolve(proj), "off")

    # ----- Layer 4: silent default = off -----

    def test_default_off_when_no_settings_at_all(self):
        proj = Path(self.tmp) / "bare"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
        self.assertEqual(_resolve(proj), "off")

    def test_default_off_when_settings_have_no_team_key(self):
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        # No DEV_KIT_TEAM key in either file -> Layer 4.
        self.assertEqual(_resolve(proj), "off")

    # ----- Outside any git repo -----

    def test_outside_git_repo_returns_off(self):
        non_git = Path(self.tmp) / "no-git"
        non_git.mkdir()
        self.assertEqual(_resolve(non_git), "off")

    # ----- Value validation (typo -> falls through + stderr warning) -----

    def test_invalid_shell_value_falls_through(self):
        """A shell-side typo ('maybe') must not silently fail-open. The
        resolver should warn to stderr and treat as unset, so the
        project layer takes over."""
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        self.assertEqual(_resolve(proj, {"DEV_KIT_TEAM": "maybe"}), "on")

    def test_invalid_project_value_falls_through_to_local(self):
        proj = _make_proj(Path(self.tmp), project_team="maybe",
                          local_team="on")
        self.assertEqual(_resolve(proj), "on")

    # ----- Orthogonality: team toggle is independent of mode -----

    def test_team_off_independent_of_mode_full(self):
        """team=off + DEV_KIT_MODE=full: both resolve independently to
        their respective silent defaults."""
        proj = _make_proj(Path(self.tmp), project_team=None, local_team=None)
        # Add a project-mode file too.
        (proj / ".claude" / "settings.json").write_text(json.dumps({
            "env": {"DEV_KIT_MODE": "full"},
        }))
        self.assertEqual(_resolve(proj), "off")

    def test_team_on_independent_of_mode_unset(self):
        """team=on with no DEV_KIT_MODE: still resolves to on regardless
        of mode state."""
        proj = _make_proj(Path(self.tmp), project_team="on", local_team=None)
        self.assertEqual(_resolve(proj), "on")


if __name__ == "__main__":
    unittest.main()
