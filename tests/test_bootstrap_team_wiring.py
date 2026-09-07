#!/usr/bin/env python3
r"""test_bootstrap_team_wiring.py — verify the team sub-stage 8.5 wiring.

The bootstrap skill reads $DEV_KIT_TEAM via hooks/lib/team-resolve.sh
and, when team=on, strips `^\.dev-kit` lines from the target
`.gitignore`. This test exercises the resolver + gitignore strip in
isolation (without running the full /dev-kit:bootstrap skill) so we
can pin the behavior:

  - team=on + target has .gitignore with `.dev-kit/` -> stripped
  - team=on + target has .gitignore without `.dev-kit/` -> untouched
  - team=on + target is not a git repo -> no-op
  - team=off (silent default) -> .gitignore untouched, no print
  - team=on + .dev-kit/ not ignored -> no-op, prints reason

The strip itself is a one-liner; this test pins the full sequence so
any future change to team-resolve.sh or the bootstrap orchestration
catches a regression in CI.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEAM_LIB = REPO_ROOT / "hooks" / "lib" / "team-resolve.sh"


def _run_team_strip(proj: Path, env_override: dict | None = None) -> tuple[int, str, str]:
    """Run the resolve + strip sequence (mirrors bootstrap sub-stage 8.5)
    and return (returncode, stdout, stderr)."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env.pop("DEV_KIT_TEAM", None)
    if env_override:
        env.update(env_override)
    script = rf"""
      source "{TEAM_LIB}"
      dev_kit_team_resolve
      if [ "$DEV_KIT_TEAM" = "on" ]; then
        if [ -d .git ] || git rev-parse --git-dir >/dev/null 2>&1; then
          if [ -f .gitignore ]; then
            if grep -q "^\.dev-kit" .gitignore; then
              grep -v "^\.dev-kit" .gitignore > .gitignore.tmp || true
              mv .gitignore.tmp .gitignore
              echo "team=on: .dev-kit/ kept in git"
            else
              echo "team=on: .gitignore has no .dev-kit/ entry to strip"
            fi
          else
            echo "team=on: no .gitignore to update"
          fi
        else
          echo "team=on: not a git repo; nothing to track"
        fi
      fi
    """
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=10,
        cwd=str(proj), env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _make_git_proj(tmp: Path, *, gitignore_content: str | None) -> Path:
    proj = tmp / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    if gitignore_content is not None:
        (proj / ".gitignore").write_text(gitignore_content)
    return proj


class TestBootstrapTeamWiring(unittest.TestCase):
    """bootstrap sub-stage 8.5: resolve + gitignore strip."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- team=on -----

    def test_team_on_strips_devkit_from_gitignore(self):
        proj = _make_git_proj(
            Path(self.tmp),
            gitignore_content=".dev-kit/\nnode_modules/\n*.pyc\n",
        )
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("team=on: .dev-kit/ kept in git", out)
        body = (proj / ".gitignore").read_text()
        self.assertNotIn(".dev-kit", body)
        self.assertIn("node_modules/", body)
        self.assertIn("*.pyc", body)

    def test_team_on_no_devkit_entry_is_noop(self):
        proj = _make_git_proj(
            Path(self.tmp),
            gitignore_content="node_modules/\n*.pyc\n",
        )
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("team=on: .gitignore has no .dev-kit/ entry to strip", out)
        # .gitignore must be unchanged.
        self.assertEqual((proj / ".gitignore").read_text(),
                         "node_modules/\n*.pyc\n")

    def test_team_on_no_gitignore_is_noop(self):
        proj = _make_git_proj(Path(self.tmp), gitignore_content=None)
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("team=on: no .gitignore to update", out)
        self.assertFalse((proj / ".gitignore").exists())

    def test_team_on_non_git_target_is_noop(self):
        """If the target is not a git repo, team=on must not crash."""
        proj = Path(self.tmp) / "no-git"
        proj.mkdir()
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("team=on: not a git repo", out)

    def test_team_on_strips_multiple_devkit_lines(self):
        proj = _make_git_proj(
            Path(self.tmp),
            gitignore_content=".dev-kit/\n.dev-kit/logs/\n.dev-kit/cache/\nfoo\n",
        )
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertEqual(rc, 0, msg=err)
        body = (proj / ".gitignore").read_text()
        self.assertNotIn(".dev-kit", body)
        self.assertEqual(body, "foo\n")

    # ----- team=off (silent default) -----

    def test_team_off_leaves_gitignore_untouched(self):
        proj = _make_git_proj(
            Path(self.tmp),
            gitignore_content=".dev-kit/\nnode_modules/\n",
        )
        rc, out, err = _run_team_strip(proj)
        self.assertEqual(rc, 0, msg=err)
        # Silent: nothing printed about team.
        self.assertNotIn("team=on", out)
        self.assertNotIn("team=off", out)
        # .gitignore unchanged.
        body = (proj / ".gitignore").read_text()
        self.assertIn(".dev-kit/", body)
        self.assertIn("node_modules/", body)

    def test_team_off_explicit_via_env_leaves_gitignore_untouched(self):
        proj = _make_git_proj(
            Path(self.tmp),
            gitignore_content=".dev-kit/\n",
        )
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "off"})
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual((proj / ".gitignore").read_text(), ".dev-kit/\n")

    # ----- Idempotency -----

    def test_team_on_idempotent_second_pass(self):
        """A second pass with team=on must find no .dev-kit/ lines and
        report the no-op branch."""
        proj = _make_git_proj(
            Path(self.tmp),
            gitignore_content=".dev-kit/\n",
        )
        # First pass strips.
        _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertNotIn(".dev-kit", (proj / ".gitignore").read_text())
        # Second pass finds nothing to strip.
        rc, out, err = _run_team_strip(proj, {"DEV_KIT_TEAM": "on"})
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("team=on: .gitignore has no .dev-kit/ entry to strip", out)


if __name__ == "__main__":
    unittest.main()
