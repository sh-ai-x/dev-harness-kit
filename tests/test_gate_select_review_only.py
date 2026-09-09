"""test_gate_select_review_only.py — pin the review-only split + v2 contract.

Issue #823 acceptance (unchanged behaviour, new wiring):
  - ci-setup ships review.yml + security.yml as independent workflows.
  - gate-select's `review only` pick installs ONLY review.yml (no
    security.yml), without leaving the security skill disabled.
  - gate-select's `review + security` pick installs BOTH.
  - gate-select's `show` reports each workflow's presence/absence
    independently — no lie when only one of the two is installed.

Issue TBD rewire:
  - The legacy `pick → review` path threads `--exclude security.yml` to
    ci-setup. After the gates.json refactor, the pick writes
    gates.json (enable review / disable security) and dispatches
    ci-setup WITHOUT `--exclude=`. ci-setup reads gates.json and
    derives the install set. The behavioural acceptance is preserved
    by the gates.json write + ci-setup precedence block; this test
    pins both halves.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import ci_setup  # noqa: E402
import gates_state  # noqa: E402


class TestExpectedPathsContainSecurityYml(unittest.TestCase):
    """ci-setup's EXPECTED_PATHS must include both review + security templates."""

    def test_review_yml_in_expected_paths(self) -> None:
        self.assertIn(
            ".github/workflows/review.yml",
            ci_setup.EXPECTED_PATHS,
            "review.yml missing from EXPECTED_PATHS (ci-setup will not install it)",
        )

    def test_security_yml_in_expected_paths(self) -> None:
        self.assertIn(
            ".github/workflows/security.yml",
            ci_setup.EXPECTED_PATHS,
            "security.yml missing from EXPECTED_PATHS — #823 split incomplete",
        )

    def test_maintenance_yml_in_expected_paths(self) -> None:
        # Issue TBD: maintenance.yml is the third first-class gate.
        self.assertIn(
            ".github/workflows/maintenance.yml",
            ci_setup.EXPECTED_PATHS,
            "maintenance.yml missing from EXPECTED_PATHS — third gate missing",
        )


