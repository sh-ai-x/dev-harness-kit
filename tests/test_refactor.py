#!/usr/bin/env python3
"""test_refactor.py — Regression for skills/refactor/SKILL.md schema.

Locks in the 3-phase refactor contract so a future edit that
silently drops a phase (or removes the iron-law gate) fails the gate
before merge. Asserts:

- frontmatter: user-invocable: true, category: build
- body has 3 phase headings ([1/3], [2/3], [3/3])
- body has an Iron Law with MUST-L1 / MUST-L3 / MUST-L4 references
- body hand-off names a downstream skill
- body disambiguates from /dev-kit:prune (refactor != delete)
- frontmatter name matches directory name (covered by test_naming.py
  but pinned here for fast failure if the new file regresses)

> Migrated from test_simplify.py. The skill was renamed `simplify` ->
> `refactor`; the deletion counterpart is now a separate skill
> (`prune`) covered by test_prune.py.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
REFACTOR_SKILL = PROJECT_ROOT / "skills" / "refactor" / "SKILL.md"


class TestRefactorSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not REFACTOR_SKILL.exists():
            raise unittest.SkipTest(f"{REFACTOR_SKILL} missing")
        cls.text = REFACTOR_SKILL.read_text(encoding="utf-8")

    def test_frontmatter_user_invocable_true(self):
        m = re.search(r"^user-invocable:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "user-invocable: frontmatter missing")
        self.assertEqual(m.group(1).strip(), "true", "refactor must be user-invocable")

    def test_frontmatter_category_build(self):
        m = re.search(r"^category:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "category: frontmatter missing")
        self.assertEqual(m.group(1).strip(), "build", "refactor category must be 'build'")

    def test_three_phases_present(self):
        for n in (1, 2, 3):
            pattern = rf"\[{n}/3\]"
            self.assertRegex(
                self.text, pattern,
                f"phase [{n}/3] heading missing from body",
            )

    def test_phase_names_match_documented_chain(self):
        self.assertRegex(self.text, r"\[1/3\]\s*INSPECT", "phase 1 should be INSPECT")
        self.assertRegex(self.text, r"\[2/3\]\s*REFACTOR", "phase 2 should be REFACTOR")
        self.assertRegex(self.text, r"\[3/3\]\s*REVIEW", "phase 3 should be REVIEW")

    def test_iron_law_cites_three_musts(self):
        # The iron-law bullet list must reference MUST-L1, MUST-L3, MUST-L4
        # (MUST-L2 is reproduce-first for fix, not relevant; MUST-L5 is no
        # option-list, also not relevant here).
        for must in ("MUST-L1", "MUST-L3", "MUST-L4"):
            self.assertIn(must, self.text, f"Iron Law must cite {must}")

    def test_hand_off_names_downstream_skill(self):
        m = re.search(r"## Next step(.*?)$", self.text, re.DOTALL)
        self.assertIsNotNone(m, "Next step section missing")
        block = m.group(1)
        self.assertRegex(
            block, r"/dev-kit:\w+",
            "Next step should route to a slash skill",
        )

    def test_no_edit_tool_allowed(self):
        # refactor is an orchestrator; source edits belong to phase 2
        # The cleanup pass mutates source; the refactor orchestrator itself
        # must not have Edit.
        m = re.search(r"^disallowed-tools:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "disallowed-tools: frontmatter missing")
        tools = m.group(1).split()
        self.assertIn(
            "Edit", tools,
            "refactor must declare Edit in disallowed-tools (phase 2 mutates, not this skill)",
        )

    def test_disambiguates_from_prune(self):
        # refactor rewrites code; prune deletes it. The body must surface
        # the distinction so a future edit doesn't accidentally make
        # refactor a deletion skill.
        self.assertIn(
            "/dev-kit:prune", self.text,
            "refactor must mention /dev-kit:prune as the deletion counterpart",
        )
        self.assertIn(
            "prune --target", self.text,
            "refactor must mention `prune --target` as the single-feature deletion skill",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
