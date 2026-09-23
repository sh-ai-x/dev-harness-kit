#!/usr/bin/env python3
"""test_bootstrap_guard_policy.py — behavior tests for the bootstrap guard
contract.

The thin contract the bootstrap skill enforces is:

1. The state file at `.dev-kit/guard-mode.session.json` must reflect the
   resolved `DEV_KIT_GUARDS` policy after SessionStart applies it.
2. `python3 -m lib.guard_mode_state reset --policy on --source project
   --branch-class main` must succeed (exit 0) AND the resulting state
   file must record every POLICY_GUARD as `on` with `policy=`,
   `policy_source=`, and `branch_class=` set from the CLI args.

These tests exercise the real behavior against scratch repos so they
catch a regression in either the CLI or the Python state codec —
prose-only assertions on the SKILL.md body would not.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
LIB = ROOT / "lib"


def _init_main_repo() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name",
                    "Test"], check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "README.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    return tmp


class TestBootstrapGuardPolicyBehavior(unittest.TestCase):
    """Behavior assertions for the bootstrap-applied guard policy."""

    def test_reset_state_against_scratch_repo_records_project_on(self):
        """`python3 -m lib.guard_mode_state reset --policy on --source
        project --branch-class main` against a scratch repo MUST:
          - exit 0
          - write `.dev-kit/guard-mode.session.json`
          - record every POLICY_GUARD as `on`
          - record `policy=on`, `policy_source=project`, `branch_class=main`
        """
        sys.path.insert(0, str(LIB))
        import guard_mode_state as gms  # noqa: E402

        tmp = _init_main_repo()
        try:
            root = Path(tmp.name)
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            result = subprocess.run(
                [sys.executable, "-m", "lib.guard_mode_state",
                 "reset", "--policy", "on",
                 "--source", "project", "--branch-class", "main"],
                cwd=str(root), env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                result.returncode, 0,
                f"reset CLI failed: stdout={result.stdout!r} "
                f"stderr={result.stderr!r}",
            )

            state_path = root / ".dev-kit" / "guard-mode.session.json"
            self.assertTrue(
                state_path.exists(),
                f"reset CLI did not write {state_path} "
                f"(stderr={result.stderr!r})",
            )

            state = json.loads(state_path.read_text())
            for guard in gms.POLICY_GUARDS:
                self.assertEqual(
                    state[guard], "on",
                    f"POLICY_GUARD {guard!r} not set to 'on' "
                    f"in {state_path}; got {state[guard]!r}",
                )
            self.assertEqual(state["policy"], "on")
            self.assertEqual(state["policy_source"], "project")
            self.assertEqual(state["branch_class"], "main")

            # Round-trip through the library read_state() — proves the
            # JSON on disk is also what the runtime sees, not just what
            # the CLI wrote.
            runtime_state = gms.read_state(root)
            self.assertEqual(
                runtime_state["policy"], "on",
                f"gms.read_state({root}) lost policy=on",
            )
            self.assertEqual(
                runtime_state["policy_source"], "project",
                f"gms.read_state({root}) lost policy_source=project",
            )
            self.assertEqual(
                runtime_state["branch_class"], "main",
                f"gms.read_state({root}) lost branch_class=main",
            )
            for guard in gms.POLICY_GUARDS:
                self.assertEqual(
                    runtime_state[guard], "on",
                    f"runtime state for {guard!r} diverged from disk",
                )
        finally:
            tmp.cleanup()

    def test_session_start_hook_applies_project_on_policy_to_scratch(self):
        """The session-start-guard-mode-reset.sh SessionStart hook must
        apply the project-scoped DEV_KIT_GUARDS=on policy to the
        session state when run against a scratch repo that ships
        `.claude/settings.json` with `env.DEV_KIT_GUARDS=on`.
        """
        sys.path.insert(0, str(LIB))
        import guard_mode_state as gms  # noqa: E402
        from conftest import harness_free_env  # noqa: E402

        tmp = _init_main_repo()
        try:
            (Path(tmp.name) / ".claude").mkdir()
            (Path(tmp.name) / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "on"}})
            )
            # Strip harness-controlled vars from the parent env so the
            # hook's policy resolution walks the project-scope path
            # instead of short-circuiting on a shell-scope export.
            env = harness_free_env({
                "PYTHONPATH": str(ROOT),
                "CLAUDE_PROJECT_DIR": tmp.name,
            })
            r = subprocess.run(
                ["bash", str(ROOT / "hooks" / "session-start-guard-mode-reset.sh")],
                capture_output=True, text=True, timeout=10,
                cwd=tmp.name, env=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)

            state = gms.read_state(Path(tmp.name))
            for guard in gms.POLICY_GUARDS:
                self.assertEqual(
                    state[guard], "on",
                    f"session-start hook did not turn {guard!r} on",
                )
            self.assertEqual(state["policy_source"], "project")
            self.assertEqual(state["branch_class"], "main")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
