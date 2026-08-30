"""test_setup_git_defaults.py — regression for bin/setup-git-defaults.sh.

The script writes operator-global git config (`~/.gitconfig`) so rebase
auto-stashes dirty changes during `git pull`. Each test runs the script
with an isolated `HOME` env var so the user's real `~/.gitconfig` is
never read or written.

Pinned contract:
  T1: --help exits 0 and lists every setting + every flag.
  T2: --dry-run prints the would-set plan without touching the file.
  T3: plain invocation writes both keys and exits 0.
  T4: re-running is idempotent — file is byte-identical, stdout says
      "(already set; nothing to do)".
  T5: --check exits 0 once both keys are present.
  T6: --check exits 1 on a fresh HOME and lists both keys as missing.
  T7: --check with a partial pre-seed reports only the missing one.
  T8: plain run with a partial pre-seed sets only the missing key.
  T9: PATH=/nonexistent -> non-zero exit + "git binary not found".
  T10: unknown flag -> non-zero + stderr mentions --help.
  T11: positional arg -> non-zero + stderr mentions positional.
  T12: --dry-run leaves no .gitconfig; plain after it creates one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "bin" / "setup-git-defaults.sh"

# Resolve bash at module load so T9 can override PATH=/nonexistent
# without losing the ability to launch bash itself. subprocess.run
# uses PATH to find the executable when given a bare program name;
# we pass the resolved absolute path so the override only affects
# what the SCRIPT sees, not how Python launches it.
BASH = shutil.which("bash") or "/bin/bash"


def _run(tmp_home: Path, *args: str, env_override: dict | None = None
         ) -> subprocess.CompletedProcess:
    """Run the script with HOME=XDG_CONFIG_HOME=tmp_home so it touches
    only the temp dir's .gitconfig — never the user's real one."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_home)
    env["XDG_CONFIG_HOME"] = str(tmp_home)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30,
        env=env, cwd=str(tmp_home),
    )


def _gitconfig_text(tmp_home: Path) -> str:
    p = tmp_home / ".gitconfig"
    return p.read_text() if p.exists() else ""


def _seed_gitconfig(tmp_home: Path, lines: list[str]) -> None:
    (tmp_home / ".gitconfig").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )


def _read_value(tmp_home: Path, key: str) -> str:
    """Read a single key from tmp_home/.gitconfig via git's own parser,
    so the test is robust to whatever section/tab format git writes."""
    r = subprocess.run(
        ["git", "config", "--file", str(tmp_home / ".gitconfig"),
         "--get", key],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


class SetupGitDefaultsContract(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    # T1
    def test_help_exits_zero_and_lists_settings(self) -> None:
        result = _run(self.home, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for needle in ("Usage:", "--dry-run", "--check",
                       "rebase.autoStash", "pull.rebase",
                       "Exit codes:"):
            self.assertIn(needle, result.stdout,
                          f"--help missing {needle!r}")

    # T2
    def test_dry_run_prints_plan_without_mutating(self) -> None:
        result = _run(self.home, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would set rebase.autoStash=true", result.stdout)
        self.assertIn("would set pull.rebase=true", result.stdout)
        self.assertFalse((self.home / ".gitconfig").exists(),
                         "--dry-run must not create .gitconfig")

    # T3
    def test_first_run_sets_both_keys(self) -> None:
        result = _run(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Read back via git so the assertion is robust to section format.
        self.assertEqual(_read_value(self.home, "rebase.autoStash"), "true")
        self.assertEqual(_read_value(self.home, "pull.rebase"), "true")
        self.assertIn("✓ set rebase.autoStash=true", result.stdout)
        self.assertIn("✓ set pull.rebase=true", result.stdout)

    # T4
    def test_rerun_is_idempotent_and_byte_identical(self) -> None:
        first = _run(self.home)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _gitconfig_text(self.home)
        second = _run(self.home)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("(already set; nothing to do)", second.stdout)
        after = _gitconfig_text(self.home)
        self.assertEqual(before, after,
                         "re-run must leave .gitconfig byte-identical")

    # T5
    def test_check_returns_zero_when_all_present(self) -> None:
        setup = _run(self.home)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        result = _run(self.home, "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("All 2 setting(s) present.", result.stdout)
        self.assertNotIn("✗", result.stdout)

    # T6
    def test_check_returns_one_when_missing(self) -> None:
        result = _run(self.home, "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("✗ rebase.autoStash=true", result.stdout)
        self.assertIn("✗ pull.rebase=true", result.stdout)
        self.assertIn("2 setting(s) missing", result.stdout)

    # T7
    def test_check_partial_set_reports_only_missing(self) -> None:
        _seed_gitconfig(self.home, ["[rebase]", "\tautoStash = true"])
        result = _run(self.home, "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("✓ rebase.autoStash=true", result.stdout)
        self.assertIn("✗ pull.rebase=true", result.stdout)
        # Pre-seeded autoStash survives; pull.rebase still missing.
        self.assertEqual(_read_value(self.home, "rebase.autoStash"), "true")
        self.assertEqual(_read_value(self.home, "pull.rebase"), "")

    # T8
    def test_partial_set_rerun_only_sets_missing(self) -> None:
        _seed_gitconfig(self.home, ["[rebase]", "\tautoStash = true"])
        result = _run(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(already set; nothing to do)", result.stdout)
        self.assertIn("✓ set pull.rebase=true", result.stdout)
        self.assertEqual(_read_value(self.home, "rebase.autoStash"), "true")
        self.assertEqual(_read_value(self.home, "pull.rebase"), "true")

    # T9 — git missing on PATH. BASH is resolved at module load so we
    # can strip PATH for the child without losing the ability to spawn
    # bash itself. The script's `command -v git` then fails as
    # designed.
    def test_missing_git_binary_exits_non_zero(self) -> None:
        result = _run(self.home, env_override={"PATH": "/nonexistent"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git binary not found in PATH", result.stderr)

    # T10
    def test_unknown_flag_exits_nonzero_with_help_hint(self) -> None:
        result = _run(self.home, "--bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown flag", result.stderr)
        self.assertIn("--help", result.stderr)

    # T11
    def test_no_positional_args_allowed(self) -> None:
        result = _run(self.home, "unexpected-name")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected positional arg", result.stderr)

    # T12
    def test_dry_run_then_real_run_changes_file_exactly_once(self) -> None:
        dry = _run(self.home, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((self.home / ".gitconfig").exists(),
                         "--dry-run must not create .gitconfig")
        real = _run(self.home)
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertTrue((self.home / ".gitconfig").exists(),
                        "plain run after --dry-run must create .gitconfig")
        self.assertEqual(_read_value(self.home, "rebase.autoStash"), "true")
        self.assertEqual(_read_value(self.home, "pull.rebase"), "true")


if __name__ == "__main__":
    unittest.main()
