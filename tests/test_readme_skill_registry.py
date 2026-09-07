#!/usr/bin/env python3
"""Root README is the skill registry SSOT.

Regression guard for the gap that `/dev-kit:gate-select` fell through:
the skill shipped in #786, the maintenance gate's registry check was
satisfied by a `docs/skills/README.md` touch alone, and the root README
— the front door every operator reads first — never learned the skill
existed. Same for `guard-mode`, `harness-mode`, and `worktree-prune`.

The invariant: every user-invocable skill is named in the root README.
`lib/maintenance_gate.py::registry_index_updated_ok` enforces that a PR
adding a new skill *touches* the README; this test enforces that the
touch actually *registered the skill*, which a path-level check cannot
see.

Model-invocable-only skills (`user-invocable: false`) are internal
machinery — operators type the parent command, not the helper — so they
are out of scope by design.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
SKILLS_DIR = REPO_ROOT / "skills"

# `user-invocable: true` on its own line, tolerating trailing comments.
_USER_INVOCABLE_RE = re.compile(
    r"^user-invocable:\s*true\s*(?:#.*)?$", re.MULTILINE
)


def _user_invocable_skills() -> list[str]:
    """Skill names whose SKILL.md declares `user-invocable: true`."""
    names = []
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        if _USER_INVOCABLE_RE.search(text):
            names.append(skill_md.parent.name)
    return names


class TestReadmeSkillRegistry(unittest.TestCase):
    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")
        self.skills = _user_invocable_skills()

    def test_fixture_finds_skills(self):
        # Guard against a silently-empty glob turning the real assertion
        # below into a no-op that always passes.
        self.assertGreater(len(self.skills), 20, self.skills)

    def test_every_user_invocable_skill_is_in_root_readme(self):
        missing = [
            name for name in self.skills
            if f"/dev-kit:{name}`" not in self.readme
        ]
        self.assertEqual(
            missing, [],
            "user-invocable skills absent from the root README: "
            f"{missing}. Add a row to one of README.md's 'Most-used "
            "skills' tables as `/dev-kit:<name>`, or flip the skill to "
            "`user-invocable: false` if it is internal machinery.",
        )


class TestReadmeBootstrapDefault(unittest.TestCase):
    """The root README must not contradict `skills/bootstrap/SKILL.md`.

    #786 flipped the ci-setup prompt from `[Y/n]` to `[y/N]`; the root
    README kept claiming "the prompt defaults to Y" for four releases.
    """

    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")
        self.skill = (
            SKILLS_DIR / "bootstrap" / "SKILL.md"
        ).read_text(encoding="utf-8")

    def test_skill_still_declares_yN_prompt(self):
        # Pins the source of truth this test compares the README against.
        self.assertIn("[y/N]", self.skill)

    def test_readme_does_not_claim_y_default(self):
        self.assertNotIn("defaults to Y", self.readme)


if __name__ == "__main__":
    unittest.main()
