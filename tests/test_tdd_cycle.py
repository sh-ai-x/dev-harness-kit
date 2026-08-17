#!/usr/bin/env python3
"""Tests for lib/tdd_cycle (RED/GREEN evidence recording).

Covers:
- CLI root resolution honors DEV_KIT_TDD_ROOT (matching tdd-guard.sh:26),
  falling back to git toplevel, then cwd
- explicit --root still wins
- RED requires a failing test command; GREEN requires a prior RED
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import tdd_cycle  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


def _env_without(key: str) -> dict:
    return {k: v for k, v in os.environ.items() if k != key}


class TestResolveRoot(unittest.TestCase):
    def test_explicit_root_wins_over_env_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit"
            with patch.dict(os.environ, {"DEV_KIT_TDD_ROOT": str(Path(tmp) / "env")}):
                self.assertEqual(tdd_cycle._resolve_root(explicit), explicit.resolve())

    def test_env_dev_kit_tdd_root_wins_over_git_toplevel(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
            with patch.dict(os.environ, {"DEV_KIT_TDD_ROOT": str(Path(tmp))}):
                self.assertEqual(tdd_cycle._resolve_root(None), Path(tmp).resolve())

    def test_git_toplevel_fallback_from_subdir(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
            subdir = Path(repo) / "a" / "b"
            subdir.mkdir(parents=True)
            old_cwd = Path.cwd()
            try:
                os.chdir(subdir)
                with patch.dict(os.environ, _env_without("DEV_KIT_TDD_ROOT"), clear=True):
                    self.assertEqual(tdd_cycle._resolve_root(None), Path(repo).resolve())
            finally:
                os.chdir(old_cwd)

    def test_cwd_fallback_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, _env_without("DEV_KIT_TDD_ROOT"), clear=True):
                    self.assertEqual(tdd_cycle._resolve_root(None), Path(tmp).resolve())
            finally:
                os.chdir(old_cwd)


class TestCliRootResolution(unittest.TestCase):
    def test_cli_honors_dev_kit_tdd_root_over_git_toplevel(self):
        """RED evidence must land under DEV_KIT_TDD_ROOT, not the git toplevel.

        tdd-guard.sh:26-27 reads ${DEV_KIT_TDD_ROOT:-git toplevel}/.dev-kit/
        .tdd-cycle.json; if the CLI (run without --root, as the guard's own
        deny message suggests) recorded to the toplevel instead, the guard
        would never see the RED evidence and false-deny the next core-code
        edit. This test is RED under the old Path.cwd() default.
        """
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
            root = Path(tmp)
            env = {**os.environ, "DEV_KIT_TDD_ROOT": str(root),
                   "PYTHONPATH": str(REPO_ROOT)}
            result = subprocess.run(
                [sys.executable, "-m", "lib.tdd_cycle", "red", "--", "false"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / ".dev-kit" / ".tdd-cycle.json").is_file())
            self.assertFalse((Path(repo) / ".dev-kit" / ".tdd-cycle.json").exists())

    def test_cli_honors_dev_kit_tdd_root_when_cwd_is_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "DEV_KIT_TDD_ROOT": str(root),
                   "PYTHONPATH": str(REPO_ROOT)}
            result = subprocess.run(
                [sys.executable, "-m", "lib.tdd_cycle", "red", "--", "false"],
                cwd=tmp, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / ".dev-kit" / ".tdd-cycle.json").is_file())


class TestCycleSemantics(unittest.TestCase):
    def test_green_requires_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "lib.tdd_cycle", "--root", tmp,
                 "green", "--", "true"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("run RED first", result.stderr)

    def test_red_requires_failing_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "lib.tdd_cycle", "--root", tmp,
                 "red", "--", "true"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("test command passed", result.stderr)
            self.assertFalse((Path(tmp) / ".dev-kit" / ".tdd-cycle.json").is_file())

    def test_red_then_green_cycle_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            red = subprocess.run(
                [sys.executable, "-m", "lib.tdd_cycle", "--root", tmp,
                 "red", "--", "false"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(red.returncode, 0, red.stderr)
            green = subprocess.run(
                [sys.executable, "-m", "lib.tdd_cycle", "--root", tmp,
                 "green", "--", "true"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(green.returncode, 0, green.stderr)
            state = json.loads((Path(tmp) / ".dev-kit" / ".tdd-cycle.json").read_text())
            self.assertEqual(state["phase"], "green")
            self.assertEqual(state["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
