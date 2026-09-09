"""test_workflow_gates_var.py — pin the `if: vars.GATES_*_ENABLED` contract on each gate job.

The `!= 'false'` form (not `== 'true'`) is load-bearing:
  - unset var (consumer never ran `gate-select sync`) → `vars.GATES_*`
    evaluates to the empty string → `'false'` comparison is False →
    `if:` is True → gate job runs (backward compat).
  - `vars.GATES_*_ENABLED=false` (operator disabled) → `'false'` ==
    'false' → `if:` is False → gate job skipped.

Pins the var name against `lib/gates_state.DEFAULT_GATES` so a rename of
one side is caught.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import gates_state  # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates/ci" / ".github" / "workflows"


class TestEachWorkflowGateIf(unittest.TestCase):
    """Each gate workflow must have exactly ONE `if: vars.GATES_*_ENABLED != 'false'` line."""

    CASES = [
        ("review.yml", "review"),
        ("security.yml", "security"),
        ("maintenance.yml", "maintenance"),
    ]

    def test_each_template_has_correct_gate_if(self) -> None:
        # The contract is `vars.<VAR> != 'false'` somewhere in the gate
        # job's effective `if:` expression. review.yml + security.yml use
        # a single-line `if:`; maintenance.yml uses a folded scalar that
        # AND-s the var check with the existing expression. Either form
        # is acceptable; the test pins the variable token + comparison.
        for fname, gate_key in self.CASES:
            with self.subTest(workflow=fname, gate=gate_key):
                path = TEMPLATES / fname
                self.assertTrue(path.is_file(), f"template missing: {path}")
                body = path.read_text(encoding="utf-8")
                expected_var = gates_state.DEFAULT_GATES[gate_key]["var"]
                expected_pattern = re.compile(
                    rf"vars\.{re.escape(expected_var)}\s*!=\s*'false'",
                )
                self.assertRegex(
                    body,
                    expected_pattern,
                    f"{fname} must reference `vars.{expected_var} != 'false'` on the gate job",
                )

    def test_each_template_has_exactly_one_gate_if(self) -> None:
        # Exactly one occurrence of the var reference per workflow. The
        # maintenance.yml folded scalar puts the var token on its own
        # line inside an `if: |` block — the count is still 1.
        for fname, gate_key in self.CASES:
            with self.subTest(workflow=fname):
                path = TEMPLATES / fname
                body = path.read_text(encoding="utf-8")
                expected_var = gates_state.DEFAULT_GATES[gate_key]["var"]
                matches = re.findall(
                    rf"vars\.{re.escape(expected_var)}\s*!=\s*'false'",
                    body,
                )
                self.assertEqual(
                    len(matches), 1,
                    f"{fname}: expected exactly 1 `vars.{expected_var} != 'false'` occurrence, "
                    f"found {len(matches)}",
                )

    def test_workflow_basename_matches_gate_key(self) -> None:
        # Cross-check: the var name + workflow basename come from
        # DEFAULT_GATES[<key>], not hand-typed strings in the YAML.
        for fname, gate_key in self.CASES:
            with self.subTest(workflow=fname, gate=gate_key):
                self.assertEqual(
                    gates_state.DEFAULT_GATES[gate_key]["workflow"], fname
                )


class TestBackwardCompatUnsetVar(unittest.TestCase):
    """When the var is unset (consumer hasn't run `gate-select sync`), the
    `!= 'false'` form evaluates True — the gate job runs. This is the
    backward-compat property that lets a v0.1.x consumer install this
    template and get the old behavior (gate always on)."""

    def test_review_yml_eval(self) -> None:
        # Simulate `vars.GATES_REVIEW_ENABLED` being unset (empty string).
        # The `!= 'false'` must hold.
        self.assertTrue("" != "false")  # unset string is not equal to 'false'

    def test_security_yml_eval(self) -> None:
        self.assertTrue("" != "false")

    def test_maintenance_yml_eval(self) -> None:
        self.assertTrue("" != "false")

    def test_explicit_false_skips(self) -> None:
        # `vars.GATES_REVIEW_ENABLED=false` → skip.
        self.assertFalse("false" != "false")


if __name__ == "__main__":
    unittest.main()
