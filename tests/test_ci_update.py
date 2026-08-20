#!/usr/bin/env python3
"""test_ci_update.py — Tests for lib/ci_update.py (the /dev-kit:ci-update engine).

The engine closes the dev-kit ⇄ consumer drift gap. It classifies every
EXPECTED_PATHS file into one of four states (new / updated / consumer_modified
/ diverged) and offers a safe apply path that backs up before overwriting.

Fixture pattern mirrors tests/test_ci_setup.py so the test runs under both
`python -m unittest` and `pytest`.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load(name: str, rel_path: str):
    """Load `lib/<rel_path>.py` by file path.

    Register the module in sys.modules FIRST so cross-module @dataclass type
    lookups resolve under Python 3.14.
    """
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "lib" / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestCiUpdate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load("ci_setup", "ci_setup.py")
        cls.ci_update = _load("ci_update", "ci_update.py")
        cls.plugin_root = PROJECT_ROOT

    # ---------------------------------------------------------------- diff

    def test_diff_marks_new_file(self):
        """File in EXPECTED_PATHS but absent at target → `new`."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Pick any EXPECTED_PATH and delete it from the consumer
            rel = ".github/workflows/ci.yml"
            (target / rel).unlink()
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            self.assertIn(rel, report.new)

    def test_diff_marks_unchanged(self):
        """All three SHAs match → `unchanged`."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            # At least one file should be unchanged after a fresh install
            self.assertGreater(len(report.unchanged), 0)
            # The ci.yml we just installed should be in unchanged
            self.assertIn(".github/workflows/ci.yml", report.unchanged)

    def test_diff_marks_updated_dev_kit_changed(self):
        """Dev-kit shipped a new template version after install → `updated`.

        Semantics: `updated` means the consumer's on-disk file is unchanged
        from install (`target_sha == installed_sha`) AND dev-kit's current
        source SHA differs from what was installed (`template_sha != installed_sha`).
        Simulated by NOT touching the consumer file and patching the
        marker's `template_shas[rel]` to a synthetic new SHA.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            # Pretend dev-kit shipped a new template version: record a
            # different `template_sha` but leave installed_file_sha and
            # the consumer's on-disk file unchanged.
            marker["template_shas"][rel] = "f" * 64
            marker_path.write_text(json.dumps(marker))
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            self.assertIn(rel, report.updated,
                          f"expected {rel} in updated; got updated={report.updated}")

    def test_diff_marks_consumer_modified(self):
        """target_sha != installed_file_shas, but installed_file_shas == template_sha → `consumer_modified`."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            consumer_path = target / rel
            original = consumer_path.read_bytes()
            consumer_path.write_bytes(original + b"\n# consumer local edit\n")
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            self.assertIn(rel, report.consumer_modified,
                          f"expected {rel} in consumer_modified; got consumer_modified={report.consumer_modified}")

    def test_diff_marks_diverged(self):
        """Both consumer and dev-kit changed the same file → `diverged`."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            consumer_path = target / rel
            original = consumer_path.read_bytes()
            consumer_path.write_bytes(original + b"\n# consumer local edit\n")
            # Also pretend dev-kit's source differs from the recorded template_sha
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker["template_shas"][rel] = "0" * 64  # nonsense SHA → template_sha != installed_file_shas
            marker_path.write_text(json.dumps(marker))
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            self.assertIn(rel, report.diverged,
                          f"expected {rel} in diverged; got diverged={report.diverged}")

    def test_diff_handles_v1_marker_without_version(self):
        """Marker without installed_dev_kit_version → warning, not crash."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker.pop("installed_dev_kit_version", None)
            marker_path.write_text(json.dumps(marker))
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            self.assertTrue(any("installed_dev_kit_version" in w for w in report.warnings),
                            f"expected version warning; got {report.warnings}")
            self.assertEqual(report.installed_dev_kit_version, "unknown")

    def test_diff_handles_missing_template_shas(self):
        """Marker without template_shas → recompute from live source on the fly."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker.pop("template_shas", None)
            marker_path.write_text(json.dumps(marker))
            report = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            # Should not raise; should report the recomputed snapshot
            self.assertIsInstance(report.template_shas, dict)
            self.assertGreater(len(report.template_shas), 0)

    def test_diff_dry_run_creates_no_files(self):
        """diff_ci_install leaves target filesystem untouched."""
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Snapshot every EXPECTED_PATH's SHA
            pre = {}
            for rel in self.ci_setup.EXPECTED_PATHS:
                p = target / rel
                if p.is_file():
                    pre[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            for rel, sha in pre.items():
                p = target / rel
                if p.is_file():
                    self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), sha,
                                     f"{rel} touched by diff")

    # ------------------------------------------------------------- apply

    def test_apply_creates_new_files(self):
        """apply mode='apply' writes NEW files; no .bak for new files."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = ".github/workflows/ci.yml"
            (target / rel).unlink()
            report = self.ci_update.apply_ci_update(
                target, mode="apply", plugin_root=self.plugin_root,
            )
            self.assertTrue((target / rel).is_file(), "NEW file should be written")
            self.assertNotIn(rel + ".bak", report.backed_up,
                             "NEW files don't get a .bak")

    def test_apply_overwrites_updated_files(self):
        """apply mode='apply' writes UPDATED files from dev-kit source.

        `updated` = dev-kit changed the template after install, consumer
        hasn't touched their copy. Simulate by patching `template_shas[rel]`
        to a synthetic new SHA; consumer file and installed_file_sha stay
        unchanged.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            consumer_path = target / rel
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker["template_shas"][rel] = "f" * 64
            marker_path.write_text(json.dumps(marker))
            self.ci_update.apply_ci_update(
                target, mode="apply", plugin_root=self.plugin_root,
            )
            # After apply, file should match dev-kit source (re-fetched
            # from `_resolve_template_source`). The content might equal
            # the pre-test bytes (the dev-kit source itself didn't change
            # in this test — only the marker's recorded template_sha was
            # patched), but the marker should now reflect the live
            # template_sha and the file should round-trip via the source.
            src = self.plugin_root / "templates" / "ci" / rel
            self.assertEqual(consumer_path.read_bytes(), src.read_bytes(),
                             "UPDATED file should match dev-kit source after apply")
            # Marker is re-recorded with the live template_sha, NOT the
            # synthetic "f"*64 we patched.
            new_marker = json.loads(marker_path.read_text())
            self.assertNotEqual(new_marker["template_shas"][rel], "f" * 64,
                                "apply should have re-recorded the live template_sha")

    def test_apply_backs_up_consumer_modified(self):
        """apply mode='force' creates .bak before overwriting consumer edits."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            consumer_path = target / rel
            original = consumer_path.read_bytes()
            consumer_path.write_bytes(original + b"\n# consumer edit\n")
            self.ci_update.apply_ci_update(
                target, mode="force", plugin_root=self.plugin_root,
            )
            bak_path = target / (rel + ".bak")
            self.assertTrue(bak_path.is_file(), ".bak should exist")
            self.assertEqual(bak_path.read_bytes(), original + b"\n# consumer edit\n",
                             ".bak should preserve consumer's edited bytes")

    def test_apply_no_backup_when_disabled(self):
        """backup=False skips .bak creation even on consumer_modified."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            consumer_path = target / rel
            consumer_path.write_bytes(consumer_path.read_bytes() + b"\n# edit\n")
            self.ci_update.apply_ci_update(
                target, mode="force", backup=False, plugin_root=self.plugin_root,
            )
            self.assertFalse((target / (rel + ".bak")).is_file(),
                             ".bak should NOT exist when backup=False")

    def test_apply_refreshes_marker_version(self):
        """After apply, marker records new installed_dev_kit_version."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Mutate the marker to simulate an older install version
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker["installed_dev_kit_version"] = "0.0.0-old"
            marker_path.write_text(json.dumps(marker))
            self.ci_update.apply_ci_update(
                target, mode="apply", plugin_root=self.plugin_root,
            )
            new_marker = json.loads(marker_path.read_text())
            self.assertEqual(
                new_marker["installed_dev_kit_version"],
                self.ci_setup.plugin_version(),
            )

    def test_apply_atomic_marker_write(self):
        """Marker survives partial crash via atomic_write_json."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            self.ci_update.apply_ci_update(
                target, mode="apply", plugin_root=self.plugin_root,
            )
            marker_path = target / ".dev-kit" / "ci-config.json"
            # Read → should be valid JSON, non-empty
            data = json.loads(marker_path.read_text())
            self.assertIsInstance(data, dict)
            self.assertGreater(len(data), 0)

    def test_apply_force_required_for_diverged(self):
        """apply mode='apply' refuses to touch DIVERGED files (lists them as warnings)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            rel = "scripts/validate.py"
            consumer_path = target / rel
            consumer_path.write_bytes(consumer_path.read_bytes() + b"\n# edit\n")
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker["template_shas"][rel] = "f" * 64
            marker_path.write_text(json.dumps(marker))
            report = self.ci_update.apply_ci_update(
                target, mode="apply", plugin_root=self.plugin_root,
            )
            self.assertIn(rel, report.diverged,
                          "diverged file should still be classified after apply")
            # mode='apply' should NOT have overwritten it
            self.assertIn(b"# edit", consumer_path.read_bytes(),
                          "consumer edit should be preserved when mode='apply'")

    # ------------------------------------------------------------- report shape

    def test_update_report_shape(self):
        """UpdateReport dataclass exposes every state list + version fields."""
        report = self.ci_update.UpdateReport()
        for attr in (
            "new", "updated", "unchanged", "consumer_modified",
            "diverged", "backed_up", "errors", "warnings",
            "elapsed_ms", "installed_dev_kit_version", "target_version",
            "template_shas",
        ):
            self.assertTrue(hasattr(report, attr), f"missing attr: {attr}")
        self.assertEqual(report.errors, [])
        self.assertTrue(report.ok)
        report_with_errors = self.ci_update.UpdateReport(errors=["boom"])
        self.assertFalse(report_with_errors.ok)

    def test_diff_returns_same_report_shape_as_apply(self):
        """diff_ci_install and apply_ci_update return the same UpdateReport type."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            d = self.ci_update.diff_ci_install(target, plugin_root=self.plugin_root)
            a = self.ci_update.apply_ci_update(
                target, mode="apply", plugin_root=self.plugin_root,
            )
            self.assertIsInstance(d, self.ci_update.UpdateReport)
            self.assertIsInstance(a, self.ci_update.UpdateReport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
