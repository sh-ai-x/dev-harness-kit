#!/usr/bin/env python3
"""test_eval_session.py — RED-first tests for the session-log judge +
golden-diff in lib/eval_runner.py (closes #285).

Two opt-in paths:

- `run_session_dim(session_log)` — judges one session log on 8 axes.
- `run_golden_diff(run_result)` — diffs a run_eval result against
  `eval/golden/*.json` and emits regression markers.

Both default OFF. These tests exercise the dry-run / mocked LLM path
so they are deterministic and require no API key.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import eval_runner  # noqa: E402
import llm_judge  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_LOGS = REPO_ROOT / "tests" / "fixtures" / "session_logs"
FIXTURE_GOLDEN = REPO_ROOT / "tests" / "fixtures" / "golden"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_fixture(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


class TestSessionAxes(unittest.TestCase):
    """The 8 axes are the SSOT for both the prompt and the runner."""

    def test_session_axes_count(self):
        self.assertEqual(len(eval_runner.SESSION_AXES), 8)


class TestSessionIdFromLog(unittest.TestCase):
    def test_derives_session_id_from_first_line(self):
        p = FIXTURE_LOGS / "good.jsonl"
        sid = eval_runner._session_id_from_log(p)
        self.assertEqual(sid, "sess-good-001")

    def test_falls_back_to_stem_when_no_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "no-sid.jsonl"
            tmp.write_text(
                '{"type":"user","message":{"role":"user",'
                '"content":[{"type":"text","text":"hi"}]}}\n',
                encoding="utf-8",
            )
            self.assertEqual(eval_runner._session_id_from_log(tmp),
                             "no-sid")

    def test_returns_empty_when_missing(self):
        self.assertEqual(
            eval_runner._session_id_from_log(Path("/nonexistent/a.jsonl")),
            "",
        )


class TestSummarizeSessionLog(unittest.TestCase):
    def test_includes_root_prompt_and_tool_counts(self):
        p = FIXTURE_LOGS / "good.jsonl"
        body = eval_runner._summarize_session_log(p)
        self.assertIn("ROOT USER PROMPT", body)
        self.assertIn("add a single helper", body)
        self.assertIn("TOOL COUNTS", body)
        self.assertIn("Read=1", body)

    def test_truncates_massive_logs(self):
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "huge.jsonl"
            with open(big, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "sessionId": "big",
                    "type": "user",
                    "message": {"role": "user",
                                "content": [{"type": "text",
                                             "text": "x" * 50000}]},
                }) + "\n")
            body = eval_runner._summarize_session_log(big, max_chars=200)
            self.assertLessEqual(len(body), 200)

    def test_unreadable_log_returns_empty(self):
        self.assertEqual(
            eval_runner._summarize_session_log(Path("/nope/x.jsonl")),
            "",
        )


class TestRunSessionDimDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_good = _copy_fixture(
            FIXTURE_LOGS / "good.jsonl",
            self.root / "good.jsonl",
        )
        self.config = {
            "provider": "minimax",
            "model": "MiniMax-M3[1m]",
            "api_key": "",  # force dry-run path
            "base_url": "x",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_returns_8_axes_at_seven(self):
        report = eval_runner.run_session_dim(
            self.root, self.root_good, config=self.config, dry_run=True,
        )
        self.assertEqual(set(report["scores"]), set(eval_runner.SESSION_AXES))
        self.assertEqual(len(report["scores"]), 8)
        for v in report["scores"].values():
            self.assertEqual(v, 7.0)
        self.assertEqual(report["verdict"], "DRIFT_WARNING")
        self.assertFalse(report["cached"])

    def test_dry_run_summary_shape(self):
        report = eval_runner.run_session_dim(
            self.root, self.root_good, config=self.config, dry_run=True,
        )
        # Round-trip via write_session_report to confirm shape.
        path = eval_runner.write_session_report(self.root, report)
        body = path.read_text(encoding="utf-8")
        for ax in eval_runner.SESSION_AXES:
            self.assertIn(ax, body)
        self.assertIn("DRIFT_WARNING", body)
        self.assertIn("sess-good-001", body)


class TestRunSessionDimCached(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_good = _copy_fixture(
            FIXTURE_LOGS / "good.jsonl",
            self.root / "good.jsonl",
        )
        self.config = {
            "provider": "minimax",
            "model": "MiniMax-M3[1m]",
            "api_key": "fake-key",
            "base_url": "https://api.minimax.io/anthropic",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_call_hits_cache_when_first_succeeds(self):
        scores = {ax: 9.0 for ax in eval_runner.SESSION_AXES}
        first_raw = {
            "scores": scores,
            "tokens_in": 100, "tokens_out": 50, "raw": "{}",
        }
        with patch.object(llm_judge, "call_judge",
                          return_value=first_raw) as mock_judge:
            r1 = eval_runner.run_session_dim(self.root, self.root_good,
                                             config=self.config)
            r2 = eval_runner.run_session_dim(self.root, self.root_good,
                                             config=self.config)
        # First call hits the network; second hits the cache.
        self.assertEqual(mock_judge.call_count, 1)
        self.assertFalse(r1["cached"])
        self.assertTrue(r2["cached"])
        self.assertEqual(r1["scores"], r2["scores"])
        self.assertEqual(r1["verdict"], "OK")

    def test_rot_not_cached(self):
        # First call raises -> ROT, no cache write.
        # After the #310 slice 315 narrowing, only (OSError, JSONDecodeError,
        # KeyError) are converted to ROT; RuntimeError must propagate so
        # the test exercises the same documented exception family.
        with patch.object(llm_judge, "call_judge",
                          side_effect=OSError("api down")):
            r1 = eval_runner.run_session_dim(self.root, self.root_good,
                                             config=self.config)
        # Second call should also hit the network (no cache from ROT).
        with patch.object(llm_judge, "call_judge",
                          return_value={"scores": {ax: 9.0
                                                    for ax in eval_runner.SESSION_AXES},
                                        "tokens_in": 1, "tokens_out": 1,
                                        "raw": "{}"}) as mock_judge:
            r2 = eval_runner.run_session_dim(self.root, self.root_good,
                                             config=self.config)
        self.assertEqual(r1["verdict"], "ROT")
        self.assertEqual(mock_judge.call_count, 1)
        self.assertFalse(r2["cached"])
        self.assertEqual(r2["verdict"], "OK")

    def test_empty_log_returns_rot_without_llm(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            with patch.object(llm_judge, "call_judge") as mock:
                r = eval_runner.run_session_dim(self.root, empty,
                                                config=self.config)
            mock.assert_not_called()
            self.assertEqual(r["verdict"], "ROT")
            self.assertIn("empty or unreadable", r["error"])


class TestRunGoldenDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.golden_dir = self.root / "eval" / "golden"
        self.golden_dir.mkdir(parents=True)
        # Seed two goldens. review-v1 will regress on recall;
        # plan-v1 will be unchanged.
        _write(self.golden_dir / "review-v1.json", json.dumps({
            "case_id": "review-v1", "dim": "review",
            "schema_version": "2.0.0",
            "baseline_hash": "sha256:abc",
            "expected": {"scores": {
                "verdict_consistency": 9.0, "severity_calibration": 9.0,
                "precision": 8.5, "recall": 9.0, "code_sanity_score": 8.0,
            }},
            "expected_behavior": "x", "iron_law_refs": [], "code_refs": [],
        }))
        _write(self.golden_dir / "plan-v1.json", json.dumps({
            "case_id": "plan-v1", "dim": "plan",
            "schema_version": "2.0.0",
            "baseline_hash": "sha256:def",
            "expected": {"scores": {
                "spec_clarity": 9.0, "step_atomicity": 8.5,
                "ac_executability": 9.0, "dependency_ordering": 8.0,
            }},
            "expected_behavior": "y", "iron_law_refs": [], "code_refs": [],
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, results: list) -> dict:
        run = {"results": results,
               "summary": {"OK": len(results), "DRIFT_WARNING": 0,
                           "ROT": 0, "SKIPPED": 0},
               "config": {}}
        return eval_runner.run_golden_diff(self.root, run)

    def test_regression_marker_emitted_when_axis_drops(self):
        results = [{
            "case_id": "review-v1", "dim": "review",
            "scores": {
                "verdict_consistency": 9.0, "severity_calibration": 9.0,
                "precision": 8.5, "recall": 5.0,  # -4.0 drop -> critical
                "code_sanity_score": 8.0,
            },
            "verdict": "DRIFT_WARNING",
        }]
        reg = self._run(results)
        self.assertEqual(reg["summary"]["markers"], 1)
        self.assertEqual(reg["summary"]["critical"], 1)
        m = reg["markers"][0]
        self.assertEqual(m["axis"], "recall")
        self.assertEqual(m["delta"], -4.0)

    def test_minor_drop_under_threshold_no_marker(self):
        results = [
            {"case_id": "review-v1", "dim": "review",
             "scores": {
                 "verdict_consistency": 9.0, "severity_calibration": 9.0,
                 "precision": 8.4, "recall": 8.9,  # -0.1, below -0.5
                 "code_sanity_score": 8.0,
             }, "verdict": "OK"},
            {"case_id": "plan-v1", "dim": "plan",
             "scores": {
                 "spec_clarity": 9.0, "step_atomicity": 8.5,
                 "ac_executability": 9.0, "dependency_ordering": 8.0,
             }, "verdict": "OK"},
        ]
        reg = self._run(results)
        self.assertEqual(reg["summary"]["markers"], 0)
        self.assertTrue(reg["summary"]["ok"])

    def test_added_and_removed_cases_detected(self):
        results = [
            {"case_id": "review-v1", "dim": "review",
             "scores": {"verdict_consistency": 9.0, "severity_calibration": 9.0,
                        "precision": 8.5, "recall": 9.0,
                        "code_sanity_score": 8.0}, "verdict": "OK"},
            # new case not in golden -> added
            {"case_id": "review-99-new", "dim": "review",
             "scores": {"verdict_consistency": 9.0, "severity_calibration": 9.0,
                        "precision": 8.5, "recall": 9.0,
                        "code_sanity_score": 8.0}, "verdict": "OK"},
            # plan-v1 absent from run -> removed
        ]
        reg = self._run(results)
        self.assertEqual(reg["summary"]["added_cases"], 1)
        self.assertEqual(reg["summary"]["removed_cases"], 1)
        self.assertIn("plan/plan-v1", reg["removed"])
        self.assertIn("review/review-99-new", reg["added"])
        self.assertFalse(reg["summary"]["ok"])  # removed -> not ok

    def test_missing_golden_dir_returns_clean_summary(self):
        import shutil
        shutil.rmtree(self.golden_dir)
        results = [{"case_id": "x", "dim": "review",
                    "scores": {}, "verdict": "OK"}]
        reg = self._run(results)
        self.assertEqual(reg["summary"]["markers"], 0)
        self.assertEqual(reg["summary"]["total_goldens"], 0)
        self.assertTrue(reg["summary"]["ok"])


class TestWriteReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_session_report_roundtrip(self):
        report = {
            "session_id": "sid-x",
            "log_path": "/x/y.jsonl",
            "scores": {ax: 8.0 for ax in eval_runner.SESSION_AXES},
            "verdict": "OK",
            "score": 8.0,
            "cached": False,
        }
        path = eval_runner.write_session_report(self.root, report)
        body = path.read_text(encoding="utf-8")
        self.assertIn("8-axis judge", body)
        self.assertIn("OK", body)
        self.assertEqual(path.parent.name, ".dev-kit")
        for ax in eval_runner.SESSION_AXES:
            self.assertIn(ax, body)

    def test_write_regression_report_roundtrip(self):
        reg = {
            "markers": [{
                "case_id": "review-v1", "dim": "review", "axis": "recall",
                "baseline": 9.0, "current": 5.0, "delta": -4.0,
                "severity": "critical",
            }],
            "added": ["review/review-99"],
            "removed": ["review/review-77"],
            "summary": {"total_goldens": 1, "total_run_cases": 1,
                        "added_cases": 1, "removed_cases": 1,
                        "markers": 1, "critical": 1, "major": 0,
                        "minor": 0, "ok": False},
            "config": {}, "baseline_hashes": {},
        }
        path = eval_runner.write_regression_report(self.root, reg)
        body = path.read_text(encoding="utf-8")
        self.assertIn("REGRESSION", body)
        self.assertIn("recall", body)
        self.assertIn("review-v1", body)
        self.assertIn("critical", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
