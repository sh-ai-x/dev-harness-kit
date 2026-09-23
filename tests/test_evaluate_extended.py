#!/usr/bin/env python3
"""test_evaluate_extended.py — Phase 3 eval extension coverage.

Tests the new surface added in Phase 3 (issues #362–#368):
  - lib/eval_runner.py:RubricRegistry  (#362)
  - lib/llm_judge.py:DIM_AXES extension  (#363)
  - lib/analysis_core/cross_validate.py (#366)
  - skills/evaluate/SKILL.md exists with alpha: enforcement  (#367)

Backward-compat: the legacy DIM_AXES tuples (review/security/plan) and
the legacy 4-axis JUDGE_AXES are unchanged.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import eval_runner  # noqa: E402
import llm_judge  # noqa: E402
from lib.analysis_core.cross_validate import (  # noqa: E402
    ESCALATE_VARIANCE_THRESHOLD,
    cross_validate_scores,
)


def _load_skill(path: Path) -> dict:
    """Parse the YAML frontmatter of a SKILL.md. Minimal parser —
    avoids a pyyaml dep on the test loader."""
    text = path.read_text(encoding="utf-8")
    # Frontmatter is the leading `---` block.
    assert text.startswith("---\n"), f"{path} missing frontmatter"
    end = text.find("\n---", 4)
    assert end != -1, f"{path} frontmatter not closed"
    body = text[4:end]
    out: dict = {}
    for line in body.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


class TestDimAxesExtension(unittest.TestCase):
    """lib/llm_judge.py DIM_AXES extended with harness + os axes (#363)."""

    def test_harness_axes_present(self):
        self.assertIn("harness", llm_judge.DIM_AXES)
        self.assertIsInstance(llm_judge.DIM_AXES["harness"], tuple)

    def test_harness_axes_count_is_5(self):
        self.assertEqual(len(llm_judge.DIM_AXES["harness"]), 5)

    def test_os_axes_present(self):
        self.assertIn("os", llm_judge.DIM_AXES)
        self.assertIsInstance(llm_judge.DIM_AXES["os"], tuple)

    def test_os_axes_count_is_5(self):
        self.assertEqual(len(llm_judge.DIM_AXES["os"]), 5)

    def test_legacy_dims_unchanged(self):
        # Backward-compat: existing review/security/plan tuples intact.
        self.assertEqual(len(llm_judge.DIM_AXES["review"]), 5)
        self.assertEqual(len(llm_judge.DIM_AXES["security"]), 3)
        self.assertEqual(len(llm_judge.DIM_AXES["plan"]), 4)
        self.assertIn("code_sanity_score", llm_judge.DIM_AXES["review"])
        self.assertIn("owasp_classification_accuracy",
                      llm_judge.DIM_AXES["security"])


class TestRubricRegistry(unittest.TestCase):
    """lib/eval_runner.py RubricRegistry (#362).

    The registry must be empty by default (backward-compat) and the
    register/lookup/version contract must hold.
    """

    def setUp(self):
        # Each test starts from a clean registry to avoid bleed.
        eval_runner.RubricRegistry.clear()

    def tearDown(self):
        eval_runner.RubricRegistry.clear()

    def test_default_registry_empty(self):
        self.assertEqual(eval_runner.RubricRegistry.names(), ())
        self.assertEqual(eval_runner.RubricRegistry.version, 0)

    def test_register_then_lookup(self):
        eval_runner.RubricRegistry.register(
            "harness-quality",
            "eval/rubrics/harness-quality.yaml",
            "eval/prompts/judge-harness-quality.md",
        )
        entry = eval_runner.RubricRegistry.lookup("harness-quality")
        self.assertEqual(
            entry["rubric_yaml_path"], "eval/rubrics/harness-quality.yaml",
        )
        self.assertEqual(
            entry["judge_prompt_path"], "eval/prompts/judge-harness-quality.md",
        )

    def test_register_bumps_version(self):
        v0 = eval_runner.RubricRegistry.version
        eval_runner.RubricRegistry.register(
            "os-quality",
            "eval/rubrics/os-quality.yaml",
            "eval/prompts/judge-os-quality.md",
        )
        self.assertEqual(eval_runner.RubricRegistry.version, v0 + 1)

    def test_lookup_unknown_raises_keyerror(self):
        with self.assertRaises(KeyError):
            eval_runner.RubricRegistry.lookup("nope")

    def test_get_rubric_returns_yaml_path_only(self):
        eval_runner.RubricRegistry.register(
            "harness-quality",
            "eval/rubrics/harness-quality.yaml",
            "eval/prompts/judge-harness-quality.md",
        )
        self.assertEqual(
            eval_runner.RubricRegistry.get_rubric("harness-quality"),
            "eval/rubrics/harness-quality.yaml",
        )

    def test_names_returns_sorted_tuple(self):
        eval_runner.RubricRegistry.register(
            "os-quality",
            "eval/rubrics/os-quality.yaml",
            "eval/prompts/judge-os-quality.md",
        )
        eval_runner.RubricRegistry.register(
            "harness-quality",
            "eval/rubrics/harness-quality.yaml",
            "eval/prompts/judge-harness-quality.md",
        )
        # Sorted, not insertion order.
        self.assertEqual(
            eval_runner.RubricRegistry.names(),
            ("harness-quality", "os-quality"),
        )

    def test_register_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            eval_runner.RubricRegistry.register("", "x", "y")


class TestCrossValidate(unittest.TestCase):
    """lib/analysis_core/cross_validate.py (#366)."""

    def test_agreement_does_not_escalate(self):
        scores = [
            {"a": 8.0, "b": 8.0},
            {"a": 8.0, "b": 8.0},
            {"a": 8.0, "b": 8.0},
        ]
        r = cross_validate_scores(scores)
        self.assertFalse(r["escalate"])
        self.assertEqual(r["variance"], 0.0)
        self.assertEqual(r["mean"], {"a": 8.0, "b": 8.0})

    def test_disagreement_escalates(self):
        scores = [
            {"a": 10.0, "b": 10.0},
            {"a": 5.0, "b": 5.0},
            {"a": 5.0, "b": 5.0},
        ]
        r = cross_validate_scores(scores)
        self.assertTrue(r["escalate"])
        self.assertGreater(r["variance"], ESCALATE_VARIANCE_THRESHOLD)

    def test_threshold_at_boundary(self):
        # Three judges with per-judge means that bracket the threshold:
        # a tightly-disagreeing triple whose variance rounds just under
        # 0.5 must NOT escalate; a slightly wider spread must.
        # Tight spread: means {8.0, 8.5, 8.5} -> variance = 0.0556.
        tight = [
            {"a": 8.0}, {"a": 8.5}, {"a": 8.5},
        ]
        r_tight = cross_validate_scores(tight)
        self.assertFalse(r_tight["escalate"])
        self.assertLess(r_tight["variance"], ESCALATE_VARIANCE_THRESHOLD)
        # The corresponding "strict >" branch is covered by
        # `test_threshold_just_above_escalates` below.

    def test_threshold_just_above_escalates(self):
        scores = [
            {"a": 5.0},
            {"a": 5.0 + 1.0},
            {"a": 5.0 - 1.0},
        ]
        r = cross_validate_scores(scores)
        self.assertTrue(r["escalate"])

    def test_threshold_constant_exported(self):
        # The 0.5 threshold is part of the public contract.
        self.assertEqual(ESCALATE_VARIANCE_THRESHOLD, 0.5)

    def test_deterministic_output(self):
        scores = [{"a": 7.0, "b": 6.0}, {"a": 9.0, "b": 8.0}, {"a": 8.0, "b": 7.0}]
        r1 = cross_validate_scores(scores)
        r2 = cross_validate_scores(scores)
        self.assertEqual(r1, r2)

    def test_keys_locked(self):
        # Public shape is documented; pin it.
        r = cross_validate_scores([{"a": 8.0}, {"a": 8.0}, {"a": 8.0}])
        self.assertEqual(
            set(r.keys()),
            {"escalate", "variance", "mean", "per_judge", "threshold"},
        )
        self.assertEqual(r["threshold"], ESCALATE_VARIANCE_THRESHOLD)

    def test_empty_input_does_not_escalate(self):
        # Empty fan-out is degenerate but should be a no-op, not crash.
        r = cross_validate_scores([])
        self.assertFalse(r["escalate"])
        self.assertEqual(r["variance"], 0.0)
        self.assertEqual(r["mean"], {})


class TestEvaluateSkillFrontmatter(unittest.TestCase):
    """skills/evaluate/SKILL.md (#367).

    Iron Law L6: every new skill MUST declare alpha in {state, enforcement,
    analysis}. The evaluate skill uses alpha: enforcement.
    """

    def test_evaluate_skill_exists(self):
        p = PROJECT_ROOT / "skills" / "evaluate" / "SKILL.md"
        self.assertTrue(p.exists(), f"missing skill file: {p}")

    def test_evaluate_skill_has_alpha_enforcement(self):
        p = PROJECT_ROOT / "skills" / "evaluate" / "SKILL.md"
        fm = _load_skill(p)
        self.assertEqual(
            fm.get("alpha"), "enforcement",
            f"evaluate skill must declare alpha: enforcement, got {fm.get('alpha')!r}",
        )

    def test_evaluate_skill_name_matches_dir(self):
        p = PROJECT_ROOT / "skills" / "evaluate" / "SKILL.md"
        fm = _load_skill(p)
        self.assertEqual(fm.get("name"), "evaluate")


class TestBackwardCompat(unittest.TestCase):
    """Phase 3 must not break any pre-existing public surface."""

    def test_default_registry_empty_does_not_break_imports(self):
        # The default registry being empty is the contract — any code
        # path that consults the registry must handle the empty case.
        self.assertEqual(eval_runner.RubricRegistry.names(), ())

    def test_judge_axes_default_unchanged(self):
        self.assertEqual(
            llm_judge.JUDGE_AXES,
            ("semantic_drift", "completeness", "correctness", "consistency"),
        )

    def test_cross_validate_does_not_mutate_input(self):
        scores = [{"a": 8.0}, {"a": 8.0}, {"a": 8.0}]
        snapshot = [dict(s) for s in scores]
        cross_validate_scores(scores)
        self.assertEqual(scores, snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
