#!/usr/bin/env python3
"""test_guard_mode_hooks.py — integration tests for the /dev-kit:guard-mode
session bypass wired into hooks/tdd-guard.sh and hooks/worktree-guard.sh.

Verifies:
  - Explicit policy `on` keeps both guards enforcing.
  - guard_mode_state "off" makes tdd-guard.sh allow a core-code edit with
    no RED evidence, and worktree-guard.sh allow an Edit in the main
    checkout.
  - Each guard's "off" state is independent of the other.
  - hooks/session-start-guard-mode-reset.sh applies the resolved policy and
    keeps the unconfigured default off.
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
#
# Strip harness-controlled vars (DEV_KIT_GUARDS*, ANTHROPIC_*) so the
# subprocess sees the same starting state regardless of the developer's
# shell. Tests that need a specific DEV_KIT_GUARDS override it explicitly
# via `env={**_ENV_WITH_LIB, "DEV_KIT_GUARDS": "off", ...}`.
_HARNESS_SKIP = {"DEV_KIT_GUARDS", "DEV_KIT_GUARDS_SOURCE", "DEV_KIT_GUARD_ROOT"} | {
    k for k in _os.environ if k.startswith("ANTHROPIC_")
}
_ENV_WITH_LIB = {
    **{k: v for k, v in _os.environ.items() if k not in _HARNESS_SKIP},
    "PYTHONPATH": str(REPO_ROOT),
}


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
    def test_unconfigured_main_is_off(self):
        tmp = _init_main_repo()
        try:
            r = subprocess.run(
                ["bash", str(HOOKS / "worktree-guard.sh")],
                input=json.dumps(_edit_payload(str(Path(tmp.name) / "foo.py"))),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env=_ENV_WITH_LIB,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            tmp.cleanup()

    def test_default_still_denies_in_main_checkout(self):
        tmp = _init_main_repo()
        try:
            gms.reset_state(Path(tmp.name), policy="on", policy_source="project",
                            branch_class="main")
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
            gms.reset_state(Path(tmp.name), policy="on", policy_source="project",
                            branch_class="main")
            gms.write_state({"worktree_guard": "off"}, root=Path(tmp.name))
            r = subprocess.run(
                ["bash", str(HOOKS / "tdd-guard.sh")],
                input=json.dumps(_edit_payload(str(Path(tmp.name) / "lib" / "core.py"))),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env={**_ENV_WITH_LIB, "DEV_KIT_GUARD_ROOT": tmp.name},
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
                env={**_ENV_WITH_LIB, "DEV_KIT_GUARD_ROOT": str(root)},
            )
            self.assertEqual(r.returncode, 0, r.stderr)


class TestSessionStartGuardModeReset(unittest.TestCase):
    def test_reset_hook_applies_default_off_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gms.write_state(
                {"tdd_guard": "off", "worktree_guard": "off", "fork_pr_confirm": "on"},
                root=root,
            )

            r = subprocess.run(
                ["bash", str(HOOKS / "session-start-guard-mode-reset.sh")],
                capture_output=True, text=True, timeout=10, cwd=root,
                env={**_ENV_WITH_LIB, "CLAUDE_PROJECT_DIR": str(root)},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            state = gms.read_state(root)
            self.assertEqual(
                state,
                {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
                 "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
                 "policy_source": "outside-git", "branch_class": "outside"},
            )

    def test_reset_hook_applies_project_on_policy(self):
        tmp = _init_main_repo()
        try:
            (Path(tmp.name) / ".claude").mkdir()
            (Path(tmp.name) / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"DEV_KIT_GUARDS": "on"}})
            )
            r = subprocess.run(
                ["bash", str(HOOKS / "session-start-guard-mode-reset.sh")],
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env={**_ENV_WITH_LIB, "CLAUDE_PROJECT_DIR": str(Path(tmp.name))},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            state = gms.read_state(Path(tmp.name))
            for guard in gms.POLICY_GUARDS:
                self.assertEqual(state[guard], "on")
            self.assertEqual(state["policy_source"], "project")
            self.assertEqual(state["branch_class"], "main")
        finally:
            tmp.cleanup()


class TestGuardFailOpenShortCircuit(unittest.TestCase):
    """Pins the silent-bypass contract: when DEV_KIT_GUARDS=off, each guard
    hook MUST exit 0 silently and MUST NOT emit any `guard.blocked` event
    into `.dev-kit/trace/events.jsonl`.

    The committed `.claude/settings.json` ships `env.DEV_KIT_GUARDS=on`, so
    the normal CI run never exercises this branch — a future contributor
    adding audit emissions inside the fail-open short-circuit would
    silently inflate the prevention_quality metric. This test catches
    that regression by exercising every guard with the policy explicitly
    forced off and asserting no blocked event lands on disk.
    """

    def _events_path(self, root: Path) -> Path:
        return root / ".dev-kit" / "trace" / "events.jsonl"

    def _read_blocked_events(self, root: Path) -> list:
        path = self._events_path(root)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def test_worktree_guard_fail_open_silent(self):
        """With DEV_KIT_GUARDS=off, worktree-guard.sh must exit 0 on a
        main-checkout Edit AND must not emit guard.blocked."""
        tmp = _init_main_repo()
        try:
            r = subprocess.run(
                ["bash", str(HOOKS / "worktree-guard.sh")],
                input=json.dumps(_edit_payload(str(Path(tmp.name) / "foo.py"))),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env={**_ENV_WITH_LIB, "DEV_KIT_GUARDS": "off",
                     "DEV_KIT_GUARD_ROOT": tmp.name},
            )
            self.assertEqual(r.returncode, 0,
                             f"worktree-guard failed open: stderr={r.stderr!r}")
            blocked = [e for e in self._read_blocked_events(Path(tmp.name))
                       if e.get("event_type") == "guard.blocked"]
            self.assertEqual(
                blocked, [],
                f"worktree-guard emitted guard.blocked on fail-open: {blocked!r}",
            )
        finally:
            tmp.cleanup()

    def test_tdd_guard_fail_open_silent(self):
        """With DEV_KIT_GUARDS=off, tdd-guard.sh must exit 0 on a core
        Edit AND must not emit guard.blocked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = subprocess.run(
                ["bash", str(HOOKS / "tdd-guard.sh")],
                input=json.dumps(_edit_payload(str(root / "lib" / "core.py"))),
                capture_output=True, text=True, timeout=10, cwd=root,
                env={**_ENV_WITH_LIB, "DEV_KIT_GUARDS": "off",
                     "DEV_KIT_GUARD_ROOT": str(root)},
            )
            self.assertEqual(r.returncode, 0,
                             f"tdd-guard failed open: stderr={r.stderr!r}")
            blocked = [e for e in self._read_blocked_events(root)
                       if e.get("event_type") == "guard.blocked"]
            self.assertEqual(
                blocked, [],
                f"tdd-guard emitted guard.blocked on fail-open: {blocked!r}",
            )

    def test_git_guard_fail_open_silent(self):
        """With DEV_KIT_GUARDS=off, git-guard.sh must exit 0 on a direct
        `git commit` on main AND must not emit guard.blocked.

        The fail-open short-circuit fires BEFORE the command-parsing
        pipeline runs, so the input payload is structurally complete
        (a real `git commit` on main would normally deny).
        """
        tmp = _init_main_repo()
        try:
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "git commit -m test"},
            }
            r = subprocess.run(
                ["bash", str(HOOKS / "git-guard.sh")],
                input=json.dumps(payload),
                capture_output=True, text=True, timeout=10, cwd=tmp.name,
                env={**_ENV_WITH_LIB, "DEV_KIT_GUARDS": "off",
                     "DEV_KIT_GUARD_ROOT": tmp.name},
            )
            self.assertEqual(r.returncode, 0,
                             f"git-guard failed open: stderr={r.stderr!r}")
            blocked = [e for e in self._read_blocked_events(Path(tmp.name))
                       if e.get("event_type") == "guard.blocked"]
            self.assertEqual(
                blocked, [],
                f"git-guard emitted guard.blocked on fail-open: {blocked!r}",
            )
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
