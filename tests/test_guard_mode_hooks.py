#!/usr/bin/env python3
"""test_guard_mode_hooks.py — integration tests for the /dev-kit:guard-mode
session bypass wired into hooks/tdd-guard.sh and hooks/worktree-guard.sh.

Verifies:
  - Default (no state file): both guards still enforce (parity with the
    pre-existing test_worktree_guard.py / test_tdd_guard.py behavior).
  - guard_mode_state "off" makes tdd-guard.sh allow a core-code edit with
    no RED evidence, and worktree-guard.sh allow an Edit in the main
    checkout.
  - Each guard's "off" state is independent of the other.
  - hooks/session-start-guard-mode-reset.sh resets a previously-"off"
    state back to "on" (the "new window = enforced by default" contract).
"""
from __future__ import annotations

import json
import os as _os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS = REPO_ROOT / "hooks"
LIB = REPO_ROOT / "lib"
sys.path.insert(0, str(LIB))

import guard_mode_state as gms  # noqa: E402

# Hooks invoke `python3 -m lib.guard_mode_state` bare (matching the
# existing `python3 -m lib.tdd_scope_policy` call already in
# hooks/tdd-guard.sh), which resolves correctly in production because
# Claude Code always sets the hook's cwd to the real project root
# (co-located with `lib/`). These tests instead build a throwaway git
# repo elsewhere to exercise worktree-guard's main-checkout detection,
# so `lib` is not importable via cwd alone — PYTHONPATH bridges that gap
# without changing how the hooks themselves resolve the module.
_ENV_WITH_LIB = {**_os.environ, "PYTHONPATH": str(REPO_ROOT)}


def _edit_payload(file_path: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path}}


def _init_main_repo() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, capture_output=True)
    return tmp


class TestWorktreeGuardBypass(unittest.TestCase):
    def test_default_still_denies_in_main_checkout(self):
        tmp = _init_main_repo()
        try:
            r = subprocess.run(
                ["bash", str(HOOKS / "worktree-guard.sh")],
                input=json.dumps(_edit_payload(str(Path(tmp.name) / "foo.py"))),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
            )
            self.assertEqual(r.returncode, 2, r.stderr)
        finally:
            tmp.cleanup()

    def test_off_allows_edit_in_main_checkout(self):
        tmp = _init_main_repo()
        try:
            gms.write_state({"worktree_guard": "off"}, root=Path(tmp.name))
            r = subprocess.run(
                ["bash", str(HOOKS / "worktree-guard.sh")],
                input=json.dumps(_edit_payload(str(Path(tmp.name) / "foo.py"))),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env=_ENV_WITH_LIB,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            tmp.cleanup()

    def test_off_worktree_guard_does_not_disable_tdd_guard(self):
        tmp = _init_main_repo()
        try:
            gms.write_state({"worktree_guard": "off"}, root=Path(tmp.name))
            r = subprocess.run(
                ["bash", str(HOOKS / "tdd-guard.sh")],
                input=json.dumps(_edit_payload(str(Path(tmp.name) / "lib" / "core.py"))),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env={**_ENV_WITH_LIB, "DEV_KIT_TDD_ROOT": tmp.name},
            )
            self.assertEqual(r.returncode, 2, r.stderr)
        finally:
            tmp.cleanup()


class TestTddGuardBypass(unittest.TestCase):
    def test_off_allows_core_edit_without_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gms.write_state({"tdd_guard": "off"}, root=root)

            r = subprocess.run(
                ["bash", str(HOOKS / "tdd-guard.sh")],
                input=json.dumps(_edit_payload(str(root / "lib" / "core.py"))),
                capture_output=True, text=True, timeout=10, cwd=root,
                env={**_ENV_WITH_LIB, "DEV_KIT_TDD_ROOT": str(root)},
            )
            self.assertEqual(r.returncode, 0, r.stderr)


class TestSessionStartGuardModeReset(unittest.TestCase):
    def test_reset_hook_restores_both_guards_to_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gms.write_state({"tdd_guard": "off", "worktree_guard": "off"}, root=root)

            r = subprocess.run(
                ["bash", str(HOOKS / "session-start-guard-mode-reset.sh")],
                capture_output=True, text=True, timeout=10, cwd=root,
                env={**_ENV_WITH_LIB, "CLAUDE_PROJECT_DIR": str(root)},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            state = gms.read_state(root)
            self.assertEqual(state, {"tdd_guard": "on", "worktree_guard": "on"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
