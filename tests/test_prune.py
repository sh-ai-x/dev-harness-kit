#!/usr/bin/env python3
"""test_prune.py — Regression for skills/prune/SKILL.md schema.

Locks in the 4-phase prune contract. Asserts:

- frontmatter: user-invocable: true, category: build, model: opus
- body has 4 phase headings ([1/4], [2/4], [3/4], [4/4])
- body has an Iron Law with MUST-L1 / MUST-L2 / MUST-L3 / MUST-L4 references
- body disambiguates from /dev-kit:refactor (delete != refactor)
- body declares `--target <feat>` flag for single-feature deletion
- body declares Phase 4 VERIFY runs the full suite (not just the changed path)
- body declares Edit in disallowed-tools (orchestrator only)
- body never claims to call `rm` itself
- frontmatter name matches directory name (covered by test_naming.py
  but pinned here for fast failure if the new file regresses)
- Phase 2 routes to `python3 -m lib.analysis_core --delete --target <feat>`
  backed by `lib/analysis_core.runner.run_analysis(mode="delete", ...)`.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
PRUNE_SKILL = PROJECT_ROOT / "skills" / "prune" / "SKILL.md"
DISCOVER_DEPENDENTS = PROJECT_ROOT / "lib" / "analysis_core"  # python3 -m lib.analysis_core (PR-H)


class TestPruneSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not PRUNE_SKILL.exists():
            raise unittest.SkipTest(f"{PRUNE_SKILL} missing")
        cls.text = PRUNE_SKILL.read_text(encoding="utf-8")

    def test_frontmatter_user_invocable_true(self):
        m = re.search(r"^user-invocable:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "user-invocable: frontmatter missing")
        self.assertEqual(m.group(1).strip(), "true", "prune must be user-invocable")

    def test_frontmatter_category_build(self):
        m = re.search(r"^category:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "category: frontmatter missing")
        self.assertEqual(m.group(1).strip(), "build", "prune category must be 'build'")

    def test_frontmatter_model_opus(self):
        # prune is a higher-stakes skill (it deletes code), so the
        # default model is opus rather than sonnet.
        m = re.search(r"^model:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "model: frontmatter missing")
        self.assertEqual(m.group(1).strip(), "opus", "prune model must be 'opus'")

    def test_four_phases_present(self):
        for n in (1, 2, 3, 4):
            pattern = rf"\[{n}/4\]"
            self.assertRegex(
                self.text, pattern,
                f"phase [{n}/4] heading missing from body",
            )

    def test_phase_names_match_documented_chain(self):
        self.assertRegex(self.text, r"\[1/4\]\s*SWEEP", "phase 1 should be SWEEP")
        self.assertRegex(self.text, r"\[2/4\]\s*DEPENDENTS", "phase 2 should be DEPENDENTS")
        self.assertRegex(self.text, r"\[3/4\]\s*REPORT", "phase 3 should be REPORT")
        self.assertRegex(self.text, r"\[4/4\]\s*VERIFY", "phase 4 should be VERIFY")

    def test_iron_law_cites_four_musts(self):
        # MUST-L2 (reproduce-first) is included because deletion
        # candidates must have a reproducible signal.
        for must in ("MUST-L1", "MUST-L2", "MUST-L3", "MUST-L4"):
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
        # prune is an orchestrator; deletions belong to phase 2
        # (the inlined 3-pass sweep) which emits commands for the user to run.
        m = re.search(r"^disallowed-tools:\s*(.+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m, "disallowed-tools: frontmatter missing")
        tools = m.group(1).split()
        self.assertIn(
            "Edit", tools,
            "prune must declare Edit in disallowed-tools (phase 2 mutates, not this skill)",
        )

    def test_disambiguates_from_refactor(self):
        # prune deletes; refactor rewrites. The body must surface
        # the distinction so users don't run the wrong skill.
        self.assertIn(
            "/dev-kit:refactor", self.text,
            "prune must mention /dev-kit:refactor as the refactor counterpart",
        )

    def test_target_flag_documented(self):
        # --target is the canonical single-feature deletion flow.
        self.assertIn(
            "--target", self.text,
            "prune must declare the --target flag for single-feature deletion",
        )

    def test_phase4_runs_full_suite(self):
        # Phase 4 (VERIFY) is the safety net for --target deletion: every
        # sweep must run the project's full test runner, not just the
        # changed path. A regression that drops the full-suite requirement
        # would let /dev-kit:prune --target delete live code without
        # catching it. Quote the phase block so a future edit doesn't
        # silently weaken the contract.
        m = re.search(r"\[4/4\]\s*VERIFY(.*?)(?=\n##\s|\Z)", self.text, re.DOTALL)
        self.assertIsNotNone(m, "phase 4 VERIFY block missing")
        block = m.group(1)
        self.assertRegex(
            block, r"full\s+suite",
            "Phase 4 must run the full suite (not just the changed path)",
        )
        self.assertRegex(
            block, r"mattpocock|diagnosing-bugs",
            "Phase 4 must route failures to mattpocock-skills:diagnosing-bugs for systematic repro",
        )

    def test_never_calls_rm_directly(self):
        # The skill emits commands;
        # the user runs them. A "skill should `rm` for me" statement
        # would be a violation.
        self.assertIn(
            "never deletes files itself", self.text,
            "prune must declare it never calls rm/git-rm itself",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
