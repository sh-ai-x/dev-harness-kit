#!/usr/bin/env python3
"""test_templates_single_source.py — Regression tests for the templates/tests
parallel-tree drift bug.

Pins the same byte-identical-dup-tree bug that test_hooks_single_source.py
guards for hooks/, but for tests/ and rules/. The single-source pattern
(both pre-existing and now extended):

  - hooks/         -> installed at hooks/         (single source: plugin root)
  - tests/         -> installed at tests/         (single source: plugin root)
  - rules/         -> installed at .claude/rules/ (single source: plugin root)
  - everything else (templates/ci/.github/, scripts/, etc.)
                   -> read from templates/ci/ as before

`lib/ci_setup.py:_resolve_template_source` does the redirection. A
parallel templates/ci/tests/ or templates/ci/rules/ tree was historically
maintained and silently drifted from the canonical plugin-root copy
across releases (e.g., test_worktree_guard.py picked up two new test
methods in `tests/` but the consumer copy under `templates/ci/tests/`
was not updated, so consumer installs were running an outdated test
suite for an entire release cycle).

Pins:
1. `templates/ci/tests/` MUST NOT exist after the consolidation.
2. `templates/ci/rules/` MUST NOT exist after the consolidation.
3. `_resolve_template_source("tests/<file>")` MUST read from
   `<plugin_root>/tests/<file>`, not `<plugin_root>/templates/ci/tests/<file>`.
4. `_resolve_template_source(".claude/rules/<file>")` MUST read from
   `<plugin_root>/rules/<file>`.
5. Installed bytes MUST match the source-of-truth plugin-root copy
   byte-for-byte.
6. Round-trip idempotency: a fresh `ci-setup --force` install followed
   by a hash check passes.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

# Make `import ci_setup` work when pytest runs this file from the worktree.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from ci_setup import (  # noqa: E402
    EXPECTED_PATHS,
    _PLUGIN_ROOT,
    _resolve_template_source,
    install_ci_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates" / "ci"
PLUGIN_TESTS_DIR = REPO_ROOT / "tests"
PLUGIN_RULES_DIR = REPO_ROOT / "rules"

# EXPECTED_PATHS entries that MUST round-trip byte-for-byte through install.
# Source-of-truth lives at the plugin root (tests/ or rules/); the install
# destination is the same path in the consumer repo.
_TESTS_RELPATHS: tuple[str, ...] = tuple(
    rel for rel in EXPECTED_PATHS if rel.startswith("tests/")
)
_RULES_RELPATHS: tuple[str, ...] = tuple(
    rel for rel in EXPECTED_PATHS if rel.startswith(".claude/rules/")
)


class TestNoLegacyParallelTrees(unittest.TestCase):
    """The parallel templates/ci/{tests,rules}/ trees MUST be gone."""

    def test_no_templates_ci_tests_dir(self):
        self.assertFalse(
            (TEMPLATES_DIR / "tests").exists(),
            f"templates/ci/tests/ exists — the parallel drift-prone tree is "
            f"alive. _resolve_template_source now reads tests/ from "
            f"<plugin_root>/tests/, so this dir must be deleted. See "
            f"test_hooks_single_source.py for the analog hooks fix.",
        )

    def test_no_templates_ci_rules_dir(self):
        self.assertFalse(
            (TEMPLATES_DIR / "rules").exists(),
            f"templates/ci/rules/ exists — the parallel drift-prone tree is "
            f"alive. _resolve_template_source now reads rules/ from "
            f"<plugin_root>/rules/, so this dir must be deleted.",
        )


class TestResolveSource(unittest.TestCase):
    """_resolve_template_source MUST redirect tests/ and rules/ to plugin root."""

    def test_tests_resolves_to_plugin_root(self):
        for rel in _TESTS_RELPATHS:
            with self.subTest(rel=rel):
                src = _resolve_template_source(rel)
                # The resolved source MUST live under <plugin_root>/tests/,
                # NOT under <plugin_root>/templates/ci/.
                self.assertTrue(
                    str(src).startswith(str(PLUGIN_TESTS_DIR) + "/")
                    or src == PLUGIN_TESTS_DIR / Path(rel).name,
                    f"{rel} resolved to {src}; expected under "
                    f"{PLUGIN_TESTS_DIR}",
                )
                self.assertFalse(
                    str(src).startswith(str(TEMPLATES_DIR) + "/"),
                    f"{rel} resolved to {src}; this is the legacy "
                    f"templates/ci/tests/ path that caused the drift bug.",
                )

    def test_rules_resolves_to_plugin_root(self):
        for rel in _RULES_RELPATHS:
            with self.subTest(rel=rel):
                src = _resolve_template_source(rel)
                # .claude/rules/<file> redirects to <plugin_root>/rules/<file>.
                filename = Path(rel).name
                expected = PLUGIN_RULES_DIR / filename
                self.assertEqual(
                    src, expected,
                    f"{rel} resolved to {src}; expected {expected} "
                    f"(<plugin_root>/rules/ — the canonical source).",
                )

    def test_other_paths_still_resolve_from_templates(self):
        """Non-hooks / non-tests / non-rules paths still come from templates/ci/."""
        for rel in ("scripts/validate.py", ".github/workflows/ci.yml"):
            with self.subTest(rel=rel):
                src = _resolve_template_source(rel)
                self.assertTrue(
                    str(src).startswith(str(TEMPLATES_DIR) + "/"),
                    f"{rel} should resolve to {TEMPLATES_DIR}/..., got {src}",
                )


class TestRoundTripByteIdentical(unittest.TestCase):
    """Installed bytes MUST match the source-of-truth plugin-root copy."""

    def _install_to_tmp(self) -> tuple[Path, dict[str, str]]:
        # Don't use TemporaryDirectory() as a context manager here — we need
        # the directory to stay alive for the assertions below.
        td = tempfile.mkdtemp()
        target = Path(td)
        report = install_ci_config(target, force=True)
        self.assertTrue(
            report.ok and not report.errors,
            f"install_ci_config reported errors: {report.errors}",
        )
        hashes = {}
        for rel in _TESTS_RELPATHS + _RULES_RELPATHS:
            installed = target / rel
            self.assertTrue(
                installed.exists(),
                f"{rel} not installed at {installed}",
            )
            h = hashlib.sha256(installed.read_bytes()).hexdigest()
            hashes[rel] = h
            src = _resolve_template_source(rel)
            self.assertEqual(
                h, hashlib.sha256(src.read_bytes()).hexdigest(),
                f"{rel} installed bytes do NOT match source "
                f"{src}. Drift between consumer install and plugin root.",
            )
        return target, hashes

    def test_tests_install_byte_identical_to_plugin_root(self):
        target, _ = self._install_to_tmp()
        installed = target / "tests" / "test_worktree_guard.py"
        canonical = PLUGIN_TESTS_DIR / "test_worktree_guard.py"
        self.assertEqual(
            installed.read_bytes(), canonical.read_bytes(),
            "Installed tests/test_worktree_guard.py differs from "
            "<plugin_root>/tests/test_worktree_guard.py. Drift bug.",
        )

    def test_rules_install_byte_identical_to_plugin_root(self):
        target, _ = self._install_to_tmp()
        installed = target / ".claude" / "rules" / "git-workflow.md"
        canonical = PLUGIN_RULES_DIR / "git-workflow.md"
        self.assertEqual(
            installed.read_bytes(), canonical.read_bytes(),
            "Installed .claude/rules/git-workflow.md differs from "
            "<plugin_root>/rules/git-workflow.md. Drift bug.",
        )


class TestIdempotentReinstall(unittest.TestCase):
    """Re-running install_ci_config is a no-op (no errors, no drift)."""

    def test_double_install_no_drift(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            for run in range(2):
                report = install_ci_config(target, force=True)
                self.assertTrue(
                    report.ok and not report.errors,
                    f"install run {run + 1} reported errors: {report.errors}",
                )
            # After two installs, all tests/ + rules/ files still match.
            for rel in _TESTS_RELPATHS + _RULES_RELPATHS:
                installed = target / rel
                src = _resolve_template_source(rel)
                self.assertEqual(
                    installed.read_bytes(), src.read_bytes(),
                    f"After double install, {rel} drifted from source {src}.",
                )


if __name__ == "__main__":
    unittest.main()
