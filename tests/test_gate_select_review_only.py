"""test_gate_select_review_only.py — pin the review-only split (#823).

Issue #823 acceptance criteria:

  - ci-setup ships review.yml + security.yml as independent templates.
  - gate-select's `review only` pick installs ONLY review.yml (no
    security.yml), without leaving the security skill disabled.
  - gate-select's `review + security` pick installs BOTH.
  - gate-select's `show` reports each workflow's presence/absence
    independently — no lie when only one of the two is installed.

This test pins those structural facts. If any of them regress, the
expectations below fail at module-import time (TDD RED) so the build
loop never lands a half-split state.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import ci_setup  # noqa: E402  (after sys.path manipulation)


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


class TestTemplatesExistOnDisk(unittest.TestCase):
    """Both templates must exist in templates/ci/.github/workflows/."""

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
            f"security.yml template missing: {path} — #823 split incomplete",
        )


class TestSecurityTemplateIsSelfContained(unittest.TestCase):
    """security.yml must NOT reference the review job (the split's reason)."""

    def test_security_template_no_review_job(self) -> None:
        path = PROJECT_ROOT / "templates/ci/.github/workflows/security.yml"
        body = path.read_text(encoding="utf-8")
        # The original bundled review.yml had both `review:` and `security:`
        # jobs. Post-#823 each workflow has exactly one of the two.
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


class TestCiSetupInstallsBothWorkflows(unittest.TestCase):
    """End-to-end default install into a tmpdir consumer repo copies both templates."""

    def test_install_copies_review_and_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            review_target = target / ".github/workflows/review.yml"
            security_target = target / ".github/workflows/security.yml"

            self.assertTrue(
                review_target.is_file(),
                f"review.yml not installed at {review_target}",
            )
            self.assertTrue(
                security_target.is_file(),
                f"security.yml not installed at {security_target}",
            )


class TestMarkerListsBothWorkflows(unittest.TestCase):
    """The ci-config marker records the source paths of both workflows (default install)."""

    def test_marker_records_review_and_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            marker = target / ci_setup.MARKER_REL
            self.assertTrue(marker.is_file(), f"marker missing: {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))

            runners = payload.get("runners", [])
            self.assertIn(
                "review.yml",
                runners,
                f"marker missing review.yml entry in runners: {runners}",
            )
            self.assertIn(
                "security.yml",
                runners,
                f"marker missing security.yml entry in runners: {runners}",
            )


class TestCiSetupReviewOnlyExclude(unittest.TestCase):
    """Issue #823 acceptance: `exclude=security.yml` lands review.yml but
    leaves security.yml absent, and the marker `runners` list reflects
    only review.yml — NOT security.yml.

    This is the headline acceptance criterion the original PR's gate
    reviewer flagged: gate-select's `review only` pick must actually drop
    security.yml from the install set (and the marker `runners` list),
    not just print a status line.
    """

    def test_install_with_exclude_drops_security_yml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(
                target, force=True,
                exclude=frozenset({"security.yml"}),
            )
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            review_target = target / ".github/workflows/review.yml"
            security_target = target / ".github/workflows/security.yml"

            self.assertTrue(
                review_target.is_file(),
                f"review.yml not installed at {review_target} — review only pick must land review.yml",
            )
            self.assertFalse(
                security_target.is_file(),
                f"security.yml installed at {security_target} — review only pick must NOT land security.yml",
            )

    def test_marker_runners_reflects_exclude(self) -> None:
        """The marker's `runners` list must NOT include security.yml when
        the install excluded it. This is the structural acceptance bullet
        the gate reviewer flagged: a `review only` consumer must NOT have
        security.yml in its marker `runners` field.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(
                target, force=True,
                exclude=frozenset({"security.yml"}),
            )
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            marker = target / ci_setup.MARKER_REL
            self.assertTrue(marker.is_file(), f"marker missing: {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))

            runners = payload.get("runners", [])
            self.assertIn(
                "review.yml",
                runners,
                f"marker missing review.yml entry in runners: {runners}",
            )
            self.assertNotIn(
                "security.yml",
                runners,
                f"marker still records security.yml in runners after exclude: {runners}",
            )

    def test_idempotent_reinstall_with_exclude_stays_clean(self) -> None:
        """Re-running install with the same exclude filter must remain a
        no-op (no spurious file copies / marker rewrites). This guards
        the `_is_already_installed(paths_to_check=...)` filter.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            exclude = frozenset({"security.yml"})
            r1 = ci_setup.install_ci_config(target, force=True, exclude=exclude)
            self.assertEqual(r1.errors, [], f"install errors: {r1.errors}")
            r2 = ci_setup.install_ci_config(target, force=False, exclude=exclude)
            self.assertEqual(r2.errors, [], f"reinstall errors: {r2.errors}")
            # Re-install should not have created/overwritten anything —
            # the no-op detection honored the filtered path set.
            self.assertEqual(
                r2.created, [],
                f"reinstall created files despite filter: {r2.created}",
            )
            self.assertEqual(
                r2.overwritten, [],
                f"reinstall overwrote files despite filter: {r2.overwritten}",
            )
            # Marker must still record only review.yml in runners.
            marker = target / ci_setup.MARKER_REL
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertNotIn("security.yml", payload.get("runners", []))


class TestGateSelectPickDispatchesExclude(unittest.TestCase):
    """Pin the gate-select SKILL.md dispatch: the `review` AI-judge pick
    passes `exclude=security.yml` to ci-setup. The check is structural
    (string in SKILL.md) because the dispatch runs via the Skill tool
    in production — testing the dispatch directly would require
    mocking the Skill tool, which is heavier than the gate's contract.
    """

    def setUp(self) -> None:
        self.skill_text = (PROJECT_ROOT / "skills/gate-select/SKILL.md").read_text()

    def test_review_pick_threads_exclude(self) -> None:
        """The SKILL.md dispatch line for the `review` AI-judge pick
        must include `exclude="security.yml"` so the install actually
        drops security.yml. Without this, the marker `runners` field
        still lists security.yml — gate-reviewer CC-2 finding."""
        # Look for an exclude assignment that triggers on the review pick.
        self.assertIn(
            'exclude_arg = "security.yml" if ai_judge_pick == "review"',
            self.skill_text,
            "gate-select SKILL.md does not thread exclude=security.yml for "
            "the `review` AI-judge pick — #823 acceptance bullet unmet",
        )

    def test_review_plus_security_pick_does_not_exclude(self) -> None:
        """The `review + security` pick must NOT exclude — the marker
        must record both runners. The dispatch logic above gates on
        `ai_judge_pick == "review"` (not "review + security"), so this
        is structurally guaranteed."""
        self.assertNotIn(
            'exclude_arg = "security.yml" if ai_judge_pick == "review + security"',
            self.skill_text,
            "review + security pick must NOT exclude security.yml",
        )


if __name__ == "__main__":
    unittest.main()
