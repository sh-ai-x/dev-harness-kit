#!/usr/bin/env python3
"""test_evaluation_rubrics.py — Phase 3 rubric + judge prompt coverage.

Tests that the new eval/rubrics/*.yaml and eval/prompts/judge-*.md files
ship with the right shape:

  - eval/rubrics/harness-quality.yaml   (#364)
  - eval/rubrics/os-quality.yaml        (#365)
  - eval/prompts/judge-harness-quality.md (#364)
  - eval/prompts/judge-os-quality.md     (#365)

Each rubric must declare the same axis set as
`lib/llm_judge.py:DIM_AXES[<dim>]`, and each judge prompt must render
without error when substituted via `llm_judge.format_prompt`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import llm_judge  # noqa: E402

RUBRICS_DIR = PROJECT_ROOT / "eval" / "rubrics"
PROMPTS_DIR = PROJECT_ROOT / "eval" / "prompts"


class TestHarnessQualityRubric(unittest.TestCase):
    """eval/rubrics/harness-quality.yaml (#364)."""

    @classmethod
    def setUpClass(cls):
        cls.path = RUBRICS_DIR / "harness-quality.yaml"
        cls.data = yaml.safe_load(cls.path.read_text(encoding="utf-8"))

    def test_file_exists(self):
        self.assertTrue(self.path.exists(), f"missing rubric: {self.path}")

    def test_schema_metadata(self):
        self.assertEqual(self.data["rubric_name"], "harness-quality")
        self.assertEqual(self.data["dim"], "harness")
        self.assertEqual(self.data["schema_version"], 1)

    def test_axes_match_dim_axes(self):
        # Lockstep contract: rubric axes == DIM_AXES["harness"].
        self.assertEqual(
            set(self.data["axes"].keys()),
            set(llm_judge.DIM_AXES["harness"]),
        )

    def test_each_axis_has_description(self):
        for name, body in self.data["axes"].items():
            self.assertIn(
                "description", body,
                f"axis {name!r} missing description",
            )
            self.assertTrue(
                body["description"].strip(),
                f"axis {name!r} description is empty",
            )


class TestOsQualityRubric(unittest.TestCase):
    """eval/rubrics/os-quality.yaml (#365)."""

    @classmethod
    def setUpClass(cls):
        cls.path = RUBRICS_DIR / "os-quality.yaml"
        cls.data = yaml.safe_load(cls.path.read_text(encoding="utf-8"))

    def test_file_exists(self):
        self.assertTrue(self.path.exists(), f"missing rubric: {self.path}")

    def test_schema_metadata(self):
        self.assertEqual(self.data["rubric_name"], "os-quality")
        self.assertEqual(self.data["dim"], "os")
        self.assertEqual(self.data["schema_version"], 1)

    def test_axes_match_dim_axes(self):
        self.assertEqual(
            set(self.data["axes"].keys()),
            set(llm_judge.DIM_AXES["os"]),
        )

    def test_each_axis_has_description(self):
        for name, body in self.data["axes"].items():
            self.assertIn(
                "description", body,
                f"axis {name!r} missing description",
            )
            self.assertTrue(
                body["description"].strip(),
                f"axis {name!r} description is empty",
            )


class TestJudgeHarnessQualityPrompt(unittest.TestCase):
    """eval/prompts/judge-harness-quality.md (#364)."""

    def test_file_exists(self):
        self.assertTrue((PROMPTS_DIR / "judge-harness-quality.md").exists())

    def test_renders_with_substitutions(self):
        text = llm_judge.format_prompt(
            PROJECT_ROOT, "judge-harness-quality.md",
            {
                "CASE_ID": "case-1",
                "DIM": "harness",
                "INPUT": "code",
                "AGENT_OUTPUT": "{}",
                "EXPECTED": "{}",
            },
        )
        self.assertIn("case-1", text)
        self.assertIn("harness", text)
        # Each axis name must appear in the rendered prompt so the
        # judge knows which keys to return.
        for ax in llm_judge.DIM_AXES["harness"]:
            self.assertIn(ax, text, f"axis {ax!r} missing from prompt")


class TestJudgeOsQualityPrompt(unittest.TestCase):
    """eval/prompts/judge-os-quality.md (#365)."""

    def test_file_exists(self):
        self.assertTrue((PROMPTS_DIR / "judge-os-quality.md").exists())

    def test_renders_with_substitutions(self):
        text = llm_judge.format_prompt(
            PROJECT_ROOT, "judge-os-quality.md",
            {
                "CASE_ID": "case-1",
                "DIM": "os",
                "INPUT": "code",
                "AGENT_OUTPUT": "{}",
                "EXPECTED": "{}",
            },
        )
        self.assertIn("case-1", text)
        self.assertIn("os", text)
        for ax in llm_judge.DIM_AXES["os"]:
            self.assertIn(ax, text, f"axis {ax!r} missing from prompt")


class TestRubricRegistryShape(unittest.TestCase):
    """The two new rubrics + prompts together form a registry-coherent
    pair. Sanity-check that a caller could register them with
    `RubricRegistry` and look them up again."""

    def test_harness_pair_roundtrip(self):
        import eval_runner
        eval_runner.RubricRegistry.clear()
        try:
            eval_runner.RubricRegistry.register(
                "harness-quality",
                str(RUBRICS_DIR / "harness-quality.yaml"),
                str(PROMPTS_DIR / "judge-harness-quality.md"),
            )
            self.assertEqual(
                eval_runner.RubricRegistry.get_rubric("harness-quality"),
                str(RUBRICS_DIR / "harness-quality.yaml"),
            )
        finally:
            eval_runner.RubricRegistry.clear()

    def test_os_pair_roundtrip(self):
        import eval_runner
        eval_runner.RubricRegistry.clear()
        try:
            eval_runner.RubricRegistry.register(
                "os-quality",
                str(RUBRICS_DIR / "os-quality.yaml"),
                str(PROMPTS_DIR / "judge-os-quality.md"),
            )
            self.assertEqual(
                eval_runner.RubricRegistry.get_rubric("os-quality"),
                str(RUBRICS_DIR / "os-quality.yaml"),
            )
        finally:
            eval_runner.RubricRegistry.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
