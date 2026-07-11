#!/usr/bin/env python3
"""test_ci_setup.py — Tests for the `/dev-kit:ci-setup` engine.

Covers lib/ci_setup.py:install_ci_config() and the templates/ tree it ships.
Uses the same importlib-from-path pattern as tests/test_smoke.py so it works
as both `python -m unittest tests/test_ci_setup.py` and `pytest tests/test_ci_setup.py`.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load_ci_setup():
    """Load lib/ci_setup.py by file path (mirrors test_smoke.py:64-66 pattern).

    NOTE: the module MUST be registered in sys.modules BEFORE exec_module for
    Python 3.14's @dataclass to resolve cross-module type lookups.
    """
    name = "ci_setup"
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "lib" / "ci_setup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register FIRST so @dataclass can resolve names
    spec.loader.exec_module(mod)
    return mod


class TestCiSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_bootstrap_engine_returns_typed_report(self):
        """Smoke-check the InstallReport dataclass shape."""
        r = self.ci_setup.InstallReport()
        self.assertIsInstance(r.created, list)
        self.assertIsInstance(r.overwritten, list)
        self.assertIsInstance(r.skipped, list)
        self.assertIsInstance(r.errors, list)
        self.assertEqual(r.elapsed_ms, 0)
        self.assertTrue(r.ok)
        r.errors.append("forced")
        self.assertFalse(r.ok)

    def test_invalid_target_dir_raises(self):
        """Non-existent target raises FileNotFoundError; non-directory raises NotADirectoryError."""
        with self.assertRaises(FileNotFoundError):
            self.ci_setup.install_ci_config(Path("/nonexistent/ci_setup_test_xyz"))
        fp = Path("/tmp/_ci_setup_file_target")
        try:
            fp.write_text("placeholder")
            with self.assertRaises((NotADirectoryError, FileNotFoundError)):
                self.ci_setup.install_ci_config(fp)
        finally:
            fp.unlink(missing_ok=True)

    def test_install_creates_expected_files_in_empty_target(self):
        """Fresh tmp dir: all EXPECTED_PATHS land; no extras."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = self.ci_setup.install_ci_config(target)
            self.assertEqual(report.errors, [], f"errors: {report.errors}")
            for rel in self.ci_setup.EXPECTED_PATHS:
                self.assertTrue((target / rel).exists(), f"missing: {rel}")
            self.assertEqual(len(report.created), len(self.ci_setup.EXPECTED_PATHS))
            self.assertEqual(report.overwritten, [])
            self.assertEqual(report.skipped, [])

    def test_install_is_idempotent_without_force(self):
        """Second run without force skips every path; no files re-touched."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r1 = self.ci_setup.install_ci_config(target)
            self.assertEqual(r1.errors, [])
            sentinels = {rel: (target / rel).read_text() for rel in self.ci_setup.EXPECTED_PATHS}
            r2 = self.ci_setup.install_ci_config(target)
            self.assertEqual(r2.created, [])
            self.assertEqual(r2.overwritten, [])
            self.assertEqual(
                len(r2.skipped), len(self.ci_setup.EXPECTED_PATHS),
                f"all paths should be skipped on re-run without --force",
            )
            self.assertEqual(r2.errors, [])
            # No file was re-written (content preserved verbatim).
            for rel in self.ci_setup.EXPECTED_PATHS:
                self.assertEqual(
                    (target / rel).read_text(), sentinels[rel],
                    f"file re-touched during idempotent re-run: {rel}",
                )

    def test_install_force_overwrites_cleanly(self):
        """Pre-seed a sentinel; --force replaces it with template content."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            sentinel_dir = target / ".github" / "workflows"
            sentinel_dir.mkdir(parents=True)
            sentinel = sentinel_dir / "ci.yml"
            sentinel.write_text("# SENTINEL: must be replaced by --force\n")
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r.errors, [])
            content = sentinel.read_text()
            self.assertNotIn("SENTINEL", content, "force=True should overwrite sentinel")
            self.assertIn("name: CI", content, "template content should land")
            overwritten = [p for p in r.overwritten if "ci.yml" in p]
            self.assertTrue(overwritten, "ci.yml should be in overwritten list")

    def test_partial_install_completes_remaining(self):
        """If some template files are missing, install copies only the missing ones."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            (target / self.ci_setup.EXPECTED_PATHS[0]).unlink()
            r = self.ci_setup.install_ci_config(target)
            self.assertTrue(
                len(r.created) + len(r.overwritten) >= 1,
                f"at least the deleted path should be re-copied; created={r.created} overwritten={r.overwritten}",
            )

    def test_executable_bit_set_on_sh_files(self):
        """All .sh + pre-push + validate.py have +x bit after install."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            for rel in self.ci_setup.EXECUTABLE_PATHS:
                p = target / rel
                self.assertTrue(p.exists(), f"missing: {rel}")
                mode = p.stat().st_mode
                self.assertTrue(mode & 0o111, f"not executable: {rel} (mode={oct(mode)})")

    def test_validate_py_runs_against_installed_ci_dir(self):
        """The installed validate.py exits 0 against the install target."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            r = subprocess.run(
                ["python3", "scripts/validate.py"],
                cwd=str(target), capture_output=True, text=True,
            )
            self.assertEqual(
                r.returncode, 0,
                f"validate.py exited {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}",
            )
            self.assertIn("OK: CI installation valid", r.stdout)

    # === Worktree-rule rollout (PR #22 + this PR) ===

    def test_worktree_rule_files_are_in_expected_paths(self):
        """EXPECTED_PATHS includes the 7 worktree-rule files added in PR #22."""
        expected_new = {
            "hooks/worktree-guard.sh",
            "hooks/task-detector.sh",
            "hooks/session-start-check.sh",
            "hooks/lib/worktree-detect.sh",
            "hooks/hooks.json",
            ".claude/rules/git-workflow.md",
            "tests/test_worktree_guard.py",
        }
        actual = set(self.ci_setup.EXPECTED_PATHS)
        self.assertTrue(
            expected_new.issubset(actual),
            f"missing from EXPECTED_PATHS: {expected_new - actual}",
        )

    def test_worktree_hooks_have_executable_bit_in_target(self):
        """All 4 new .sh files end up executable in the installed target."""
        import tempfile
        import stat
        new_sh = (
            "hooks/worktree-guard.sh",
            "hooks/task-detector.sh",
            "hooks/session-start-check.sh",
            "hooks/lib/worktree-detect.sh",
        )
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            for rel in new_sh:
                p = target / rel
                self.assertTrue(p.exists(), f"missing: {rel}")
                self.assertTrue(p.stat().st_mode & stat.S_IXUSR, f"not +x: {rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
