#!/usr/bin/env python3
"""test_fixers.py — RED-first tests for lib/fixers/."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib" / "fixers"))

import fixers  # noqa: E402
from fixers.base import FixerABC, Issue  # noqa: E402


class TestFixerRegistry(unittest.TestCase):
    def test_fixer_registry_lists_all_9(self):
        names = fixers.list_fixers()
        for expected in ("bootstrap", "plan", "build", "review", "security",
                          "audit", "iron_law", "hooks", "a2a"):
            self.assertIn(expected, names, f"missing fixer: {expected}")

    def test_get_fixer_returns_instance(self):
        for name in ("bootstrap", "plan", "build", "review", "security",
                      "audit", "iron_law", "hooks", "a2a"):
            instance = fixers.get_fixer(name)
            self.assertIsInstance(instance, FixerABC)

    def test_get_fixer_unknown_raises(self):
        with self.assertRaises(KeyError):
            fixers.get_fixer("nonexistent")


class TestFixerABC(unittest.TestCase):
    def test_issue_dataclass_shape(self):
        issue = Issue(
            asset="CLAUDE.md",
            file_path="CLAUDE.md",
            line=10,
            axis="semantic_drift",
            problem="Wrong wording for L1",
            suggested_fix="Update L1 to match lib/write_claude_md.py:IRON_LAWS",
        )
        self.assertEqual(issue.axis, "semantic_drift")
        self.assertEqual(issue.line, 10)

    def test_diagnose_basic(self):
        issue = Issue(
            asset="test",
            file_path="test.md",
            line=1,
            axis="completeness",
            problem="missing",
            suggested_fix="add",
        )
        self.assertIn("missing", issue.problem)


class TestHooksFixer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hooks_fixer_target_files(self):
        import fixers.hooks as h
        f = h.HooksFixer()
        self.assertIn("tdd-guard", " ".join(f.target_files))
        self.assertIn("bash-guard", " ".join(f.target_files))

    def test_hooks_fixer_diagnose_finds_missing_shebang(self):
        import fixers.hooks as h
        f = h.HooksFixer()
        bad_content = "echo no shebang\n"
        issues = f.diagnose(self.root, {
            "path": "bad-hook.sh",
            "kind": "hook",
            "content": bad_content,
        })
        self.assertTrue(any("shebang" in i.problem.lower() for i in issues),
                        f"expected shebang-related issue, got: {issues}")


class TestIronLawFixer(unittest.TestCase):
    def test_iron_law_fixer_checks_consistency(self):
        import fixers.iron_law as il
        f = il.IronLawFixer()
        drifted = "## §1 Iron Laws\n- **L1 Test-First**: write test then code\n"
        issues = f.diagnose(Path("/nonexistent"), {
            "path": "CLAUDE.md",
            "kind": "claude_md",
            "content": drifted,
        })
        self.assertTrue(len(issues) > 0, "expected consistency issue for drifted Iron Laws")


if __name__ == "__main__":
    unittest.main(verbosity=2)
