#!/usr/bin/env python3
"""test_ci_doctor.py — Tests for `/dev-kit:ci-doctor` audit engine.

Issue #212-D1: the audit must answer "is CI ready?" deterministically,
read-only, with one PASS/FAIL summary. These tests pin every check to
known behavior and exercise both the happy path (after a fresh
`ci-setup` install) and the most common failure modes (missing marker,
missing provider file, corrupt JSON, unknown provider).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load(mod_name: str, file: str):
    """Load `lib/<file>` by path so the test works under pytest and bare
    unittest alike. Mirrors test_ci_setup.py:_load_ci_setup()."""
    spec = importlib.util.spec_from_file_location(mod_name, PROJECT_ROOT / "lib" / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_ci_doctor():
    return _load("ci_doctor", "ci_doctor.py")


def _load_ci_setup():
    return _load("ci_setup", "ci_setup.py")


class TestCiDoctor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cs = _load("ci_setup", "ci_setup.py")
        cls.cd = _load("ci_doctor", "ci_doctor.py")

    def _install(self, target: Path) -> None:
        self.cs.install_ci_config(target)

    def test_audit_passes_after_fresh_install(self):
        """Happy path: `ci-setup` leaves a target that `ci-doctor` audits as PASS.

        Excludes the `gh auth` / `repo context` / `secret set:` rows
        from the ok check because the test environment's gh CLI may be
        installed-but-unauthenticated (typical for CI runners). The
        audit correctly surfaces that as FAIL in production; the test
        asserts only that the install-shape rows (files / marker /
        provider declaration) are PASS — secrets behavior is exercised
        by the per-secret tests below.

        Seeds `.env.example` because a real consumer repo already has
        it (standard convention); the ci-setup install doesn't ship it.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".env.example").write_text(
                "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8",
            )
            r = self.cd.audit(target)
            install_shape_rows = [
                c for c in r.checks
                if not c.label.startswith(("gh auth", "repo context", "secret set:"))
            ]
            failing_shape = [c for c in install_shape_rows if c.state == "FAIL"]
            self.assertEqual(
                failing_shape, [],
                f"install-shape audit failed: {[(c.label, c.state, c.detail) for c in failing_shape]}",
            )

    def test_required_files_check_finds_missing_workflow(self):
        """FAIL row surfaces when a required workflow file is missing."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".github" / "workflows" / "review.yml").unlink()
            r = self.cd.audit(target)
            self.assertFalse(r.ok)
            labels = [c.label for c in r.failing()]
            self.assertTrue(
                any("review.yml" in lbl for lbl in labels),
                f"review.yml missing should FAIL; got: {labels}",
            )

    def test_missing_provider_env_fails(self):
        """No CI_REVIEW_PROVIDER anywhere (env, .env, .env.example) → FAIL."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            # No .env.example seeded here on purpose — this is the
            # negative path. Ensure no env var, no .env, no .env.example.
            old = os.environ.pop("CI_REVIEW_PROVIDER", None)
            try:
                r = self.cd.audit(target)
            finally:
                if old is not None:
                    os.environ["CI_REVIEW_PROVIDER"] = old
            self.assertFalse(r.ok)
            self.assertTrue(
                any("provider declared" in c.label for c in r.failing()),
                "missing provider declaration should FAIL",
            )

    def test_corrupt_marker_fails(self):
        """Zero-byte or non-JSON marker must FAIL (issue #212-A3/E1)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".dev-kit" / "ci-config.json").write_text("not-json{")
            r = self.cd.audit(target)
            self.assertFalse(r.ok)
            self.assertTrue(
                any("marker parseable" in c.label and c.state == "FAIL" for c in r.checks),
                "corrupt marker should FAIL the parseable check",
            )

    def test_unknown_provider_in_env_fails(self):
        """`.env` holds a value not in the catalog ⇒ FAIL."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=gpt5\n", encoding="utf-8")
            r = self.cd.audit(target)
            self.assertFalse(r.ok)
            self.assertTrue(
                any("provider declared" in c.label and c.state == "FAIL" for c in r.checks),
                "unknown provider should FAIL the declared check",
            )

    def test_provider_override_changes_required_secrets(self):
        """`.env:CI_REVIEW_PROVIDER=anthropic` must drive the secrets check
        toward ANTHROPIC_API_KEY not MINIMAX_API_KEY."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=anthropic\n", encoding="utf-8")
            r = self.cd.audit(target)
            declared = [c for c in r.checks if "provider declared" in c.label]
            self.assertEqual(declared[0].state, "PASS")
            self.assertIn("anthropic", declared[0].detail)

    def test_audit_summary_lines_renders_passes_and_fails(self):
        """`summary_lines()` output is suitable for stdout printing."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            r = self.cd.audit(target)
            lines = r.summary_lines()
            self.assertGreater(len(lines), 1)
            self.assertTrue(lines[0].startswith("ci-doctor verdict"))
            # PASS for present files; INFO for marker rows
            joined = "\n".join(lines)
            self.assertIn("PASS", joined)

    def test_audit_handles_target_dir_that_does_not_exist(self):
        """Non-existent target dir produces a single FAIL row (graceful)."""
        r = self.cd.audit(Path("/nonexistent/ci_doctor_test_xyz_987"))
        self.assertFalse(r.ok)
        self.assertEqual(len(r.failing()), 1)
        self.assertEqual(r.failing()[0].label, "target dir")

    def test_doctor_report_dataclass_shape(self):
        """Smoke-check the DoctorReport / Check dataclasses."""
        c = self.cd.Check("foo", "PASS", "ok")
        self.assertEqual(c.row(), "[PASS] foo: ok")
        r = self.cd.DoctorReport()
        r.checks.append(c)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.failing()), 0)

    # --- source-repo detection + consumer-only skip ---------------------

    def _mark_source_repo(self, target: Path) -> None:
        """Make `target` look like the dev-kit plugin authoring source:
        a `.claude-plugin/plugin.json` naming this plugin `dev-kit`."""
        (target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (target / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "dev-kit", "version": "0.0.0"}), encoding="utf-8"
        )

    def test_is_source_repo_true_for_dev_kit_manifest(self):
        """`.claude-plugin/plugin.json` naming dev-kit ⇒ source repo."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._mark_source_repo(target)
            self.assertTrue(self.cd._is_source_repo(target))

    def test_is_source_repo_false_for_consumer(self):
        """A consumer install (no plugin manifest) is not the source repo."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            self.assertFalse(self.cd._is_source_repo(target))

    def test_is_source_repo_false_for_other_plugin(self):
        """A different plugin's manifest is not the dev-kit source repo."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".claude-plugin").mkdir(parents=True)
            (target / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "some-other-plugin"}), encoding="utf-8"
            )
            self.assertFalse(self.cd._is_source_repo(target))

    def test_source_repo_skips_marker_rows(self):
        """In the source repo, the missing consumer marker is SKIP not FAIL."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            # Source repo gitignores `.dev-kit/` — simulate its absence.
            import shutil as _sh
            _sh.rmtree(target / ".dev-kit")
            self._mark_source_repo(target)
            r = self.cd.audit(target)
            marker_rows = [
                c for c in r.checks
                if "ci-config.json" in c.label or c.label.startswith("marker")
            ]
            self.assertTrue(marker_rows, "expected marker/config rows present")
            self.assertTrue(
                all(c.state == "SKIP" for c in marker_rows),
                f"marker rows must be SKIP in source repo; got "
                f"{[(c.label, c.state) for c in marker_rows]}",
            )
            self.assertFalse(
                any("ci-config.json" in c.label and c.state == "FAIL"
                    for c in r.checks),
                "missing consumer marker must not FAIL in source repo",
            )

    def test_source_repo_skips_dev_kit_github_token_secret(self):
        """In the source repo, DEV_KIT_GITHUB_TOKEN is not a required secret.

        Provider API-key secrets are still required (source CI uses them),
        so only the PAT row is skipped. Independent of gh availability:
        the row must never be a FAIL asking to set DEV_KIT_GITHUB_TOKEN.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            self._mark_source_repo(target)
            r = self.cd.audit(target)
            pat_fail = [
                c for c in r.checks
                if "DEV_KIT_GITHUB_TOKEN" in c.label and c.state == "FAIL"
            ]
            self.assertEqual(
                pat_fail, [],
                "DEV_KIT_GITHUB_TOKEN must not FAIL in source repo",
            )


# ------------------------------------------------------------------
    # Workflow diagnostics (WARN/INFO only — verdict-neutral)
    # ------------------------------------------------------------------

    def _write_workflow(self, target: Path, name: str, body) -> Path:
        """Write a hand-crafted workflow YAML to
        `target/.github/workflows/<name>`. Returns the path. Used by the
        diagnostic tests below to feed the scanner crafted shapes
        without going through the full `install_ci_config` path.
        Accepts `str` (utf-8) or `bytes` (raw, for unparseable-garbage tests).
        """
        p = target / ".github" / "workflows" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")
        return p

    def _minimal_install(self, target: Path) -> None:
        """Minimal install shape that satisfies `_check_required_files`
        so the diagnostic rows dominate the report. Writes a marker +
        provider declaration (.env.example stub) + stub workflows so
        file-present + marker + provider rows are PASS; only the
        diagnostic-under-test is exercising a path. Each test that
        exercises a specific workflow's diagnostics overwrites the stub
        with its own body via `_write_workflow`."""
        (target / ".github").mkdir(parents=True, exist_ok=True)
        (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (target / ".env.example").write_text(
            "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8",
        )
        (target / ".dev-kit").mkdir(parents=True, exist_ok=True)
        (target / ".dev-kit" / "ci-config.json").write_text(
            json.dumps({
                "schema_version": 1,
                "installed_at": "2026-01-01T00:00:00Z",
                "provider_env_key": "CI_REVIEW_PROVIDER",
            }),
            encoding="utf-8",
        )
        self._write_workflow(target, "review.yml", (
            "on:\n  pull_request:\njobs:\n  review:\n"
            "    name: review\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"
        ))
        self._write_workflow(target, "auto-fix-pr.yml", (
            "on:\n  pull_request_review:\n    types: [submitted]\njobs:\n"
            "  auto-fix:\n    name: auto-fix\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo\n"
        ))
        self._write_workflow(target, "ci.yml", (
            "on:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n"
            "  test:\n    name: test\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"
        ))

    def _diagnostic_rows(self, r, label_substr: str) -> list:
        """Filter `r.checks` to rows whose label contains `label_substr`."""
        return [c for c in r.checks if label_substr in c.label]

    def test_workflow_diagnostics_warns_on_missing_pull_request_trigger(self):
        """review.yml with `on: workflow_dispatch` only — no PR-family
        trigger — must WARN, and the verdict must remain PASS."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "name: review\n"
                "on:\n"
                "  workflow_dispatch:\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            trig = self._diagnostic_rows(r, "workflow triggers: review.yml")
            self.assertEqual(len(trig), 1, f"expected 1 trigger row, got {trig}")
            self.assertEqual(trig[0].state, "WARN", trig[0].row())
            self.assertTrue(r.ok, f"verdict must remain PASS; failures={r.failing()}")
            self.assertIn("pull_request", trig[0].detail)

    def test_workflow_diagnostics_passes_when_pull_request_trigger_present(self):
        """Standard review.yml with `pull_request:` → PASS row."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "    types: [opened]\n"
                "jobs:\n"
                "  review:\n"
                "    name: review\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            trig = self._diagnostic_rows(r, "workflow triggers: review.yml")
            self.assertEqual(len(trig), 1)
            self.assertEqual(trig[0].state, "PASS")
            self.assertIn("pull_request", trig[0].detail)

    def test_workflow_diagnostics_warns_on_fork_pr_secret_gap(self):
        """`pull_request:` only — must WARN about fork-PR secret gap."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            gap = self._diagnostic_rows(r, "fork-PR secret gap: review.yml")
            self.assertEqual(len(gap), 1)
            self.assertEqual(gap[0].state, "WARN")
            self.assertIn("fork PRs lose repo secrets", gap[0].detail)

    def test_workflow_diagnostics_passes_fork_gap_when_target_present(self):
        """`pull_request_target:` flips the same row to PASS."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "  pull_request_target:\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            gap = self._diagnostic_rows(r, "fork-PR secret gap: review.yml")
            self.assertEqual(len(gap), 1)
            self.assertEqual(gap[0].state, "PASS")

    def test_workflow_diagnostics_passes_fork_gap_when_guard_present(self):
        """`pull_request`-only but a same-repo fork guard is present →
        PASS. This is the shipped consumer review.yml shape: it keeps
        `pull_request` (to avoid the OIDC-401 that `pull_request_target`
        causes without org trust) and skips fork PRs via the guard."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  review:\n"
                "    name: review\n"
                "    if: github.event.pull_request.head.repo.full_name == github.repository\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            gap = self._diagnostic_rows(r, "fork-PR secret gap: review.yml")
            self.assertEqual(len(gap), 1)
            self.assertEqual(gap[0].state, "PASS")
            self.assertIn("same-repo guard", gap[0].detail)

    def test_workflow_diagnostics_info_fork_gap_in_source_repo(self):
        """`pull_request`-only, no guard, but the target is the dev-kit
        source repo → INFO (internal-branch PRs only), not WARN."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._mark_source_repo(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  review:\n"
                "    name: review\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            gap = self._diagnostic_rows(r, "fork-PR secret gap: review.yml")
            self.assertEqual(len(gap), 1)
            self.assertEqual(gap[0].state, "INFO")
            self.assertIn("source repo", gap[0].detail)

    def test_workflow_diagnostics_info_paths_filter(self):
        """`pull_request.paths:` filter surfaces an INFO row."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "    paths:\n"
                "      - 'lib/**'\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            paths = self._diagnostic_rows(r, "paths filter: review.yml")
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].state, "INFO")
            self.assertIn("lib/**", paths[0].detail)

    def test_workflow_diagnostics_info_branches_filter(self):
        """`pull_request.branches:` filter surfaces an INFO row."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "    branches:\n"
                "      - main\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            br = self._diagnostic_rows(r, "branches filter: review.yml")
            self.assertEqual(len(br), 1)
            self.assertEqual(br[0].state, "INFO")
            self.assertIn("main", br[0].detail)

    def test_workflow_diagnostics_warns_on_concurrency_cancel(self):
        """`concurrency.cancel-in-progress: true` → WARN."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "concurrency:\n"
                "  group: ${{ github.event.pull_request.number }}\n"
                "  cancel-in-progress: true\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            conc = self._diagnostic_rows(r, "concurrency: review.yml")
            self.assertEqual(len(conc), 1)
            self.assertEqual(conc[0].state, "WARN")
            self.assertIn("cancel-in-progress=true", conc[0].detail)

    def test_workflow_diagnostics_passes_concurrency_cancel_false(self):
        """`cancel-in-progress: false` → PASS."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "concurrency:\n"
                "  group: ${{ github.event.pull_request.number }}\n"
                "  cancel-in-progress: false\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            conc = self._diagnostic_rows(r, "concurrency: review.yml")
            self.assertEqual(len(conc), 1)
            self.assertEqual(conc[0].state, "PASS")

    def test_workflow_diagnostics_info_job_if(self):
        """Job-level `if:` expression surfaces verbatim in an INFO row."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    if: \"github.event.pull_request.title != 'bot'\"\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            if_rows = self._diagnostic_rows(r, "job if: review.yml/review")
            self.assertEqual(len(if_rows), 1)
            self.assertEqual(if_rows[0].state, "INFO")
            self.assertIn("bot", if_rows[0].detail)

    def test_workflow_diagnostics_info_missing_job_name_review(self):
        """review.yml job without `name:` emits an INFO row (not WARN —
        review.yml jobs in the shipped template all have `name:`; INFO
        is for user-customised variants)."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            name_rows = self._diagnostic_rows(r, "job name: review.yml/review")
            self.assertEqual(len(name_rows), 1)
            self.assertEqual(name_rows[0].state, "INFO")

    def test_workflow_diagnostics_warns_missing_job_name_auto_fix(self):
        """auto-fix-pr.yml's single job without `name:` is WARN — its
        bare-key name is harder to match in branch-protection required
        status checks."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "auto-fix-pr.yml", (
                "on:\n"
                "  pull_request_review:\n"
                "    types: [submitted]\n"
                "jobs:\n"
                "  auto-fix:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            name_rows = self._diagnostic_rows(r, "job name: auto-fix-pr.yml/auto-fix")
            self.assertEqual(len(name_rows), 1)
            self.assertEqual(name_rows[0].state, "WARN")

    def test_workflow_diagnostics_info_unparseable_yaml(self):
        """Binary-garbage workflow file emits an INFO row (parse error)
        — never FAIL. The file-present PASS row (from `_check_required_files`)
        is still emitted."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", b"\x00\x01\x02\xffnot yaml")
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            # File-present row still PASS.
            file_present = [
                c for c in r.checks
                if c.label == "file present: .github/workflows/review.yml"
            ]
            self.assertEqual(len(file_present), 1)
            self.assertEqual(file_present[0].state, "PASS",
                             "file-present PASS row must survive unparseable YAML")
            # All diagnostic rows for review.yml are INFO, never FAIL.
            diags = [
                c for c in r.checks
                if "review.yml" in c.label
                and any(c.label.startswith(p) for p in (
                    "workflow triggers:", "fork-PR secret gap:",
                    "concurrency:", "paths filter:", "branches filter:",
                    "job if:", "job name:", "action ref mutable:",
                ))
            ]
            self.assertGreater(len(diags), 0)
            for d in diags:
                self.assertIn(d.state, {"INFO", "WARN"},
                              f"diagnostic row must not be FAIL: {d.row()}")
                # Either `parse_error` ("could not parse: ...") or a
                # raw read failure ("read error: ...") is acceptable —
                # both indicate we cannot introspect the workflow.
                self.assertTrue(
                    "parse" in d.detail or "read error" in d.detail,
                    f"detail must indicate non-parseable: {d.row()}",
                )

    def test_workflow_diagnostics_handles_quoted_on_key(self):
        """`\"on\": pull_request` (YAML keyword-quoted form) is recognised."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "\"on\": pull_request\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            trig = self._diagnostic_rows(r, "workflow triggers: review.yml")
            self.assertEqual(len(trig), 1)
            self.assertEqual(trig[0].state, "PASS")
            self.assertIn("pull_request", trig[0].detail)

    def test_workflow_diagnostics_info_action_pin_review(self):
        """Third-party action ref (`claude-code-action@v1`) not pinned
        to a 40-char SHA emits an INFO row listing the mutable refs."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._minimal_install(target)
            self._write_workflow(target, "review.yml", (
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  review:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: anthropics/claude-code-action@v1\n"
            ))
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    r = self.cd.audit(target)
            pin = self._diagnostic_rows(r, "action ref mutable: review.yml")
            self.assertEqual(len(pin), 1)
            self.assertEqual(pin[0].state, "INFO")
            self.assertIn("anthropics/claude-code-action@v1", pin[0].detail)

    # ------------------------------------------------------------------
    # Branch-protection (single-row check; WARN on mismatch, SKIP on
    # degraded gh/repo, INFO in source repo).
    # ------------------------------------------------------------------

    def test_branch_protection_skip_when_no_repo(self):
        """No git remote → SKIP."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            # No `git remote` set up in the tmpdir → `_detect_owner_repo`
            # returns "". Patch _check_gh_auth and _check_secrets so the
            # audit's verdict is driven by the branch-policy row under
            # test (CI runners have no gh auth → those checks would
            # otherwise FAIL).
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "PASS", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    with patch.object(self.cd, "_detect_owner_repo", return_value=""):
                        r = self.cd.audit(target)
            bp = self._diagnostic_rows(r, "branch policy")
            self.assertEqual(len(bp), 1)
            self.assertEqual(bp[0].state, "SKIP")
            self.assertIn("no GitHub remote", bp[0].detail)

    def test_branch_protection_skip_when_gh_missing(self):
        """`_fetch_required_status_checks` returns degraded → SKIP."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "PASS", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    with patch.object(self.cd, "_detect_owner_repo", return_value="example/repo"):
                        with patch.object(
                            self.cd, "_fetch_required_status_checks",
                            return_value=(set(), "gh not on PATH"),
                        ):
                            r = self.cd.audit(target)
            bp = self._diagnostic_rows(r, "branch policy")
            self.assertEqual(len(bp), 1)
            self.assertEqual(bp[0].state, "SKIP")
            self.assertIn("gh not on PATH", bp[0].detail)

    def test_branch_protection_skip_when_gh_unauth(self):
        """`gh api` returns degraded → SKIP."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "PASS", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    with patch.object(self.cd, "_detect_owner_repo", return_value="example/repo"):
                        with patch.object(
                            self.cd, "_fetch_required_status_checks",
                            return_value=(set(), "gh not authenticated"),
                        ):
                            r = self.cd.audit(target)
            bp = self._diagnostic_rows(r, "branch policy")
            self.assertEqual(len(bp), 1)
            self.assertEqual(bp[0].state, "SKIP")
            self.assertIn("gh not authenticated", bp[0].detail)

    def test_branch_protection_warn_on_required_check_mismatch(self):
        """Mock API returns `["lint"]` while review.yml emits
        jobs named `review`/`security` → WARN on mismatch."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".env.example").write_text(
                "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8",
            )
            # Replace review.yml with jobs whose names don't match the
            # required checks, so the diff is observable.
            self._write_workflow(target, "review.yml", (
                "on:\n  pull_request:\njobs:\n"
                "  review:\n    name: review\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo\n"
                "  security:\n    name: security\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "PASS", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    with patch.object(self.cd, "_detect_owner_repo", return_value="example/repo"):
                        with patch.object(
                            self.cd, "_fetch_required_status_checks",
                            return_value=({"lint"}, ""),
                        ):
                            r = self.cd.audit(target)
            bp = self._diagnostic_rows(r, "branch policy")
            self.assertEqual(len(bp), 1, f"unexpected rows: {bp}")
            self.assertEqual(bp[0].state, "WARN", bp[0].row())
            self.assertIn("lint", bp[0].detail)
            self.assertTrue(r.ok)

    def test_branch_protection_pass_on_full_match(self):
        """Mock API returns the same set as workflow job `name:`s → PASS."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".env.example").write_text(
                "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8",
            )
            self._write_workflow(target, "review.yml", (
                "on:\n  pull_request:\njobs:\n"
                "  review:\n    name: lint\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo\n"
                "  security:\n    name: test (python 3.12)\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo\n"
            ))
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "PASS", "")):
                with patch.object(self.cd, "_check_secrets", return_value=[]):
                    with patch.object(self.cd, "_detect_owner_repo", return_value="example/repo"):
                        with patch.object(
                            self.cd, "_fetch_required_status_checks",
                            return_value=({"lint", "test (python 3.12)"}, ""),
                        ):
                            r = self.cd.audit(target)
            bp = self._diagnostic_rows(r, "branch policy")
            self.assertEqual(len(bp), 1)
            self.assertEqual(bp[0].state, "PASS", bp[0].row())
            self.assertTrue(r.ok)

    def test_branch_protection_info_in_source_repo(self):
        """Source-repo mode → INFO (not audited)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            self._mark_source_repo(target)
            r = self.cd.audit(target)
            bp = self._diagnostic_rows(r, "branch policy")
            self.assertEqual(len(bp), 1)
            self.assertEqual(bp[0].state, "INFO")
            self.assertIn("source repo", bp[0].detail)

    # ------------------------------------------------------------------
    # WARN state semantics
    # ------------------------------------------------------------------

    def test_summary_lines_shows_warn_count(self):
        """summary_lines() includes `warnings: N` and verdict stays PASS
        even with WARN rows present."""
        r = self.cd.DoctorReport()
        r.checks.append(self.cd.Check("a", "WARN", "x"))
        r.checks.append(self.cd.Check("b", "WARN", "y"))
        r.checks.append(self.cd.Check("c", "PASS", "z"))
        lines = r.summary_lines()
        self.assertTrue(lines[0].startswith("ci-doctor verdict: PASS"))
        self.assertIn("warnings: 2", lines[1])
        self.assertIn("failing: 0", lines[1])
        self.assertTrue(r.ok)

    def test_warn_rows_do_not_flip_ok(self):
        """WARN rows do not flip `ok`. `r.warnings()` returns them."""
        r = self.cd.DoctorReport()
        r.checks.append(self.cd.Check("a", "WARN", "x"))
        self.assertTrue(r.ok)
        self.assertEqual(len(r.warnings()), 1)
        self.assertEqual(r.failing(), [])

    def test_info_rows_not_in_warnings(self):
        """INFO rows are not in `warnings()` and not in `failing()`."""
        r = self.cd.DoctorReport()
        r.checks.append(self.cd.Check("a", "INFO", "x"))
        r.checks.append(self.cd.Check("b", "WARN", "y"))
        self.assertEqual(len(r.warnings()), 1)
        self.assertEqual(r.failing(), [])
        self.assertTrue(r.ok)

    def test_no_fail_regression_in_fresh_install(self):
        """Re-run the install-shape smoke after wiring; no NEW FAIL rows
        beyond the pre-existing baseline."""
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self._install(target)
            (target / ".env.example").write_text(
                "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8",
            )
            with patch.object(self.cd, "_check_gh_auth", return_value=self.cd.Check("gh auth", "SKIP", "")):
                r = self.cd.audit(target)
            install_shape_rows = [
                c for c in r.checks
                if not c.label.startswith(("gh auth", "repo context", "secret set:"))
            ]
            failing_shape = [c for c in install_shape_rows if c.state == "FAIL"]
            self.assertEqual(
                failing_shape, [],
                f"unexpected FAIL rows after wiring diagnostics: {failing_shape}",
            )
            self.assertTrue(r.ok)


class TestOpenPrState(unittest.TestCase):
    """Issue #249: ci-doctor must surface CI-silently-skipped PR states.

    When a PR is opened in `mergeable: CONFLICTING` (or still computing)
    GitHub Actions refuses to run any workflow on the PR. ci-doctor
    must NOT return PASS in that state. The check inspects the open PR
    for the current branch via `gh pr view --json ...` and emits
    FAIL/WARN/INFO/SKIP rows accordingly.
    """

    @classmethod
    def setUpClass(cls):
        cls.cd = _load("ci_doctor", "ci_doctor.py")

    def _diagnostic_rows(self, r, label_substr: str) -> list:
        """Filter `r.checks` to rows whose label contains `label_substr`."""
        return [c for c in r.checks if label_substr in c.label]

    def _gh_pr_json(self, *, mergeable, is_draft=False, title=""):
        """Return the dict shape `gh pr view --json mergeable,...` emits."""
        return {
            "mergeable": mergeable,  # "CONFLICTING" | "MERGEABLE" | "UNKNOWN"
            "mergeStateStatus": "DIRTY" if mergeable == "CONFLICTING" else "CLEAN",
            "isDraft": is_draft,
            "title": title,
        }

    def _audit_with_pr_state(self, pr_payload):
        """Run `audit()` with `_fetch_open_pr_state` mocked to `pr_payload`.

        The tempdir has no `.env.example`, no workflow files, no marker,
        no git remote — so the install-shape + provider + secrets checks
        would all FAIL and pollute `r.ok`. Patch those out so the only
        check under test is the open-PR check.
        """
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_required_files", return_value=[]):
                    with patch.object(self.cd, "_check_marker_payload", return_value=[]):
                        with patch.object(self.cd, "_check_provider_declared", return_value=[]):
                            with patch.object(self.cd, "_check_secrets", return_value=[]):
                                with patch.object(self.cd, "_fetch_open_pr_state",
                                                  return_value=(pr_payload, "")):
                                    return self.cd.audit(target), target

    def _open_pr_rows(self, r):
        return self._diagnostic_rows(r, "open PR ")

    def test_conflicting_pr_flips_verdict_to_fail(self):
        """Issue #249 repro: open PR in CONFLICTING state → audit must FAIL.

        Pre-fix: this test fails because the open-PR check does not exist
        and audit returns PASS even when the PR is in a CI-silently-skipped
        state. Post-fix: a FAIL row appears with the merge-conflict message
        and `r.ok` becomes False.
        """
        pr = self._gh_pr_json(mergeable="CONFLICTING", title="fix: something")
        r, _ = self._audit_with_pr_state(pr)
        rows = self._open_pr_rows(r)
        self.assertTrue(
            any(c.state == "FAIL" for c in rows),
            f"CONFLICTING PR must produce a FAIL row; got: "
            f"{[(c.label, c.state, c.detail) for c in rows]}",
        )
        merge_row = next((c for c in rows if "mergeable" in c.label), None)
        self.assertIsNotNone(merge_row, "no `open PR mergeable` row emitted")
        self.assertEqual(merge_row.state, "FAIL")
        self.assertIn("conflict", merge_row.detail.lower())
        self.assertFalse(
            r.ok,
            "audit verdict must be FAIL when open PR has merge conflicts",
        )

    def test_mergeable_pr_emits_pass_row(self):
        """Open PR in MERGEABLE state → PASS row, audit still PASS."""
        pr = self._gh_pr_json(mergeable="MERGEABLE", title="feat: ok")
        r, _ = self._audit_with_pr_state(pr)
        rows = self._open_pr_rows(r)
        merge_row = next((c for c in rows if "mergeable" in c.label), None)
        self.assertIsNotNone(merge_row)
        self.assertEqual(merge_row.state, "PASS")
        self.assertTrue(r.ok)

    def test_unknown_merge_state_warns(self):
        """GitHub still computing (mergeable: UNKNOWN) → WARN, not FAIL.

        Transient: re-running ci-doctor in 30s should resolve to either
        MERGEABLE or CONFLICTING. WARN keeps the verdict PASS but tells
        the user the state is in flux.
        """
        pr = self._gh_pr_json(mergeable="UNKNOWN", title="fix: ?")
        r, _ = self._audit_with_pr_state(pr)
        rows = self._open_pr_rows(r)
        merge_row = next((c for c in rows if "mergeable" in c.label), None)
        self.assertIsNotNone(merge_row)
        self.assertEqual(merge_row.state, "WARN")
        self.assertTrue(
            r.ok,
            "UNKNOWN merge state must not flip the verdict (it's transient)",
        )

    def test_draft_pr_emits_info_row(self):
        """isDraft: true → INFO row (drafts don't trigger required checks)."""
        pr = self._gh_pr_json(mergeable="MERGEABLE", is_draft=True, title="WIP")
        r, _ = self._audit_with_pr_state(pr)
        rows = self._open_pr_rows(r)
        draft_row = next((c for c in rows if "draft" in c.label), None)
        self.assertIsNotNone(draft_row)
        self.assertEqual(draft_row.state, "INFO")
        self.assertIn("draft", draft_row.detail.lower())
        self.assertTrue(r.ok)

    def test_bump_pr_title_emits_info_row(self):
        """Title starts `chore(release): bump dev-kit to v` → INFO row.

        The version-bump workflow skips ci/review/security on bump PRs
        by design (their job is purely the version-tag dance). ci-doctor
        should surface this so users don't ask 'why didn't test run?'.
        """
        pr = self._gh_pr_json(
            mergeable="MERGEABLE",
            title="chore(release): bump dev-kit to v0.3.92",
        )
        r, _ = self._audit_with_pr_state(pr)
        rows = self._open_pr_rows(r)
        title_row = next((c for c in rows if "title" in c.label), None)
        self.assertIsNotNone(title_row)
        self.assertEqual(title_row.state, "INFO")
        self.assertIn("bump", title_row.detail.lower())
        self.assertTrue(r.ok)

    def test_no_open_pr_skips_check(self):
        """`_fetch_open_pr_state` returns degraded msg → SKIP row.

        Most ci-doctor runs happen BEFORE the PR is opened. In that case
        the open-PR check is meaningless and must not flip the verdict.
        """
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_required_files", return_value=[]):
                    with patch.object(self.cd, "_check_marker_payload", return_value=[]):
                        with patch.object(self.cd, "_check_provider_declared", return_value=[]):
                            with patch.object(self.cd, "_check_secrets", return_value=[]):
                                with patch.object(self.cd, "_fetch_open_pr_state",
                                                  return_value=({}, "no open PR for current branch")):
                                    r = self.cd.audit(target)
        rows = self._open_pr_rows(r)
        self.assertTrue(
            any(c.state == "SKIP" for c in rows),
            f"no-open-PR case must SKIP; got: "
            f"{[(c.label, c.state, c.detail) for c in rows]}",
        )
        self.assertTrue(r.ok)

    def test_gh_unavailable_skips_check(self):
        """`gh` absent or unauthenticated → SKIP, not FAIL.

        Same degraded-mode discipline as the rest of ci-doctor: missing
        tool is SKIP, not FAIL, because the user might be running the
        audit in an environment without gh auth (CI runner, container).
        """
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            with patch.object(self.cd, "_check_gh_auth",
                              return_value=self.cd.Check("gh auth", "SKIP", "")):
                with patch.object(self.cd, "_check_required_files", return_value=[]):
                    with patch.object(self.cd, "_check_marker_payload", return_value=[]):
                        with patch.object(self.cd, "_check_provider_declared", return_value=[]):
                            with patch.object(self.cd, "_check_secrets", return_value=[]):
                                with patch.object(self.cd, "_fetch_open_pr_state",
                                                  return_value=({}, "gh not on PATH")):
                                    r = self.cd.audit(target)
        rows = self._open_pr_rows(r)
        self.assertTrue(
            any(c.state == "SKIP" for c in rows),
            f"gh-unavailable case must SKIP; got: "
            f"{[(c.label, c.state, c.detail) for c in rows]}",
        )
        self.assertTrue(r.ok)

    def test_diagnostic_rows_helper_filters_by_label(self):
        """Sanity: `_diagnostic_rows` finds rows containing the substring.

        Not strictly an open-PR test — verifies the shared helper used by
        the rest of this class actually filters as expected.
        """
        r = self.cd.DoctorReport()
        r.checks.append(self.cd.Check("open PR mergeable", "PASS", "ok"))
        r.checks.append(self.cd.Check("other", "FAIL", "x"))
        rows = self._diagnostic_rows(r, "open PR ")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].label, "open PR mergeable")


