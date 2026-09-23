#!/usr/bin/env python3
"""test_worktree_log_auto_install.py — End-to-end coverage for the
worktree-log-auto-install PostToolUse hook.

Wires up a fake loghooks source repo + a fake target project + the
REAL hook script (and its sibling log-setup.sh / log-on.sh) under
tmpdir, runs `git worktree add`, then drives the hook with a synthetic
PostToolUse payload (matching the actual shape Claude Code emits).
Asserts:
  - hook exits 0 for non-worktree-add commands (no-op)
  - hook exits 0 for empty payloads
  - hook auto-installs loghooks on the new worktree after a real
    `git worktree add` succeeds (save_log.py + .claude/settings.json
    managed entries both present)
  - hook tolerates the --detach / --force flag variations
  - hook tolerates relative worktree paths
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
HOOK = REPO_ROOT / "hooks" / "worktree-log-auto-install.sh"
LOG_SETUP = REPO_ROOT / "skills" / "log" / "scripts" / "log-setup.sh"
LOG_ON = REPO_ROOT / "skills" / "log" / "scripts" / "log-on.sh"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=check, timeout=10,
    )


def _make_fake_loghooks(tmp: Path) -> Path:
    src = tmp / "loghooks"
    (src / "tools").mkdir(parents=True)
    (src / ".claude").mkdir(parents=True)
    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command",
                                  "command": 'for i in python3 python py; do if "$i" -c "" </dev/null >/dev/null 2>&1; then exec "$i" "${CLAUDE_PROJECT_DIR}/tools/save_log.py" --tool claude-code; fi; done'}]}],
            "SessionEnd": [{"hooks": [{"type": "command",
                                        "command": 'for i in python3 python py; do if "$i" -c "" </dev/null >/dev/null 2>&1; then exec "$i" "${CLAUDE_PROJECT_DIR}/tools/save_log.py" --tool claude-code; fi; done'}]}],
        }
    }
    (src / ".claude" / "settings.json").write_text(json.dumps(settings))
    # Stub save_log.py — does nothing useful, but file must exist for
    # the install to succeed.
    (src / "tools" / "save_log.py").write_text(
        "#!/usr/bin/env python3\nimport sys, os, json\n"
        "payload=json.load(sys.stdin); open('logs/__noop__','w').close(); sys.exit(0)\n"
    )
    (src / "tools" / "save_log.py").chmod(0o755)
    return src


def _make_fake_target(tmp: Path, *, log_state: str = "on") -> Path:
    """Fake git checkout.

    log_state:
      "on"  — target has tools/save_log.py + .claude/settings.json
              carrying _loghooks_managed=true entries. The hook should
              propagate this state into the new worktree.
      "off" — target has neither. The hook should skip the install.
    """
    tgt = tmp / "target"
    (tgt / ".claude" / "worktrees").mkdir(parents=True)
    _git(tgt, "init", "-q")
    (tgt / "f").write_text("init")
    _git(tgt, "add", ".")

    if log_state == "on":
        # Mirror a real `/dev-kit:log on` install: tools/save_log.py +
        # .claude/settings.json managed entries.
        _git(tgt, "mkdir", "-p", "tools", "-q", check=False)
        (tgt / "tools").mkdir(parents=True, exist_ok=True)
        (tgt / "tools" / "save_log.py").write_text(
            "#!/usr/bin/env python3\n"
            "import sys, json\n"
            "open('logs/__noop__','w').close()\n"
        )
        (tgt / "tools" / "save_log.py").chmod(0o755)
        (tgt / ".claude").mkdir(parents=True, exist_ok=True)
        (tgt / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {
                # _loghooks_managed lives on the matcher-level object
                # (matches the real merge path at scripts/lib.sh:194-205
                # which adds ($sentinel): true to the matcher entry).
                "Stop": [{"hooks": [{"type": "command",
                                      "command": 'for i in python3 python py; do if "$i" -c "" </dev/null >/dev/null 2>&1; then exec "$i" "${CLAUDE_PROJECT_DIR}/tools/save_log.py" --tool claude-code; fi; done'}],
                          "_loghooks_managed": True}],
                "SessionEnd": [{"hooks": [{"type": "command",
                                            "command": 'for i in python3 python py; do if "$i" -c "" </dev/null >/dev/null 2>&1; then exec "$i" "${CLAUDE_PROJECT_DIR}/tools/save_log.py" --tool claude-code; fi; done'}],
                                "_loghooks_managed": True}],
            }
        }))

    _git(tgt, "add", ".")
    _git(tgt, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    return tgt


def _drive_hook(payload: dict, *, env_extra: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20,
        env={**os.environ, **env_extra},
    )


class TestWorktreeLogAutoInstall(unittest.TestCase):
    def setUp(self):
        if not HOOK.exists():
            self.skipTest(f"hook not found: {HOOK}")
        self.tmp = Path(tempfile.mkdtemp(prefix="wtlog-auto-"))
        self.src = _make_fake_loghooks(self.tmp)
        # Default: target is "log on" state. Per-repo OFF cases override
        # this in their own setUp / via a fresh target.
        self.tgt = _make_fake_target(self.tmp, log_state="on")
        self.env = {"LOGHOOKS_DIR": str(self.src),
                    "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}

    def _make_off_target(self) -> Path:
        """Independent target with log OFF (no save_log.py, no managed
        entries). Used by the per-repo OFF tests."""
        return _make_fake_target(self.tmp / "off_child", log_state="off")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_noop_on_empty_payload(self):
        r = _drive_hook({}, env_extra=self.env)
        self.assertEqual(r.returncode, 0, f"hook should no-op, got {r.stderr}")
        self.assertEqual(r.stderr.strip(), "",
                         f"empty payload should be silent: {r.stderr!r}")

    def test_noop_on_non_worktree_command(self):
        payload = {"tool_input": {"command": "ls -la"}, "cwd": str(self.tgt)}
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        # Hook should silently no-op on unrelated commands.
        self.assertNotIn("hooks installed", r.stderr)

    def test_noop_when_target_dir_missing(self):
        # Command names a worktree path that does not exist on disk
        # (e.g. user typo'd or git worktree add failed). Hook must
        # silently bail out, not throw.
        payload = {
            "tool_input": {"command": "git worktree add -b feat/x .claude/worktrees/nonexistent"},
            "cwd": str(self.tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("does not exist", r.stderr)

    def test_auto_installs_on_successful_worktree_add(self):
        # Pre-create the worktree (so the dir exists for the hook).
        wt_path = self.tgt / ".claude" / "worktrees" / "wt-x"
        _git(self.tgt, "worktree", "add", "-b", "fix/x", str(wt_path))
        self.assertTrue(wt_path.exists())

        payload = {
            "tool_input": {"command": f"git worktree add -b fix/x {wt_path}"},
            "cwd": str(self.tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0,
                         f"hook failed: stdout={r.stdout} stderr={r.stderr}")
        self.assertIn("hooks installed", r.stderr)

        # save_log.py was copied.
        self.assertTrue((wt_path / "tools" / "save_log.py").exists(),
                        f"save_log.py not copied into {wt_path / 'tools'}")
        self.assertTrue((wt_path / "logs" / "claude-code").is_dir(),
                        f"logs/claude-code/ missing in {wt_path}")

        # settings.json has managed hooks.
        settings = json.loads((wt_path / ".claude" / "settings.json").read_text())
        managed = [h for ev in (settings.get("hooks") or {}).values()
                   for h in ev if h.get("_loghooks_managed")]
        self.assertEqual(len(managed), 2,
                         f"expected 2 managed hooks (Stop, SessionEnd), got {managed}")

    def test_handles_relative_worktree_path(self):
        wt_rel = ".claude/worktrees/wt-rel"
        wt_abs = self.tgt / wt_rel
        _git(self.tgt, "worktree", "add", "-b", "fix/rel", wt_rel)
        self.assertTrue(wt_abs.exists())

        payload = {
            "tool_input": {"command": f"git worktree add -b fix/rel {wt_rel}"},
            "cwd": str(self.tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0, f"hook failed: {r.stderr}")
        self.assertIn("hooks installed", r.stderr)
        self.assertTrue((wt_abs / "tools" / "save_log.py").exists())

    def test_handles_force_flag(self):
        # `git worktree add --force -b fix/forced .claude/worktrees/wt-f`
        # — the hook must skip the --force flag when picking the path.
        wt_abs = self.tgt / ".claude" / "worktrees" / "wt-f"
        _git(self.tgt, "worktree", "add", "--force", "-b", "fix/forced", str(wt_abs))
        payload = {
            "tool_input": {"command": f"git worktree add --force -b fix/forced {wt_abs}"},
            "cwd": str(self.tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0, f"hook failed: {r.stderr}")
        self.assertIn("hooks installed", r.stderr)
        self.assertTrue((wt_abs / "tools" / "save_log.py").exists())

    def test_noop_on_worktree_remove(self):
        # `git worktree remove` should NOT trigger auto-install.
        payload = {"tool_input": {"command": "git worktree remove .claude/worktrees/old"},
                   "cwd": str(self.tgt)}
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("hooks installed", r.stderr)

    # ----- per-repo log-state gate (fix/log-per-repo-state-gate) -----
    #
    # When the source repo has /dev-kit:log OFF, the hook MUST skip the
    # auto-install in the new worktree — otherwise an OFF repo flips to
    # ON silently on every `git worktree add`, which is the bug being
    # fixed. Detection signal: managed entries + tools/save_log.py.

    def test_skips_when_source_log_off(self):
        """Source repo: /dev-kit:log OFF → no install in new worktree."""
        off_tgt = self._make_off_target()
        wt_path = off_tgt / ".claude" / "worktrees" / "wt-off"
        _git(off_tgt, "worktree", "add", "-b", "fix/off-x", str(wt_path))
        self.assertTrue(wt_path.exists())

        payload = {
            "tool_input": {"command": f"git worktree add -b fix/off-x {wt_path}"},
            "cwd": str(off_tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0,
                         f"hook failed: stdout={r.stdout} stderr={r.stderr}")
        # Source-OFF path: must NOT install.
        self.assertNotIn("hooks installed", r.stderr,
                         f"unexpected install: {r.stderr}")
        self.assertIn("OFF", r.stderr)
        # And no save_log.py should land in the new worktree.
        self.assertFalse((wt_path / "tools" / "save_log.py").exists(),
                         "save_log.py leaked into worktree when source was OFF")
        self.assertFalse((wt_path / ".claude" / "settings.json").exists(),
                         "settings.json was created in worktree when source was OFF")

    def test_skips_when_only_script_present_no_settings_entries(self):
        """Source has tools/save_log.py but no managed entries.

        This is the post-`log setup` / pre-`log on` state. The strict
        gate (managed entries required, not just script presence) treats
        this as OFF — the user has not opted in to capture yet.
        """
        off_tgt = self._make_off_target()
        # Add only the script — no settings.json managed entries.
        (off_tgt / "tools").mkdir(parents=True, exist_ok=True)
        (off_tgt / "tools" / "save_log.py").write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
        )
        (off_tgt / "tools" / "save_log.py").chmod(0o755)
        _git(off_tgt, "add", "tools")
        _git(off_tgt, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "script only")

        wt_path = off_tgt / ".claude" / "worktrees" / "wt-script-only"
        _git(off_tgt, "worktree", "add", "-b", "fix/script-only", str(wt_path))

        payload = {
            "tool_input": {"command": f"git worktree add -b fix/script-only {wt_path}"},
            "cwd": str(off_tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("hooks installed", r.stderr)
        self.assertFalse((wt_path / ".claude" / "settings.json").exists(),
                         "settings.json was created when source had no managed entries")

    def test_installs_when_source_log_on(self):
        """Regression: when source is ON, hook must still install.

        Asserted separately from the older test_auto_installs_on_*
        tests so the OFF→ON inversion is its own readable case.
        """
        # self.tgt is created with log_state="on" by setUp.
        wt_path = self.tgt / ".claude" / "worktrees" / "wt-on"
        _git(self.tgt, "worktree", "add", "-b", "fix/on-y", str(wt_path))

        payload = {
            "tool_input": {"command": f"git worktree add -b fix/on-y {wt_path}"},
            "cwd": str(self.tgt),
        }
        r = _drive_hook(payload, env_extra=self.env)
        self.assertEqual(r.returncode, 0, f"hook failed: {r.stderr}")
        self.assertIn("hooks installed", r.stderr)
        self.assertTrue((wt_path / "tools" / "save_log.py").exists())


if __name__ == "__main__":
    unittest.main()