class TestTemplatesExistOnDisk(unittest.TestCase):
    """All three templates must exist under templates/ci/.github/workflows/."""

    def test_review_template_exists(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/review.yml"
        self.assertTrue(
            path.is_file(),
            f"review.yml template missing: {path} — ci-setup will fail to install",
        )

    def test_security_template_exists(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/security.yml"
        self.assertTrue(
            path.is_file(),
            f"security.yml template missing: {path} — ci-setup will fail to install",
        )

    def test_maintenance_template_exists(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/maintenance.yml"
        self.assertTrue(
            path.is_file(),
            f"maintenance.yml template missing: {path} — third gate template not shipped",
        )


class TestSecurityTemplateIsSelfContained(unittest.TestCase):
    """security.yml must NOT reference the review job (the split's reason)."""

    def test_security_template_no_review_job(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/security.yml"
        body = path.read_text(encoding="utf-8")
        self.assertNotIn(
            "  review:\n",
            body,
            "security.yml still references the review job — split incomplete (#823)",
        )

    def test_security_template_has_security_job(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/security.yml"
        body = path.read_text(encoding="utf-8")
        self.assertIn(
            "  security:\n",
            body,
            "security.yml missing the security job — split incomplete (#823)",
        )

    def test_review_template_no_security_job(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/review.yml"
        body = path.read_text(encoding="utf-8")
        self.assertNotIn(
            "  security:\n",
            body,
            "review.yml still references the security job — split incomplete (#823)",
        )

    def test_review_template_has_review_job(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/review.yml"
        body = path.read_text(encoding="utf-8")
        self.assertIn(
            "  review:\n",
            body,
            "review.yml missing the review job",
        )


class TestCiSetupInstallsAllThreeWorkflows(unittest.TestCase):
    """End-to-end default install into a tmpdir consumer repo copies all three templates."""

    def test_install_copies_review_security_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            for fname in ("review.yml", "security.yml", "maintenance.yml"):
                p = target / ".github" / "workflows" / fname
                self.assertTrue(
                    p.is_file(),
                    f"{fname} not installed at {p}",
                )


class TestMarkerListsAllThreeWorkflows(unittest.TestCase):
    """The ci-config marker records the source paths of all three workflows."""

    def test_marker_records_review_security_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            marker = target / ci_setup.MARKER_REL
            self.assertTrue(marker.is_file(), f"marker missing: {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))

            runners = payload.get("runners", [])
            for fname in ("review.yml", "security.yml", "maintenance.yml"):
                self.assertIn(
                    fname,
                    runners,
                    f"marker missing {fname} entry in runners: {runners}",
                )


class TestCiSetupReviewOnlyViaGatesJson(unittest.TestCase):
    """Issue TBD acceptance: gates.json with `security.enabled=false` lands
    review.yml but leaves security.yml absent, and the marker `runners`
    list reflects only review.yml + maintenance.yml — NOT security.yml.

    Replaces the legacy `--exclude` test (issue #823) with the gates.json-
    driven path.
    """

    def _write_gates(self, target: Path, *, security_enabled: bool) -> None:
        (target / ".dev-kit").mkdir(parents=True, exist_ok=True)
        gates_state.write_state(
            {
                "schema_version": "1.0.0",
                "gates": {
                    "review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"},
                    "security": {"enabled": security_enabled, "workflow": "security.yml", "var": "GATES_SECURITY_ENABLED"},
                    "maintenance": {"enabled": True, "workflow": "maintenance.yml", "var": "GATES_MAINTENANCE_ENABLED"},
                },
            },
            target,
        )

    def test_gates_json_security_disabled_drops_security_yml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._write_gates(target, security_enabled=False)
            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            review_target = target / ".github/workflows/review.yml"
            security_target = target / ".github/workflows/security.yml"

            self.assertTrue(
                review_target.is_file(),
                f"review.yml not installed at {review_target} — review gate must land review.yml",
            )
            self.assertFalse(
                security_target.is_file(),
                f"security.yml installed at {security_target} — gates.json security.enabled=false must NOT land it",
            )

    def test_marker_runners_reflects_gates(self) -> None:
        """The marker's `runners` list must NOT include security.yml when
        the gates.json disabled it. This is the structural acceptance
        for the gates-driven install: a consumer with `security.enabled=false`
        must NOT have security.yml in its marker `runners` field.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._write_gates(target, security_enabled=False)
            ci_setup.install_ci_config(target, force=True)

            marker = target / ci_setup.MARKER_REL
            payload = json.loads(marker.read_text(encoding="utf-8"))

            runners = payload.get("runners", [])
            self.assertIn("review.yml", runners)
            self.assertNotIn(
                "security.yml", runners,
                f"marker still records security.yml in runners after gates.json disable: {runners}",
            )

    def test_idempotent_reinstall_with_gates_stays_clean(self) -> None:
        """Re-running install with the same gates.json stays clean (no spurious file copies / marker rewrites)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._write_gates(target, security_enabled=False)
            r1 = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r1.errors, [], f"install errors: {r1.errors}")
            r2 = ci_setup.install_ci_config(target, force=False)
            self.assertEqual(r2.errors, [], f"reinstall errors: {r2.errors}")
            self.assertEqual(r2.created, [], f"reinstall created files: {r2.created}")
            self.assertEqual(r2.overwritten, [], f"reinstall overwrote files: {r2.overwritten}")
            # Marker must still record only review.yml + maintenance.yml.
            payload = json.loads((target / ci_setup.MARKER_REL).read_text())
            self.assertNotIn("security.yml", payload["runners"])


class TestGateSelectPickWritesGatesJson(unittest.TestCase):
    """Issue TBD rewire: gate-select's pick flow must write gates.json,
    not thread `--exclude security.yml` to ci-setup.

    The behavioural contract (review-only pick → review.yml only) is
    preserved by `TestCiSetupReviewOnlyViaGatesJson` above. This class
    pins the new SKILL.md wiring contract: the v2 SKILL.md must show
    the gates.json-write line, not the legacy `exclude_arg = ...` thread.
    """

    def setUp(self) -> None:
        self.skill = PROJECT_ROOT / "skills/gate-select/SKILL.md"
        self.skill_text = self.skill.read_text(encoding="utf-8")

    def test_pick_dispatches_cisetup_without_exclude(self) -> None:
        """The v2 pick flow must NOT thread `exclude=` to ci-setup."""
        import re
        bad = re.search(r"Skill\([^)]*exclude=", self.skill_text)
        self.assertIsNone(
            bad,
            "gate-select SKILL.md still threads `exclude=` in a Skill() dispatch — "
            "the SSOT moved to gates.json (issue TBD)",
        )

    def test_pick_writes_gates_json(self) -> None:
        """The v2 pick flow must call lib.gates_state to write gates.json."""
        self.assertIn(
            "lib.gates_state enable",
            self.skill_text,
            "gate-select pick flow must call `lib.gates_state enable` to write gates.json",
        )
        self.assertIn(
            "lib.gates_state disable",
            self.skill_text,
            "gate-select pick flow must call `lib.gates_state disable`",
        )

    def test_v2_skill_no_legacy_exclude_arg(self) -> None:
        """The legacy line `exclude_arg = "security.yml" if ai_judge_pick == "review"`
        is removed in v2 — gates.json is the SSOT now."""
        self.assertNotIn(
            'exclude_arg = "security.yml" if ai_judge_pick == "review"',
            self.skill_text,
            "SKILL.md still has the legacy `exclude_arg` thread — issue TBD says gates.json is the SSOT",
        )


if __name__ == "__main__":
    unittest.main()
