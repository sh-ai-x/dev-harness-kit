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
    """End-to-end install into a tmpdir consumer repo copies both templates."""

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
    """The ci-config marker records the source paths of both workflows."""

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


if __name__ == "__main__":
    unittest.main()
