#!/usr/bin/env python3
"""test_eval_hygiene.py — Tests for /dev-kit:eval dry-run + golden cross-check."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import eval_runner  # noqa: E402


class TestEvalDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # minimal asset
        (self.root / "CLAUDE.md").write_text("# test\nIron laws: L1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_no_api_key_skips_llm(self):
        # No MINMAX_API_KEY in env → dry_run branches activate
        result = eval_runner.run_eval(self.root, dry_run=True)
        self.assertGreater(len(result["results"]), 0)
        # all should be DRIFT_WARNING (mock 7.0)
        for r in result["results"]:
            self.assertEqual(r["verdict"], "DRIFT_WARNING")

    def test_run_eval_writes_report_at_correct_path(self):
        eval_runner.run_eval(self.root, dry_run=True)
        report = self.root / ".dev-kit" / "eval-report.md"
        self.assertTrue(report.exists())

    def test_golden_load_includes_metadata(self):
        # Each golden baseline should have required fields per ADR-0014
        golden_dir = self.root / "eval" / "golden"
        golden_dir.mkdir(parents=True)
        sample = golden_dir / "test.json"
        eval_runner.save_golden(sample, {
            "asset": "TEST",
            "schema_version": "1.0.0",
            "baseline_hash": "sha256:abc",
            "summary": "demo",
            "expected_behavior": "test",
            "iron_law_refs": [],
            "code_refs": ["TEST"],
            "captured_at": "2026-07-04",
        })
        loaded = eval_runner.load_golden(sample)
        for key in ("asset", "schema_version", "baseline_hash", "summary",
                    "expected_behavior", "iron_law_refs", "code_refs"):
            self.assertIn(key, loaded, f"missing {key}")


class TestEvalRealGolden(unittest.TestCase):
    """Verify that the real eval/golden/*.json files exist + are valid."""

    def test_real_golden_files_exist(self):
        root = Path(__file__).parent.parent
        golden_dir = root / "eval" / "golden"
        if not golden_dir.exists():
            self.skipTest("golden dir not generated yet")
        files = list(golden_dir.glob("*.json"))
        self.assertGreater(len(files), 5, f"expected >5 golden baselines, got {len(files)}")

    def test_real_golden_files_valid_json(self):
        root = Path(__file__).parent.parent
        golden_dir = root / "eval" / "golden"
        if not golden_dir.exists():
            self.skipTest("golden dir not generated")
        for f in list(golden_dir.glob("*.json"))[:5]:
            data = json.loads(f.read_text())
            self.assertEqual(data["schema_version"], "1.0.0")
            self.assertIn("asset", data)
            self.assertIn("baseline_hash", data)

    def test_eval_prompts_exist(self):
        root = Path(__file__).parent.parent
        prompts = root / "eval" / "prompts"
        for fname in ("judge-skill", "judge-claude-md", "judge-hook"):
            path = prompts / f"{fname}.md"
            self.assertTrue(path.exists(), f"missing: {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
