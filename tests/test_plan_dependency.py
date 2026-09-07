#!/usr/bin/env python3
"""Tests for lib/plan_dependency.py — DAG validator for plan-skill Gate 4/5.

Covers:
  - empty / no-edges -> valid, topo == N-order
  - linear chain A->B->C -> valid, topo == [A,B,C]
  - cycle A->B->A -> invalid, cycles non-empty
  - reference to missing step -> invalid, missing non-empty
  - self-loop (A->A) -> cycle
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from plan_dependency import compute_dag  # noqa: E402


def _step(n: int, *, depends_on: list[int] | None = None) -> dict:
    """Build a minimal step dict shaped like `phases/<phase>/index.json`."""
    out: dict = {"step": n, "name": f"step{n}", "status": "pending"}
    if depends_on is not None:
        out["depends_on"] = list(depends_on)
    return out


class TestComputeDagHappyPath(unittest.TestCase):
    def test_empty_no_edges_is_valid(self):
        steps = [_step(1), _step(2), _step(3)]
        result = compute_dag(steps)
        self.assertTrue(result["valid"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["topo"], [1, 2, 3])

    def test_empty_steps_is_valid(self):
        result = compute_dag([])
        self.assertTrue(result["valid"])
        self.assertEqual(result["topo"], [])

    def test_linear_chain_topological_order(self):
        # 3 depends on 2; 2 depends on 1.
        steps = [
            _step(1),
            _step(2, depends_on=[1]),
            _step(3, depends_on=[2]),
        ]
        result = compute_dag(steps)
        self.assertTrue(result["valid"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["topo"], [1, 2, 3])

    def test_diamond_dependency(self):
        # 4 depends on 2 and 3; 2 and 3 both depend on 1.
        steps = [
            _step(1),
            _step(2, depends_on=[1]),
            _step(3, depends_on=[1]),
            _step(4, depends_on=[2, 3]),
        ]
        result = compute_dag(steps)
        self.assertTrue(result["valid"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["topo"], [1, 2, 3, 4])


class TestComputeDagCycles(unittest.TestCase):
    def test_two_node_cycle(self):
        # 2 depends on 1, 1 depends on 2 -> cycle.
        steps = [
            _step(1, depends_on=[2]),
            _step(2, depends_on=[1]),
        ]
        result = compute_dag(steps)
        self.assertFalse(result["valid"])
        self.assertEqual(result["missing"], [])
        self.assertTrue(len(result["cycles"]) >= 1)

    def test_three_node_cycle(self):
        steps = [
            _step(1, depends_on=[3]),
            _step(2, depends_on=[1]),
            _step(3, depends_on=[2]),
        ]
        result = compute_dag(steps)
        self.assertFalse(result["valid"])
        self.assertEqual(result["missing"], [])
        self.assertTrue(len(result["cycles"]) >= 1)

    def test_self_loop(self):
        steps = [_step(1, depends_on=[1])]
        result = compute_dag(steps)
        self.assertFalse(result["valid"])
        self.assertTrue(len(result["cycles"]) >= 1)


class TestComputeDagMissing(unittest.TestCase):
    def test_reference_to_unknown_step(self):
        steps = [
            _step(1),
            _step(2, depends_on=[1, 99]),  # 99 doesn't exist
        ]
        result = compute_dag(steps)
        self.assertFalse(result["valid"])
        self.assertIn(99, result["missing"])

    def test_only_unknown_reference(self):
        steps = [_step(1, depends_on=[7])]
        result = compute_dag(steps)
        self.assertFalse(result["valid"])
        self.assertIn(7, result["missing"])
        self.assertEqual(result["topo"], [])


class TestComputeDagShape(unittest.TestCase):
    """The result dict's keys are stable across all paths."""

    def test_keys_present_for_all_branches(self):
        for steps in (
            [],
            [_step(1)],
            [_step(1, depends_on=[1])],
            [_step(1, depends_on=[99])],
        ):
            result = compute_dag(steps)
            self.assertEqual(
                set(result.keys()),
                {"valid", "missing", "cycles", "topo"},
            )


if __name__ == "__main__":
    unittest.main()
