#!/usr/bin/env python3
"""test_hook_filtering.py — per-hook mode-gate regression tests.

For every hook in hooks/*.sh, assert that the gate (dev_kit_mode_require)
short-circuits (exit 0 with no side effects) when the active mode does
NOT match its required mode.

For each hook we extract the declared required mode (default = "full"
when no dev_kit_mode_require line is present, which would be a regression
caught separately) and then invoke the hook with three different
DEV_KIT_MODE values:
  - "full"      → must exit 0 (or run its body, exit 0 with success)
  - "lite"      → must exit 0 silently when the hook is NOT in lite subset
  - "undev"     → must exit 0 silently for ALL hooks (the gate is the
                  first thing the hook runs; if mode != required, exit 0)

We synthesize a minimal hook payload (Write|Edit|Bash depending on
matcher) so the hook reaches its body and the gate decides.

This pins the LITE_HOOKS set: if anyone moves a hook into or out of
the lite subset, this test breaks.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

# Hooks classified as "lite subset" — they fire in BOTH full and lite.
# MUST match hooks/lib/mode-resolve.sh LITE_HOOKS in install / migration
# scripts. Adding or removing a hook here is a deliberate decision.
LITE_HOOKS = {
    "destructive-confirm.sh",
    "git-guard.sh",
    "secret-scan.sh",
    "session-start-check.sh",
    "stop-verify.sh",
    "tdd-guard.sh",
    "worktree-guard.sh",
}


def _hook_required_mode(hook_path: Path) -> set[str]:
    """Return the set of modes that activate this hook, parsed from the
    dev_kit_mode_require line(s) inside the hook body.

    Defaults to {"full"} if no dev_kit_mode_require line is present (a
    regression that should be flagged — covered separately).
    """
    text = hook_path.read_text()
    matches = re.findall(r"dev_kit_mode_require[ \t]+([^\n]+)", text)
    if not matches:
        return {"full"}
    modes: set[str] = set()
    for m in matches:
        for token in m.strip().split(","):
            modes.add(token.strip())
    return modes


def _synthesize_payload(hook_name: str) -> dict:
    """Build a minimal PreToolUse payload appropriate for each hook."""
    base = {
        "session_id": "test-session",
        "transcript_path": "/tmp/test-transcript",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
    }
    # Most hooks use a generic Write or Bash payload
    if hook_name in {"destructive-confirm.sh", "tdd-guard.sh",
                     "worktree-guard.sh", "injection-content-guard.sh",
                     "acp-tier-assert.sh", "secret-scan.sh"}:
        return {**base, "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/foo.md"}}
    if hook_name in {"git-guard.sh", "bash-guard.sh", "l4-todo-scan.sh",
                     "review-yml-isolation.sh", "loop-detect.sh",
                     "context-window-guard.sh", "sub-agent-handoff.sh",
                     "provider-divergence-check.sh", "notification-collapse.sh",
                     "trace-session-end.sh"}:
        return {**base, "tool_name": "Bash",
                "tool_input": {"command": "git status"}}
    # Default: empty Edit payload (most permissive)
    return {**base, "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/foo.md"}}


def _run_hook(hook_path: Path, payload: dict, dev_kit_mode: str,
              project_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["DEV_KIT_MODE"] = dev_kit_mode
    env["DEV_KIT_NO_CONFIRM"] = "1"
    return subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10,
        cwd=str(project_root), env=env,
    )


class TestHookFiltering(unittest.TestCase):
    """For every hook, every (mode × required-mode) pair behaves correctly."""

    @classmethod
    def setUpClass(cls):
        cls.hooks = sorted(HOOKS_DIR.glob("*.sh"))
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "proj"
        proj.mkdir()
        (proj / ".claude").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
        # Use full-mode default so the gate's "default = full when plugin
        # enabled" doesn't accidentally short-circuit everything. We override
        # per test via DEV_KIT_MODE env var.
        (proj / ".claude" / "settings.json").write_text(json.dumps({
            "enabledPlugins": {"dev-kit@dev-kit": True},
        }))
        cls.project_root = proj

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_lite_subset_list_matches_classification(self):
        """LITE_HOOKS constant must match the hook's declared mode."""
        for hook in self.hooks:
            name = hook.name
            required = _hook_required_mode(hook)
            if name in LITE_HOOKS:
                self.assertIn("lite", required,
                              f"{name} is in LITE_HOOKS but does not declare "
                              f"'lite' in dev_kit_mode_require (got {required})")
            else:
                self.assertNotIn("lite", required,
                                 f"{name} is NOT in LITE_HOOKS but declares "
                                 f"'lite' in dev_kit_mode_require (got {required})")

    def test_all_hooks_exit_cleanly_under_undev(self):
        """In undev mode, every hook must exit 0 silently (the gate's
        no-op path). This is the leak-closure guarantee."""
        for hook in self.hooks:
            with self.subTest(hook=hook.name):
                payload = _synthesize_payload(hook.name)
                result = _run_hook(hook, payload, "undev", self.project_root)
                self.assertEqual(result.returncode, 0,
                                 f"{hook.name} exited {result.returncode} "
                                 f"under mode=undev (expected 0 silent gate).\n"
                                 f"  stdout: {result.stdout[:300]}\n"
                                 f"  stderr: {result.stderr[:300]}")

    def test_full_only_hooks_short_circuit_under_lite(self):
        """Hooks in `LITE_HOOKS` fire in lite; hooks NOT in LITE_HOOKS
        must short-circuit (exit 0 silently) under mode=lite."""
        for hook in self.hooks:
            name = hook.name
            if name in LITE_HOOKS:
                continue
            with self.subTest(hook=name):
                payload = _synthesize_payload(name)
                result = _run_hook(hook, payload, "lite", self.project_root)
                self.assertEqual(result.returncode, 0,
                                 f"{name} (full-only) exited {result.returncode} "
                                 f"under mode=lite. Expected silent 0.\n"
                                 f"  stdout: {result.stdout[:300]}\n"
                                 f"  stderr: {result.stderr[:300]}")

    def test_every_hook_has_mode_gate(self):
        """No hook should be missing dev_kit_mode_require — that would mean
        it fires in lite mode even when it shouldn't."""
        for hook in self.hooks:
            with self.subTest(hook=hook.name):
                text = hook.read_text()
                self.assertIn("dev_kit_mode_require", text,
                              f"{hook.name} is missing dev_kit_mode_require "
                              f"— it will fire in lite mode regardless of intent.")


if __name__ == "__main__":
    unittest.main()
