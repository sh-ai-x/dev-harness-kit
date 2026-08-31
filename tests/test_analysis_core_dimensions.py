#!/usr/bin/env python3
"""test_analysis_core_dimensions.py — Registry SSOT for analysis-core.

Locks in the named analysis dimensions the engine knows how to fan out
across. Adding/removing/renaming a dimension is a breaking change for the
6 skills (review/security/inspect/audit/prune/refactor) that delegate to
`run_analysis(dimensions=[...])`.

Asserts:
- the registry exposes exactly the documented names
- each entry has the structured fields the engine consumes (charter,
  shared contract fragment, severity default, mode-eligibility)
- unknown dimensions raise a clear error
- groups resolve correctly (review/security/inspect/audit)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.analysis_core import dimensions as dim_mod  # noqa: E402

# Names that the 6 SKILL.md bodies reference; if any are removed the
# skills' --dim flags and hand-off tables become unreachable.
EXPECTED_DIM_NAMES = frozenset({
    # review core
    "correctness", "security", "architecture",
    # security OWASP Top 10
    "owasp-a01", "owasp-a02", "owasp-a03", "owasp-a04", "owasp-a05",
    "owasp-a06", "owasp-a07", "owasp-a08", "owasp-a09", "owasp-a10",
    # security prompt-injection (LLM01 — separate from OWASP per L9)
    "prompt-injection",
    # inspect health
    "dead", "dup", "smell", "overeng", "overarch", "cleancode",
    "tokenbudget", "slop",
    # audit cross-cutting
    "secret",
})


class TestDimensionRegistry(unittest.TestCase):
    def test_registry_contains_expected_names(self):
        registry = dim_mod.REGISTRY
        self.assertEqual(
            set(registry.keys()), EXPECTED_DIM_NAMES,
            f"registry drift: missing={EXPECTED_DIM_NAMES - set(registry)}, "
            f"extra={set(registry) - EXPECTED_DIM_NAMES}",
        )

    def test_each_entry_has_required_fields(self):
        registry = dim_mod.REGISTRY
        for name, dim in registry.items():
            self.assertEqual(dim.name, name, f"key/name mismatch for {name}")
            self.assertIsInstance(dim.charter, str, f"{name}: charter must be str")
            self.assertGreater(
                len(dim.charter), 20,
                f"{name}: charter too short ({len(dim.charter)} chars); "
                "engine needs enough text to build the expert prompt",
            )
            self.assertIn(
                dim.family, {"review", "security", "inspect", "audit"},
                f"{name}: family {dim.family!r} not in known families",
            )
            self.assertIsInstance(
                dim.contract_fields, tuple,
                f"{name}: contract_fields must be a tuple (immutable)",
            )
            self.assertGreater(
                len(dim.contract_fields), 0,
                f"{name}: contract_fields must list required output keys",
            )
            for sev in dim.severity_floor:
                self.assertIn(
                    sev, {"critical", "major", "minor", "nit"},
                    f"{name}: severity_floor has unknown value {sev!r}",
                )
            self.assertIn(
                dim.mode, {"read-only", "delete", "rewrite"},
                f"{name}: mode {dim.mode!r} not in known modes",
            )

    def test_owasp_dimensions_are_ten(self):
        owasp = [n for n in dim_mod.REGISTRY if n.startswith("owasp-a")]
        self.assertEqual(
            len(owasp), 10,
            f"expected 10 OWASP dims (A01..A10), got {len(owasp)}: {sorted(owasp)}",
        )

    def test_get_dimension_known(self):
        d = dim_mod.get("correctness")
        self.assertEqual(d.name, "correctness")
        self.assertEqual(d.family, "review")

    def test_get_dimension_unknown_raises(self):
        with self.assertRaises(KeyError) as ctx:
            dim_mod.get("not-a-dim")
        self.assertIn("not-a-dim", str(ctx.exception))

    def test_resolve_accepts_strings(self):
        out = dim_mod.resolve(["correctness", "security"])
        self.assertEqual([d.name for d in out], ["correctness", "security"])

    def test_resolve_accepts_dim_objects(self):
        out = dim_mod.resolve([dim_mod.get("correctness")])
        self.assertEqual(out[0].name, "correctness")

    def test_resolve_unknown_raises(self):
        with self.assertRaises(KeyError):
            dim_mod.resolve(["correctness", "bogus"])

    def test_group_review(self):
        names = {d.name for d in dim_mod.group("review")}
        self.assertEqual(names, {"correctness", "security", "architecture"})

    def test_group_security(self):
        names = {d.name for d in dim_mod.group("security")}
        # 10 OWASP Top 10 + 1 separate prompt-injection (LLM01) dimension.
        # OWASP Top 10 is the symbolic surface; prompt-injection sits next
        # to it (not as A11) per iron-laws/index.md L9.
        self.assertEqual(len(names), 11)
        owasp = {n for n in names if n.startswith("owasp-")}
        self.assertEqual(len(owasp), 10, f"OWASP set drifted: {sorted(owasp)}")
        self.assertIn("prompt-injection", names)

    def test_group_inspect(self):
        names = {d.name for d in dim_mod.group("inspect")}
        self.assertEqual(
            names,
            {"dead", "dup", "smell", "overeng", "overarch",
             "cleancode", "tokenbudget", "slop"},
        )

    def test_group_audit(self):
        names = {d.name for d in dim_mod.group("audit")}
        self.assertTrue({"slop", "secret"}.issubset(names))

    def test_unknown_group_raises(self):
        with self.assertRaises(KeyError):
            dim_mod.group("nope")


class TestDimensionPromptContract(unittest.TestCase):
    """The prompt contract must be uniform across all dims so the engine
    can fan out one Agent call per dim without per-dim boilerplate."""

    def test_all_dims_share_failure_scenario_field(self):
        registry = dim_mod.REGISTRY
        for name, dim in registry.items():
            self.assertIn(
                "failure_scenario", dim.contract_fields,
                f"{name}: must require failure_scenario (precision-over-recall gate)",
            )

    def test_all_dims_share_severity_field(self):
        registry = dim_mod.REGISTRY
        for name, dim in registry.items():
            self.assertIn(
                "severity", dim.contract_fields,
                f"{name}: must require severity",
            )

    def test_all_dims_share_confidence_field(self):
        registry = dim_mod.REGISTRY
        for name, dim in registry.items():
            self.assertIn(
                "confidence", dim.contract_fields,
                f"{name}: must require confidence",
            )



class TestDimensionImmutability(unittest.TestCase):
    """Dimension must be a frozen dataclass — the runner MUST NEVER try
    to mutate one in place; use `dataclasses.replace` to derive variants.
    Mutation attempts must raise `dataclasses.FrozenInstanceError`.
    """

    def test_dimension_is_frozen(self):
        import dataclasses
        dim = dim_mod.get("correctness")
        # Assignment to a frozen dataclass field raises FrozenInstanceError.
        with self.assertRaises(dataclasses.FrozenInstanceError):
            dim.name = "other"
        # dataclasses.replace still works (returns a fresh instance).
        replaced = dataclasses.replace(dim, name="renamed")
        self.assertEqual(replaced.name, "renamed")
        self.assertIsNot(replaced, dim)
        # Original is unchanged.
        self.assertEqual(dim.name, "correctness")

    def test_all_dimensions_are_frozen(self):
        # Every dimension in the registry must carry `__dataclass_params__.frozen=True`,
        # not just the one we touched in test_dimension_is_frozen. Spoofing
        # would otherwise silently bypass the mutation gate.
        for name, dim in dim_mod.REGISTRY.items():
            self.assertTrue(
                hasattr(dim, "__dataclass_params__") and dim.__dataclass_params__.frozen,
                f"{name}: dimension must be a frozen dataclass",
            )
        # Spot-check slots=True too so memory invariants are explicit.
        # __dataclass_params__.slots is only present in Python 3.12+.
        # Checking __slots__ on the instance is portable across all
        # Python 3.x versions that dataclass supports.
        for name, dim in dim_mod.REGISTRY.items():
            self.assertTrue(
                hasattr(dim, "__slots__") and dim.__slots__,
                f"{name}: dimension must use __slots__ (from slots=True)",

            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
