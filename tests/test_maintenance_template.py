"""test_maintenance_template.py — pin the maintenance.yml consumer template.

Verifies that `templates/ci/.github/workflows/maintenance.yml` exists, is
referenced by `_CI_PATHS_BEFORE_HOOKS`, and carries the gate-job `if:`
that `.dev-kit/gates.json:maintenance.enabled=false` reads at runtime.
The plain existence + structural checks are the regression net: a future
edit that drops the `if:` (forgetting the SSOT contract) fails the gate
job's wiring test, and a refactor that drops the file from ci-setup fails
the EXPECTED_PATHS test.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import ci_setup  # noqa: E402

WORKFLOW = PROJECT_ROOT / "templates/ci/.github/workflows/maintenance.yml"


class TestMaintenanceTemplateExists(unittest.TestCase):
    def test_template_file_on_disk(self) -> None:
        self.assertTrue(
            WORKFLOW.is_file(),
            f"maintenance template missing: {WORKFLOW} — ci-setup will not install it",
        )

    def test_template_in_expected_paths(self) -> None:
        self.assertIn(
            ".github/workflows/maintenance.yml",
            ci_setup.EXPECTED_PATHS,
            "maintenance.yml missing from EXPECTED_PATHS — ci-setup will not install it",
        )

    def test_template_in_before_hooks_group(self) -> None:
        # Pin the install grouping: maintenance.yml is a workflow file
        # (.github/workflows/*) and must land in the before-hooks tuple
        # (not after hooks — hooks live at hooks/* and are unrelated).
        # See lib/ci_setup._CI_PATHS_BEFORE_HOOKS for the contract.
        from ci_setup import _CI_PATHS_BEFORE_HOOKS
        self.assertIn(".github/workflows/maintenance.yml", _CI_PATHS_BEFORE_HOOKS)


class TestMaintenanceGateJobIf(unittest.TestCase):
    """The gate job must `if:` on `vars.GATES_MAINTENANCE_ENABLED`.

    The agent/judge job (`maintenance_judge`) intentionally does NOT gate
    on it — running the judge is harmless when the gate job is off (gates
    are not gating agents, they're gating merge decisions). This pins the
    asymmetry: only the `gate:` job skips when the var is `false`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = WORKFLOW.read_text(encoding="utf-8")

    def test_gate_job_has_if(self) -> None:
        # The `gate:` job header must include an `if:` expression that
        # reads `vars.GATES_MAINTENANCE_ENABLED != 'false'`. maintenance.yml
        # uses a folded scalar (`if: |`) so the variable reference lives
        # inside a multi-line block; the regex pins the variable token
        # along with its comparison operator (verbatim literal 'false').
        self.assertRegex(
            self.body,
            re.compile(
                r"vars\.GATES_MAINTENANCE_ENABLED\s*!=\s*'false'",
                re.MULTILINE,
            ),
            "gate job must reference `vars.GATES_MAINTENANCE_ENABLED != 'false'`",
        )

    def test_gate_job_var_matches_lib_gates_state(self) -> None:
        # The var name must equal `lib/gates_state.py:DEFAULT_GATES.maintenance.var`
        # so a future rename of one side is caught by this test.
        from gates_state import DEFAULT_GATES
        expected = DEFAULT_GATES["maintenance"]["var"]
        self.assertIn(
            f"vars.{expected}",
            self.body,
            f"maintenance.yml gate if: must reference vars.{expected}",
        )

    def test_workflow_basename_matches_gate_key(self) -> None:
        # The workflow file is `maintenance.yml`; the gate key in
        # DEFAULT_GATES is `maintenance`. The validator cross-checks these.
        from gates_state import DEFAULT_GATES
        self.assertEqual(DEFAULT_GATES["maintenance"]["workflow"], "maintenance.yml")

    def test_judge_job_is_unaffected(self) -> None:
        # Sanity: the `maintenance_judge` job header should NOT carry a
        # gate if: — the judge must always run so the verdict is posted
        # and the human gate (REVIEW_REQUIRED / CHANGES_REQUESTED) can
        # decide merge even when vars.GATES_MAINTENANCE_ENABLED is 'false'.
        # The substring `vars.GATES_MAINTENANCE_ENABLED` must appear exactly
        # once (on the gate job's folded `if:` block) and never as a
        # standalone `if:` line on the judge job.
        var_occurrences = self.body.count("vars.GATES_MAINTENANCE_ENABLED")
        self.assertEqual(
            var_occurrences, 1,
            f"expected exactly 1 GATES_MAINTENANCE_ENABLED occurrence, found {var_occurrences}",
        )


if __name__ == "__main__":
    unittest.main()
