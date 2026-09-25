#!/usr/bin/env python3
"""Regression tests for the optional public Linear skill contract."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILL = ROOT / "skills" / "linear" / "SKILL.md"
HOOK = ROOT / "hooks" / "linear-autosync.sh"
SYNC = ROOT / "tools" / "linear_sync.py"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"
HOOKS_INDEX = ROOT / "hooks" / "index.md"


def _frontmatter(text: str) -> str:
    match = re.match(r"^---\s*\n(.+?)\n---", text, re.DOTALL)
    if not match:
        raise AssertionError("linear skill frontmatter is missing")
    return match.group(1)


class TestLinearSkill(unittest.TestCase):
    def test_public_skill_metadata_is_valid(self):
        text = SKILL.read_text(encoding="utf-8")
        frontmatter = _frontmatter(text)
        self.assertIn("name: linear", frontmatter)
        self.assertIn("category: config", frontmatter)
        self.assertIn("alpha: state", frontmatter)
        self.assertIn("user-invocable: true", frontmatter)

    def test_explicit_and_implicit_paths_are_documented(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("/dev-kit:linear", text)
        self.assertIn("LINEAR_SKIP", text)
        self.assertIn("LINEAR_ERROR", text)
        # The explicit /dev-kit:linear skill is NOT model-invoked on every
        # prompt; auto-sync is a separate, gated hook path. Both contracts
        # must be visible in the skill body.
        self.assertIn("called once by a workflow skill at task start", text)
        self.assertIn("fired automatically on every Edit|Write", text)

    def test_stale_handoff_does_not_force_issue_reuse(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("existing handoff as context", text)
        self.assertIn("old, closed, or unrelated handoff", text)
        self.assertIn("scope and intended outcome match", text)

    def test_workflow_callers_use_single_optional_preflight(self):
        for name in ("plan", "build", "refactor"):
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("Optional Linear preflight", text, name)
            self.assertIn("LINEAR_SKIP", text, name)
            self.assertIn("once", text, name)

    def test_configuration_describes_non_blocking_modes(self):
        text = (ROOT / "skills" / "config" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("off", text)
        self.assertIn("auto", text)
        self.assertIn("without blocking", text)

    def test_auto_sync_trigger_is_documented(self):
        """#539 follow-up: the skill must describe the hook-based
        auto-sync on every Edit|Write, and the underlying script
        + hook that implement it."""
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("hooks/linear-autosync.sh", text)
        self.assertIn("tools/linear_sync.py", text)
        self.assertRegex(text, r"Edit\|Write\|MultiEdit")
        # Must still document the non-blocking contract.
        self.assertIn("non-blocking", text.lower())
        self.assertIn("exit code 0", text)

    def test_auto_sync_hook_and_script_exist(self):
        self.assertTrue(HOOK.is_file(), "hooks/linear-autosync.sh missing")
        self.assertTrue(SYNC.is_file(), "tools/linear_sync.py missing")
        self.assertEqual(HOOK.stat().st_mode & 0o111, 0o111, "hook must be executable")
        # Hook must not block: any non-zero exit would deny the tool call.
        hook_text = HOOK.read_text(encoding="utf-8")
        self.assertIn("exit 0", hook_text)
        self.assertIn("linear-autosync", hook_text)
        # Script must define a non-raising `sync()` entry point.
        sync_text = SYNC.read_text(encoding="utf-8")
        self.assertIn("def sync()", sync_text)
        self.assertIn("except Exception", sync_text)

    def test_hooks_json_wires_linear_autosync_into_edit_write(self):
        cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        pretooluse = cfg["hooks"]["PreToolUse"]
        edit_write = next(
            m for m in pretooluse if m.get("matcher") == "Write|Edit|MultiEdit"
        )
        commands = [h["command"] for h in edit_write["hooks"]]
        self.assertTrue(
            any("linear-autosync.sh" in c for c in commands),
            f"linear-autosync.sh not wired into Write|Edit|MultiEdit: {commands}",
        )

    def test_hooks_index_lists_linear_autosync(self):
        text = HOOKS_INDEX.read_text(encoding="utf-8")
        self.assertIn("linear-autosync", text)
        # The matrix entry must mark it as gated rather than always-on.
        self.assertIn("gated", text.lower())

    def test_skill_exposes_cli_subcommands(self):
        """`/dev-kit:linear on | off | status | setup | project-name ...`
        must delegate to `tools/linear_sync.py`."""
        text = SKILL.read_text(encoding="utf-8")
        frontmatter = _frontmatter(text)
        # Subcommand bullets must be in when_to_use.
        self.assertIn("/dev-kit:linear on", text)
        self.assertIn("/dev-kit:linear off", text)
        self.assertIn("/dev-kit:linear status", text)
        # Bash is required to invoke the CLI.
        self.assertIn("Bash", frontmatter.split("allowed-tools:")[1].split("\n")[0])
        # The body must reference the CLI as the source of truth.
        self.assertIn("python3 tools/linear_sync.py", text)

    def test_skill_documents_priority_model(self):
        """The skill must document that the Linear API is priority 1
        and the hand-off file is priority 2 (cache)."""
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Linear API > hand-off file", text)
        self.assertIn("priority", text.lower())
        self.assertIn("source of truth", text.lower())
        self.assertIn("_meta", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