class TestCheckTemplatesCurrent(unittest.TestCase):
    """`templates current` check: PASS/INFO/WARN/SKIP mapping for the
    consumer's installed CI templates vs the live dev-kit source.

    Wired into `audit()` after `_check_marker_payload` so the new
    information appears next to the marker read. Lives in its own test
    class because it needs the real `install_ci_config` machinery (not
    the `_minimal_install` stub) to populate `template_shas`.
    """

    @classmethod
    def setUpClass(cls):
        cls.cd = _load_ci_doctor()
        cls.ci_setup = _load_ci_setup()
        cls.plugin_root = Path(__file__).resolve().parent.parent

    def test_check_passes_on_clean_install(self):
        """Fresh install → templates match dev-kit source → PASS."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            r = self.cd.audit(target)
            rows = [c for c in r.checks if "templates current" in c.label]
            self.assertEqual(len(rows), 1, f"expected one templates-current row; got {rows}")
            self.assertEqual(rows[0].state, "PASS", f"expected PASS; got {rows[0]}")

    def test_check_warns_on_consumer_drift(self):
        """Consumer edits a file → ci-doctor surfaces drift as WARN."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Simulate consumer modifying an installed file
            rel = "scripts/validate.py"
            (target / rel).write_bytes((target / rel).read_bytes() + b"\n# edit\n")
            r = self.cd.audit(target)
            rows = [c for c in r.checks if "templates current" in c.label]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].state, "WARN",
                             f"expected WARN on consumer drift; got {rows[0]}")
            self.assertIn("consumer_modified", rows[0].detail)

    def test_check_skips_when_marker_lacks_version(self):
        """v1.0.0 marker (no installed_dev_kit_version) → SKIP."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker.pop("installed_dev_kit_version", None)
            marker.pop("template_shas", None)
            marker_path.write_text(json.dumps(marker))
            r = self.cd.audit(target)
            rows = [c for c in r.checks if "templates current" in c.label]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].state, "SKIP",
                             f"expected SKIP on unknown version; got {rows[0]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
