#!/usr/bin/env python3
"""test_eval_runner.py — RED-first tests for lib/eval_runner.py."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import eval_runner  # noqa: E402


class TestEvalRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "skills").mkdir(parents=True)
        (self.root / "eval" / "golden").mkdir(parents=True)
        (self.root / "eval" / "prompts").mkdir(parents=True)
        (self.root / "eval" / "fixtures").mkdir(parents=True)
        # CLAUDE.md
        (self.root / "CLAUDE.md").write_text("# Test CLAUDE.md\nIron laws: L1 test\n", encoding="utf-8")
        # Skill
        (self.root / "skills").mkdir(exist_ok=True)
        sd = self.root / "skills" / "bootstrap"
        sd.mkdir()
        (sd / "sanity").mkdir()
        (sd / "sanity" / "SKILL.md").write_text(
            "---\nname: sanity\ncategory: bootstrap\ndescription: test\n---\n# Body\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_assets(self):
        # Should find CLAUDE.md + 1 skill = 2 assets
        assets = eval_runner.discover_assets(self.root)
        names = [a["path"] for a in assets]
        self.assertIn("CLAUDE.md", names)
        self.assertTrue(any("SKILL.md" in n for n in names))

    def test_discover_assets_includes_hooks(self):
        # Create a hook
        hooks_dir = self.root / ".claude-plugin" / "plugin" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "test.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
        assets = eval_runner.discover_assets(self.root)
        kinds = {a["kind"] for a in assets}
        self.assertIn("claude_md", kinds)
        self.assertIn("skill", kinds)
        self.assertIn("hook", kinds)

    def test_load_golden_or_default(self):
        path = self.root / "eval" / "golden" / "test.json"
        # missing → returns default structure
        result = eval_runner.load_golden(path)
        self.assertEqual(result["status"], "pending")

    def test_load_golden_existing(self):
        path = self.root / "eval" / "golden" / "test.json"
        path.write_text(json.dumps({
            "asset": "test",
            "version": "1.0.0",
            "summary": "demo",
            "expected_behavior": "...",
        }), encoding="utf-8")
        result = eval_runner.load_golden(path)
        self.assertEqual(result["asset"], "test")

    def test_save_golden(self):
        path = self.root / "eval" / "golden" / "test.json"
        eval_runner.save_golden(path, {
            "asset": "test",
            "version": "1.0.0",
            "summary": "demo",
        })
        self.assertTrue(path.exists())
        self.assertIn("test", path.read_text())

    def test_score_asset_full_pipeline_mocked_judge(self):
        # Mock the judge to return perfect scores
        with patch.object(eval_runner, "_judge_asset", return_value={
            "scores": {
                "semantic_drift": 9,
                "completeness": 10,
                "correctness": 9,
                "consistency": 10,
            },
            "tokens_in": 100,
            "tokens_out": 50,
            "raw": "mock",
            "verdict": "OK",
            "score": 9.5,
        }):
            asset = {"path": "CLAUDE.md", "kind": "claude_md", "content": "test"}
            result = eval_runner.score_asset(self.root, asset)
        self.assertEqual(result["verdict"], "OK")
        self.assertEqual(result["score"], 9.5)

    def test_score_asset_drift_warning(self):
        with patch.object(eval_runner, "_judge_asset", return_value={
            "scores": {
                "semantic_drift": 6,
                "completeness": 7,
                "correctness": 6,
                "consistency": 7,
            },
            "tokens_in": 50, "tokens_out": 30, "raw": "mock",
            "verdict": "DRIFT_WARNING",
            "score": 6.5,
        }):
            asset = {"path": "CLAUDE.md", "kind": "claude_md", "content": "test"}
            result = eval_runner.score_asset(self.root, asset)
        self.assertEqual(result["verdict"], "DRIFT_WARNING")

    def test_score_asset_rot(self):
        with patch.object(eval_runner, "_judge_asset", return_value={
            "scores": {
                "semantic_drift": 2,
                "completeness": 3,
                "correctness": 2,
                "consistency": 4,
            },
            "tokens_in": 30, "tokens_out": 20, "raw": "mock",
            "verdict": "ROT",
            "score": 2.75,
        }):
            asset = {"path": "CLAUDE.md", "kind": "claude_md", "content": "test"}
            result = eval_runner.score_asset(self.root, asset)
        self.assertEqual(result["verdict"], "ROT")


    def test_run_eval_writes_report(self):
        assets = [{"path": "CLAUDE.md", "kind": "claude_md", "content": "x"}]
        results = [{"path": "CLAUDE.md", "verdict": "OK", "score": 9.0}]
        report_path = self.root / ".dev-kit" / "eval-report.md"
        # Ensure parent dir
        report_path.parent.mkdir(parents=True, exist_ok=True)
        eval_runner.write_report(self.root, results, {"providers": "minimax"})
        self.assertTrue(report_path.exists())
        content = report_path.read_text()
        self.assertIn("OK", content)
        self.assertIn("9.0", content)

    def test_cross_check_agreement(self):
        # 2-judge cross-check (MUST-NOT-23)
        a = {"score": 8, "verdict": "OK"}
        b = {"score": 8, "verdict": "OK"}
        c = {"score": 5, "verdict": "DRIFT_WARNING"}
        # both same → agree
        self.assertTrue(eval_runner.cross_check_agree([a, b]))
        # disagree
        self.assertFalse(eval_runner.cross_check_agree([a, c]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
