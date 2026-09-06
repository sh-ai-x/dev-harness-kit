#!/usr/bin/env python3
"""
test_naming.py — Naming convention consistency (MUST-NOT-15, ADR-0010).

Tests verify:
- Skill directory names = SKILL.md frontmatter `name:` field
- Skill `category:` ∈ 9 allowed categories
- Slash command files exist
- File kebab-case naming in lib/ + commands/ + skills/
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

# Use direct path manipulation since this test reads filesystem
PROJECT_ROOT = Path(__file__).parent.parent
ALLOWED_CATEGORIES = frozenset({
    "bootstrap", "plan", "design", "build", "review", "security", "audit", "shortcuts", "ship",
    "config", "eval", "status", "mode",
})
KEBAB_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")


def extract_frontmatter_field(text: str, key: str) -> str | None:
    """Extract a YAML frontmatter field from a Markdown file."""
    m = re.match(r"^---\s*\n(.+?)\n---", text, re.DOTALL)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None


class TestNaming(unittest.TestCase):
    def test_skill_directory_and_frontmatter_name_match(self):
        skills_dir = PROJECT_ROOT / "skills"
        if not skills_dir.exists():
            self.skipTest("no skills dir yet")
        mismatches = []
        for skill_dir in skills_dir.rglob("SKILL.md"):
            expected_dir_name = skill_dir.parent.name  # skills/<skill-name>/SKILL.md
            text = skill_dir.read_text(encoding="utf-8")
            name_field = extract_frontmatter_field(text, "name")
            if name_field != expected_dir_name:
                mismatches.append(
                    f"dir={expected_dir_name} but frontmatter name={name_field} in {skill_dir.relative_to(PROJECT_ROOT)}"
                )
        self.assertEqual(mismatches, [], "Naming mismatches (MUST-NOT-15):\n" + "\n".join(mismatches))

    def test_skill_categories_valid(self):
        # Plugin-only: walk `skills/` (flat, one level). category comes from frontmatter.
        invalid = []
        for skills_dir_name in ("skills",):
            skills_dir = PROJECT_ROOT / skills_dir_name
            if not skills_dir.exists():
                continue
            for skill_dir in skills_dir.rglob("SKILL.md"):
                text = skill_dir.read_text(encoding="utf-8")
                cat = extract_frontmatter_field(text, "category")
                # category is REQUIRED (MUST-NOT-15). No fallback to dir name.
                if cat is None or cat not in ALLOWED_CATEGORIES:
                    invalid.append(f"{skill_dir}: category={cat}")
        self.assertEqual(invalid, [], f"Invalid categories: {invalid}")

    def test_skills_kebab_case(self):
        skills_dir = PROJECT_ROOT / "skills"
        if not skills_dir.exists():
            self.skipTest("no skills dir yet")
        violations = []
        for d in skills_dir.rglob("*"):
            if d.is_dir():
                name = d.name
                if name in {"bootstrap", "plan", "design", "build", "review", "security", "audit", "shortcuts", "ship"}:
                    continue  # category names themselves
                if name in {"__pycache__", ".git", ".worktrees", ".dev-kit", "_acp"}:
                    continue  # ephemeral / dev artifacts not part of the skill layout
                if not KEBAB_RE.match(name):
                    violations.append(str(d.relative_to(PROJECT_ROOT)))
        self.assertEqual(violations, [], f"Non-kebab-case dirs: {violations}")

    def test_commands_exist(self):
        commands_dir = PROJECT_ROOT / "commands"
        if not commands_dir.exists():
            self.skipTest("no commands dir yet")
        existing = {p.name for p in commands_dir.glob("*.md")}
        # Incremental wrapper installs are valid, but the directory must not be empty.
        self.assertTrue(existing, "commands dir empty")

    def test_lib_python_kebab_or_snake(self):
        lib_dir = PROJECT_ROOT / "lib"
        if not lib_dir.exists():
            self.skipTest("no lib dir yet")
        violations = []
        for f in lib_dir.glob("*.py"):
            # Allow __init__.py (Python package marker) + snake_case modules.
            if f.name == "__init__.py":
                continue
            if not re.match(r"^[a-z][a-z0-9_]*\.py$", f.name):
                violations.append(f.name)
        self.assertEqual(violations, [], f"Python naming: {violations}")

    def test_marketplace_plugin_name_match(self):
        mp = PROJECT_ROOT / ".claude-plugin" / "marketplace.json"
        pl = PROJECT_ROOT / ".claude-plugin" / "plugin" / ".claude-plugin" / "plugin.json"
        if not (mp.exists() and pl.exists()):
            self.skipTest("plugin manifests missing")
        import json
        m_name = json.loads(mp.read_text())["name"]
        p_name = json.loads(pl.read_text())["name"]
        self.assertEqual(m_name, "dev-kit")
        self.assertEqual(p_name, "dev-kit")

    def test_plugin_manifest_version_is_semver(self):
        """feat/skill-versions: `.claude-plugin/plugin.json:version` must match semver.

        Single source of truth — the refactor dropped per-skill `version:`
        fields to avoid bureaucratic tax. Plugin-level is the only one
        maintained; if it goes stale the PR gate and the `audit --outdated`
        mode (inlined into skills/inspect/SKILL.md (--secrets)) both surface the drift.
        """
        mp = PROJECT_ROOT / ".claude-plugin" / "plugin.json"
        if not mp.exists():
            self.skipTest("plugin manifest missing")
        import json
        data = json.loads(mp.read_text())
        ver = data.get("version")
        semver = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
        self.assertIsNotNone(ver, f"{mp}: missing `version:` field")
        self.assertRegex(
            ver or "", semver,
            f"{mp}: version={ver!r} is not valid semver",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
