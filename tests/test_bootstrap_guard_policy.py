#!/usr/bin/env python3
"""Regression tests for the thin guard bootstrap contract."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent


class TestBootstrapGuardPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = (ROOT / "skills" / "bootstrap" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    def test_bootstrap_has_one_time_project_or_local_prompt(self):
        self.assertIn("Enable repository guards (worktree, git, TDD)? [y/N]", self.skill)
        self.assertIn("[project/local]", self.skill)
        self.assertIn("SessionStart never asks this question", self.skill)
        self.assertIn("On later bootstrap runs, report the current", self.skill)

    def test_bootstrap_defaults_to_off_and_preserves_scopes(self):
        self.assertIn("Guards default off everywhere", self.skill)
        self.assertIn("Preserve all existing JSON keys", self.skill)
        user_template = json.loads(
            (ROOT / "docs/scopes/templates/settings.user.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("DEV_KIT_GUARDS", user_template.get("env", {}))

    def test_all_project_and_local_templates_are_explicitly_off(self):
        template_dir = ROOT / "docs/scopes/templates"
        for path in sorted(template_dir.glob("settings*.json")):
            with self.subTest(path=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                if path.name == "settings.user.json":
                    self.assertNotIn("DEV_KIT_GUARDS", data.get("env", {}))
                else:
                    self.assertEqual(data["env"]["DEV_KIT_GUARDS"], "off")

    def test_session_start_is_prompt_free_and_reports_policy(self):
        hook = (ROOT / "hooks" / "session-start-check.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("prompt-free", hook)
        self.assertIn("guards=", hook)
        self.assertNotIn("AskUserQuestion", hook)


if __name__ == "__main__":
    unittest.main()
