#!/usr/bin/env python3
"""test_eval_runner.py — RED-first tests for lib/eval_runner.py (schema 2.0.0).

Targets the new agent-behavior eval: case-based discovery, transcript
replay, per-dim judge dispatch, per-dim report. Mocks the LLM judge so
the tests are deterministic.
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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_case(root: Path, dim: str, case_id: str, category: str, expected: dict) -> Path:
    case_dir = root / "eval" / "cases" / dim
    case_dir.mkdir(parents=True, exist_ok=True)
    case_path = case_dir / f"{case_id}.json"
    case_path.write_text(
        json.dumps(
            {
                "case_id": case_id,
                "dim": dim,
                "category": category,
                "expected": expected,
                "schema_version": "2.0.0",
            }
        ),
        encoding="utf-8",
    )
    return case_path


def _seed_transcript(root: Path, dim: str, case_id: str, agent_output: dict) -> Path:
    t_dir = root / "eval" / "transcripts" / dim
    t_dir.mkdir(parents=True, exist_ok=True)
    t_path = t_dir / f"{case_id}.json"
    t_path.write_text(
        json.dumps(
            {
                "case_id": case_id,
                "dim": dim,
                "agent_output": agent_output,
                "captured_at": "2026-07-09T19:00:00+09:00",
                "captured_by": "test",
            }
        ),
        encoding="utf-8",
    )
    return t_path


class TestDiscoverCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for dim in ("review", "security", "plan"):
            (self.root / "eval" / "cases" / dim).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovers_one_case_per_dim(self):
        for dim in ("review", "security", "plan"):
            _seed_case(self.root, dim, f"{dim}-01-test", "real-bug", {"verdict": "Blocked"})
        cases = eval_runner.discover_cases(self.root)
        kinds = sorted(c["dim"] for c in cases)
        self.assertEqual(kinds, ["plan", "review", "security"])
        self.assertEqual(len(cases), 3)

    def test_skips_case_with_wrong_dim_field(self):
        _write(
            self.root / "eval" / "cases" / "review" / "bad.json",
            json.dumps({"case_id": "bad", "dim": "security", "schema_version": "2.0.0"}),
        )
        cases = eval_runner.discover_cases(self.root)
        self.assertEqual(cases, [])

    def test_skips_unknown_dim_dir(self):
        (self.root / "eval" / "cases" / "weird-dim").mkdir()
        _write(
            self.root / "eval" / "cases" / "weird-dim" / "x.json",
            json.dumps({"case_id": "x", "dim": "weird-dim"}),
        )
        cases = eval_runner.discover_cases(self.root)
        self.assertEqual(cases, [])

    def test_no_cases_dir_returns_empty(self):
        cases = eval_runner.discover_cases(self.root)
        self.assertEqual(cases, [])

    def test_each_case_has_required_fields(self):
        _seed_case(self.root, "review", "review-01-test", "real-bug", {"verdict": "Blocked"})
        cases = eval_runner.discover_cases(self.root)
        for c in cases:
            for key in ("case_id", "dim", "expected", "schema_version", "raw_path"):
                self.assertIn(key, c, f"missing {key}")


class TestTranscriptIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_transcript_returns_none_when_missing(self):
        self.assertIsNone(eval_runner.load_transcript(self.root, "review", "nope"))

    def test_load_transcript_roundtrip(self):
        _seed_transcript(
            self.root, "review", "review-01", {"verdict": "Blocked", "findings": []}
        )
        t = eval_runner.load_transcript(self.root, "review", "review-01")
        self.assertIsNotNone(t)
        self.assertEqual(t["agent_output"]["verdict"], "Blocked")

    def test_save_transcript_atomic(self):
        p = eval_runner.save_transcript(
            self.root, "review", "review-01", {"agent_output": {"verdict": "Approve"}}
        )
        self.assertTrue(p.exists())
        self.assertIn("Approve", p.read_text())


class TestJudgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for dim in ("review", "security", "plan"):
            (self.root / "eval" / "cases" / dim).mkdir(parents=True)
            (self.root / "eval" / "transcripts" / dim).mkdir(parents=True)
        # Seed a review case + transcript.
        _seed_case(
            self.root, "review", "review-01-sql", "real-bug",
            {"verdict": "Blocked", "min_severity": "major"},
        )
        _seed_transcript(
            self.root, "review", "review-01-sql",
            {"verdict": "Blocked", "findings": [{"dim": "security", "severity": "critical"}]},
        )
        # A prompt template is required for _judge_case to render.
        _write(
            self.root / "eval" / "prompts" / "judge-review.md",
            "# judge-review\n${INPUT}\n${AGENT_OUTPUT}\n${EXPECTED}\n${RUBRIC}\n"
            "${CASE_ID} ${DIM} ${CATEGORY}\n",
        )
        _write(
            self.root / "eval" / "prompts" / "judge-code-sanity.md",
            "# code-sanity rubric (placeholder for tests)",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_judge_case_returns_5_axes_for_review(self):
        with patch.object(llm_judge, "call_judge", return_value={
            "scores": {ax: 9.0 for ax in llm_judge.DIM_AXES["review"]},
            "tokens_in": 1, "tokens_out": 1, "raw": "{}",
        }):
            case = eval_runner.discover_cases(self.root)[0]
            result = eval_runner.judge_case(self.root, case)
        self.assertEqual(set(result["scores"]), set(llm_judge.DIM_AXES["review"]))
        self.assertEqual(len(result["scores"]), 5)
        self.assertEqual(result["verdict"], "OK")
        self.assertEqual(result["score"], 9.0)

    def test_judge_case_missing_transcript_returns_skipped(self):
        # Case without a transcript.
        _seed_case(
            self.root, "plan", "plan-01-no-tx", "clear-spec", {"verdict": "Approve"}
        )
        case = eval_runner.discover_cases(self.root)
        plan_case = [c for c in case if c["case_id"] == "plan-01-no-tx"][0]
        with patch.object(llm_judge, "call_judge") as mock_judge:
            result = eval_runner.judge_case(self.root, plan_case)
        mock_judge.assert_not_called()  # no LLM call for missing transcript
        self.assertEqual(result["verdict"], "SKIPPED")


class TestRunEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for dim in ("review", "security", "plan"):
            (self.root / "eval" / "cases" / dim).mkdir(parents=True)
            (self.root / "eval" / "transcripts" / dim).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_no_api_key_returns_mocks(self):
        for dim in eval_runner.SUPPORTED_DIMS:
            _seed_case(self.root, dim, f"{dim}-01", "x", {"verdict": "OK"})
            _seed_transcript(
                self.root, dim, f"{dim}-01", {"verdict": "Approve", "findings": []}
            )
        report = eval_runner.run_eval(self.root, dry_run=True)
        n_dims = len(eval_runner.SUPPORTED_DIMS)
        self.assertEqual(report["summary"]["OK"], 0)
        self.assertEqual(report["summary"]["DRIFT_WARNING"], n_dims)
        self.assertEqual(report["summary"]["ROT"], 0)
        self.assertEqual(report["summary"]["SKIPPED"], 0)
        self.assertEqual(len(report["results"]), n_dims)

    def test_run_eval_writes_report(self):
        # Seed at least one case per dim so the per-dim table has content.
        for dim in llm_judge.DIM_AXES:
            _seed_case(self.root, dim, f"{dim}-01", "x", {"verdict": "OK"})
            _seed_transcript(
                self.root, dim, f"{dim}-01", {"verdict": "Approve", "findings": []}
            )
        eval_runner.run_eval(self.root, dry_run=True)
        out = self.root / ".dev-kit" / "eval-report.md"
        self.assertTrue(out.exists())
        body = out.read_text()
        self.assertIn("Per-Dimension Scores", body)
        self.assertIn("review", body)
        self.assertIn("security", body)
        self.assertIn("plan", body)

    def test_dim_filter(self):
        _seed_case(self.root, "review", "review-01", "x", {"verdict": "OK"})
        _seed_transcript(
            self.root, "review", "review-01", {"verdict": "Approve", "findings": []}
        )
        _seed_case(self.root, "plan", "plan-01", "x", {"verdict": "OK"})
        _seed_transcript(self.root, "plan", "plan-01", {"verdict": "Approve"})
        report = eval_runner.run_eval(self.root, dry_run=True, dim="plan")
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["dim"], "plan")

    def test_case_filter(self):
        _seed_case(self.root, "review", "review-01-a", "x", {"verdict": "OK"})
        _seed_transcript(self.root, "review", "review-01-a", {"verdict": "Approve"})
        _seed_case(self.root, "review", "review-02-b", "x", {"verdict": "OK"})
        _seed_transcript(self.root, "review", "review-02-b", {"verdict": "Approve"})
        report = eval_runner.run_eval(
            self.root, dry_run=True, case="review-02-b"
        )
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["case_id"], "review-02-b")

    def test_invalid_dim_raises(self):
        with self.assertRaises(ValueError):
            eval_runner.run_eval(self.root, dry_run=True, dim="bogus")

    def test_no_fixtures_dim_returns_no_fixtures_verdict(self):
        # P3b: --dim naming a dim with zero `eval/cases/<dim>/` fixtures
        # must NOT render as a clean "0 cases" pass; must instead return
        # a single NO_FIXTURES result.
        report = eval_runner.run_eval(self.root, dry_run=True, dim="harness")
        self.assertEqual(report["summary"]["NO_FIXTURES"], 1)
        self.assertEqual(report["summary"]["OK"], 0)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["verdict"], "NO_FIXTURES")
        self.assertEqual(report["results"][0]["dim"], "harness")

    def test_no_fixtures_with_empty_case_dir(self):
        (self.root / "eval" / "cases" / "os").mkdir(parents=True)
        # No *.json inside, just the dir.
        report = eval_runner.run_eval(self.root, dry_run=True, dim="os")
        self.assertEqual(report["summary"]["NO_FIXTURES"], 1)

    def test_no_dim_filter_never_triggers_no_fixtures(self):
        # Default (no --dim) run must NOT substitute NO_FIXTURES for dims
        # with no fixtures — those dims are simply absent from results,
        # exactly as before.
        _seed_case(self.root, "review", "review-01", "x", {"verdict": "OK"})
        _seed_transcript(self.root, "review", "review-01", {"verdict": "Approve"})
        report = eval_runner.run_eval(self.root, dry_run=True)
        self.assertEqual(report["summary"]["NO_FIXTURES"], 0)
        self.assertGreater(len(report["results"]), 0)

    def test_missing_transcript_marked_skipped(self):
        _seed_case(self.root, "review", "review-01", "x", {"verdict": "OK"})
        # No transcript seeded.
        report = eval_runner.run_eval(self.root, dry_run=True)
        self.assertEqual(report["summary"]["SKIPPED"], 1)

    def test_judge_api_error_marks_rot_continues(self):
        _seed_case(self.root, "review", "review-01", "x", {"verdict": "OK"})
        _seed_transcript(self.root, "review", "review-01", {"verdict": "Approve"})
        _write(
            self.root / "eval" / "prompts" / "judge-review.md",
            "# judge-review stub\n${CASE_ID} ${DIM} ${CATEGORY}\n${INPUT}\n${AGENT_OUTPUT}\n${EXPECTED}\n${RUBRIC}\n",
        )
        _write(
            self.root / "eval" / "prompts" / "judge-code-sanity.md",
            "# rubric stub",
        )
        with patch.object(
            llm_judge, "call_judge", side_effect=OSError("api down")
        ), patch.object(
            llm_judge, "load_config",
            return_value={"provider": "minimax", "model": "x",
                          "api_key": "fake", "base_url": "x"},
        ):
            report = eval_runner.run_eval(self.root, dry_run=False)
        self.assertEqual(report["summary"]["ROT"], 1)
        self.assertIn("api down", report["results"][0].get("error", ""))


# --- per-helper tests (issue #93) ---------------------------------------

class TestRenderSummary(unittest.TestCase):
    def test_block_includes_verdict_counts(self):
        results = [
            {"verdict": "OK"}, {"verdict": "OK"},
            {"verdict": "DRIFT_WARNING"}, {"verdict": "ROT"},
            {"verdict": "SKIPPED"},
        ]
        out = eval_runner._render_summary(results)
        self.assertIn("## Summary", out)
        self.assertIn("- Total cases: 5", out)
        self.assertIn("- OK: 2", out)
        self.assertIn("- DRIFT_WARNING: 1", out)
        self.assertIn("- ROT: 1", out)
        self.assertIn("- SKIPPED: 1", out)

    def test_block_handles_empty_results(self):
        out = eval_runner._render_summary([])
        self.assertIn("## Summary", out)
        self.assertIn("- Total cases: 0", out)


class TestRenderPerDimTable(unittest.TestCase):
    def test_block_includes_axes(self):
        results = [
            {"dim": "review", "verdict": "OK", "scores": {"precision": 9.0, "recall": 8.0}},
            {"dim": "review", "verdict": "DRIFT_WARNING", "scores": {"precision": 7.0, "recall": 7.0}},
        ]
        out = eval_runner._render_per_dim_table(results)
        self.assertIn("## Per-Dimension Scores", out)
        self.assertIn("### review", out)
        self.assertIn("| Axis | Mean |", out)
        self.assertIn("`precision`", out)
        self.assertIn("`recall`", out)


class TestRenderPerCase(unittest.TestCase):
    def test_block_includes_case_id_and_axes(self):
        results = [
            {"verdict": "OK", "case_id": "case-1", "dim": "review",
             "score": 9.0, "scores": {"precision": 9.0, "recall": 8.0}},
        ]
        out = eval_runner._render_per_case(results)
        self.assertIn("## Per-Case Results", out)
        self.assertIn("`case-1`", out)
        self.assertIn("dim=review", out)
        self.assertIn("precision=9.0", out)

    def test_error_field_surfaced_when_present(self):
        # P4: judge-infra failure must read differently from a genuine
        # behavior regression — both are ROT with score=0, but only the
        # infra one carries an `error` field.
        results = [
            {"verdict": "ROT", "case_id": "case-2", "dim": "review",
             "score": 0.0, "scores": {}, "error": "connection refused"},
        ]
        out = eval_runner._render_per_case(results)
        self.assertIn("error=connection refused", out)

    def test_no_error_suffix_when_error_absent(self):
        results = [
            {"verdict": "OK", "case_id": "case-3", "dim": "review",
             "score": 9.0, "scores": {}},
        ]
        out = eval_runner._render_per_case(results)
        self.assertNotIn("error=", out)


class TestInfraFailureBanner(unittest.TestCase):
    """P4 (eval-loop runtime hardening): a run whose cases are mostly
    ROT-with-error is far more likely to be a judge-infra failure than
    a real behavior regression. The banner prevents misdiagnosing the run.
    """

    def _rot_with_error(self, n: int) -> list:
        return [
            {"verdict": "ROT", "case_id": f"c-{i}", "dim": "review",
             "score": 0.0, "scores": {}, "error": "boom"}
            for i in range(n)
        ]

    def test_banner_emitted_when_all_cases_rot_with_error(self):
        results = self._rot_with_error(12)
        banner = eval_runner._infra_failure_banner(results)
        self.assertIn("INFRA_FAILURE", banner)
        self.assertIn("12/12", banner)

    def test_no_banner_when_ratio_under_threshold(self):
        results = self._rot_with_error(2) + [
            {"verdict": "OK", "case_id": "ok-1", "dim": "review",
             "score": 9.0, "scores": {}},
            {"verdict": "OK", "case_id": "ok-2", "dim": "review",
             "score": 9.0, "scores": {}},
        ]
        self.assertEqual(eval_runner._infra_failure_banner(results), "")

    def test_no_banner_when_rot_has_no_error(self):
        results = [
            {"verdict": "ROT", "case_id": f"c-{i}", "dim": "review",
             "score": 0.0, "scores": {}}
            for i in range(12)
        ]
        self.assertEqual(eval_runner._infra_failure_banner(results), "")

    def test_no_banner_on_empty_results(self):
        self.assertEqual(eval_runner._infra_failure_banner([]), "")

    def test_write_report_includes_banner_section(self):
        results = self._rot_with_error(5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = eval_runner.write_report(root, results, {"provider": "minimax"})
            body = path.read_text(encoding="utf-8")
            self.assertIn("INFRA_FAILURE", body)

    def test_write_report_omits_banner_when_clean(self):
        results = [
            {"verdict": "OK", "case_id": "ok-1", "dim": "review",
             "score": 9.0, "scores": {}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = eval_runner.write_report(root, results, {"provider": "minimax"})
            body = path.read_text(encoding="utf-8")
            self.assertNotIn("INFRA_FAILURE", body)


class TestDimHasCases(unittest.TestCase):
    """P3(b): cheap existence check used by `run_eval` to short-circuit."""

    def test_false_when_dim_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(eval_runner._dim_has_cases(Path(tmp), "harness"))

    def test_false_when_dim_dir_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "eval" / "cases" / "os").mkdir(parents=True)
            self.assertFalse(eval_runner._dim_has_cases(Path(tmp), "os"))

    def test_true_when_at_least_one_case_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "eval" / "cases" / "review").mkdir(parents=True)
            (root / "eval" / "cases" / "review" / "01.json").write_text("{}")
            self.assertTrue(eval_runner._dim_has_cases(root, "review"))


class TestWriteReportDispatcher(unittest.TestCase):
    def test_body_thin(self):
        import inspect
        source = inspect.getsource(eval_runner.write_report)
        logic_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertLess(
            len(logic_lines), 50,
            f"write_report too long: {len(logic_lines)} lines",
        )


class TestRunEvalDispatcher(unittest.TestCase):
    def test_body_thin(self):
        import inspect
        source = inspect.getsource(eval_runner.run_eval)
        logic_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertLess(
            len(logic_lines), 50,
            f"run_eval too long: {len(logic_lines)} lines",
        )


# --- shared helpers (issue #310 slice) -------------------------------------


class TestCoerceScore(unittest.TestCase):
    """`_coerce_score` is the shared coercion used by both `_judge_case`
    and `run_golden_diff`. Coerce-or-None is the contract: any non-numeric
    input returns None so the caller can drop or skip it.
    """

    def test_numeric_inputs_pass_through(self):
        self.assertEqual(eval_runner._coerce_score(1.0), 1.0)
        self.assertEqual(eval_runner._coerce_score(0), 0.0)
        self.assertEqual(eval_runner._coerce_score("3.5"), 3.5)

    def test_non_numeric_inputs_return_none(self):
        self.assertIsNone(eval_runner._coerce_score("nope"))
        self.assertIsNone(eval_runner._coerce_score(None))
        self.assertIsNone(eval_runner._coerce_score({}))


class TestSessionReportBuilder(unittest.TestCase):
    """`_session_report(...)` is the centralized builder used by every
    branch of `run_session_dim`. Same shape for dry-run / empty / real /
    exception paths — only the inputs differ.
    """

    def test_session_report_keys_locked(self):
        # The public report dict has the documented field set; if you
        # add or rename a field, downstream consumers break.
        keys = set(eval_runner._session_report(
            session_id="sid", log_path="/x/y.jsonl",
            scores={"a": 1.0}, verdict="OK", score=1.0,
            tokens_in=0, tokens_out=0, raw="", error=None,
            cached=False,
        ).keys())
        self.assertTrue(
            {"session_id", "log_path", "scores", "tokens_in", "tokens_out",
             "raw", "verdict", "score", "error", "cached", "summary"}
            <= keys,
            f"missing keys: {keys}",
        )

    def test_session_report_summary_is_attached(self):
        r = eval_runner._session_report(
            session_id="sid", log_path="/x", scores={"a": 9.0, "b": 8.0},
            verdict="OK", score=8.5,
            tokens_in=10, tokens_out=5, raw="{}", error=None, cached=False,
        )
        # `summary` is the canonical short shape consumers key off.
        self.assertIn("summary", r)
        self.assertEqual(r["summary"]["verdict"], "OK")
        self.assertEqual(r["summary"]["cached"], False)
        self.assertEqual(r["summary"]["axes"], 2)


class TestSummarizeSessionLogNormalized(unittest.TestCase):
    """`_summarize_session_log` is split into per-field helpers so the
    main function reads top-down without embedded nested loops.
    """

    def test_root_prompt_extracted_from_string_content(self):
        msg = {"content": "hello"}
        self.assertEqual(eval_runner._extract_root_prompt(msg), "hello")

    def test_root_prompt_extracted_from_block_list(self):
        msg = {"content": [
            {"type": "text", "text": "first"},
            {"type": "tool_use", "name": "Read"},
        ]}
        self.assertEqual(eval_runner._extract_root_prompt(msg), "first")

    def test_root_prompt_extracted_from_text_blocks(self):
        msg = {"content": [
            {"type": "text", "text": "block-text"},
        ]}
        self.assertEqual(eval_runner._extract_root_prompt(msg), "block-text")

    def test_root_prompt_returns_empty_for_other_shapes(self):
        self.assertEqual(eval_runner._extract_root_prompt({}), "")
        self.assertEqual(eval_runner._extract_root_prompt(None), "")


class TestSessionCacheContentAware(unittest.TestCase):
    """The cache key must include log content (mtime+size is enough)
    so two logs with the same session_id but different content don't
    share a stale cache entry. Stale cache = silently-wrong verdict.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "session.jsonl"
        self.log.write_text(
            '{"sessionId":"sid-A","type":"user",'
            '"message":{"content":"hello"}}\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_key_includes_log_path_and_size(self):
        key_a = eval_runner._session_cache_key(self.log, "sid-A")
        # Same session id, different log path → distinct key
        other = self.root / "other.jsonl"
        other.write_text('{"sessionId":"sid-A"}\n', encoding="utf-8")
        key_b = eval_runner._session_cache_key(other, "sid-A")
        self.assertNotEqual(key_a, key_b)

    def test_cache_path_includes_content_hash(self):
        # The cache file path is keyed off (session_id, content_hash).
        p1 = eval_runner._session_cache_path(self.root, "sid-A", self.log)
        # Mutating the log file (different size/content) produces a
        # different cache path so stale entries are bypassed.
        self.log.write_text(
            '{"sessionId":"sid-A","type":"user",'
            '"message":{"content":"hello"}}\n'
            '{"type":"assistant","message":{"content":"more"}}\n',
            encoding="utf-8",
        )
        p2 = eval_runner._session_cache_path(self.root, "sid-A", self.log)
        self.assertNotEqual(p1, p2)

    def test_cache_hit_invalidated_when_log_changes(self):
        # Run 1: cache populated
        config = {
            "provider": "minimax", "model": "x",
            "api_key": "fake-key",
            "base_url": "https://api.minimax.io/anthropic",
        }
        scores_high = {ax: 9.0 for ax in eval_runner.SESSION_AXES}
        with patch.object(llm_judge, "call_judge", return_value={
            "scores": scores_high,
            "tokens_in": 1, "tokens_out": 1, "raw": "{}",
        }):
            eval_runner.run_session_dim(self.root, self.log, config=config)
        # Mutate log (different content)
        self.log.write_text(
            '{"sessionId":"sid-A","type":"user",'
            '"message":{"content":"DIFFERENT"}}\n',
            encoding="utf-8",
        )
        scores_low = {ax: 4.0 for ax in eval_runner.SESSION_AXES}
        with patch.object(llm_judge, "call_judge", return_value={
            "scores": scores_low,
            "tokens_in": 1, "tokens_out": 1, "raw": "{}",
        }) as mock_judge:
            r2 = eval_runner.run_session_dim(
                self.root, self.log, config=config,
            )
        # Second call hits the LLM because cache was content-keyed.
        self.assertEqual(mock_judge.call_count, 1)
        self.assertFalse(r2["cached"])
        self.assertEqual(r2["score"], 4.0)


class TestReportDictsPreserved(unittest.TestCase):
    """The public report dicts MUST keep their existing shape — every
    downstream consumer keys off them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for dim in ("review", "security", "plan"):
            (self.root / "eval" / "cases" / dim).mkdir(parents=True)
            (self.root / "eval" / "transcripts" / dim).mkdir(parents=True)
        _seed_case(self.root, "review", "review-01", "x", {"verdict": "OK"})
        _seed_transcript(
            self.root, "review", "review-01", {"verdict": "Approve"},
        )
        self.log_path = self.root / "session.jsonl"
        self.log_path.write_text(
            '{"sessionId":"sid","type":"user",'
            '"message":{"content":"hi"}}\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_eval_summary_keys_locked(self):
        report = eval_runner.run_eval(self.root, dry_run=True)
        self.assertEqual(set(report.keys()),
                         {"results", "config", "summary", "harness_effectiveness"})
        self.assertEqual(
            set(report["summary"].keys()),
            {"OK", "DRIFT_WARNING", "ROT", "SKIPPED", "NO_FIXTURES"},
        )

    def test_run_session_dim_keys_locked(self):
        report = eval_runner.run_session_dim(
            self.root, self.log_path, dry_run=True,
        )
        self.assertEqual(
            set(report.keys()),
            {"session_id", "log_path", "scores", "tokens_in", "tokens_out",
             "raw", "verdict", "score", "error", "cached", "summary"},
        )

    def test_run_golden_diff_keys_locked(self):
        run = {"results": [], "summary": {}, "config": {}}
        reg = eval_runner.run_golden_diff(self.root, run)
        self.assertEqual(
            set(reg.keys()),
            {"markers", "added", "removed", "summary",
             "config", "baseline_hashes"},
        )


class TestCliConflictRejection(unittest.TestCase):
    """`build_parser` + `_validate_cli_args` reject mutually-exclusive
    flag combos and missing prerequisites. The CLI is the user-facing
    surface — a silent typo (e.g. `--session-log` with no log path) must
    error, not produce a misleading summary.

    Issue #310: `--session-log` and `--golden-diff` are now enforced as
    a native argparse `add_mutually_exclusive_group`, so the parser
    itself rejects the combination with exit 2. `--session-log` also
    declares `conflicts_with` against `--dim` / `--case`. The remaining
    prerequisite checks (`--write-*` requires its mode flag) still
    live in `_validate_cli_args`.
    """

    def _ns(
        self,
        session_log=None,
        write_session_report=False,
        golden_diff=False,
        write_regression_report=False,
        dim=None,
        case=None,
    ):
        import argparse
        return argparse.Namespace(
            session_log=session_log,
            write_session_report=write_session_report,
            golden_diff=golden_diff,
            write_regression_report=write_regression_report,
            dim=dim,
            case=case,
        )

    # ---- argparse-level checks (issue #310) ---------------------------

    def test_session_log_and_golden_diff_mutually_exclusive_via_parser(self):
        """`--session-log + --golden-diff` is rejected at parse time with exit 2.

        Previously this combination silently selected one mode via the
        precedence `if args.session_log: ... else: ...` in `main()`. The
        fix promotes the check into a `add_mutually_exclusive_group()` so
        argparse itself emits the error and exit code.
        """
        parser = eval_runner.build_parser()
        with self.assertRaises(SystemExit) as cm:
            parser.parse_args([
                "--session-log", "/tmp/x.jsonl",
                "--golden-diff",
            ])
        # argparse uses exit code 2 for usage errors.
        self.assertEqual(cm.exception.code, 2)

    def test_session_log_alone_passes_via_parser(self):
        parser = eval_runner.build_parser()
        ns = parser.parse_args(["--session-log", "/tmp/x.jsonl"])
        self.assertEqual(ns.session_log, "/tmp/x.jsonl")
        self.assertFalse(ns.golden_diff)

    def test_golden_diff_alone_passes_via_parser(self):
        parser = eval_runner.build_parser()
        ns = parser.parse_args(["--golden-diff"])
        self.assertTrue(ns.golden_diff)
        self.assertIsNone(ns.session_log)

    def test_no_args_passes_via_parser(self):
        parser = eval_runner.build_parser()
        ns = parser.parse_args([])
        self.assertIsNone(ns.session_log)
        self.assertFalse(ns.golden_diff)

    def test_session_log_rejects_dim_filter(self):
        """`--session-log + --dim` is rejected: per-dim filters only apply
        to the per-dim `run_eval` path, not the session-log judge.
        Lives in `_validate_cli_args` because argparse on the runtime
        Python (3.13) does not expose `conflicts_with` for declaratively
        expressing this cross-group conflict.
        """
        with self.assertRaises(SystemExit):
            eval_runner._validate_cli_args(self._ns(
                session_log=Path("/x.jsonl"),
                dim="review",
            ))

    def test_session_log_rejects_case_filter(self):
        with self.assertRaises(SystemExit):
            eval_runner._validate_cli_args(self._ns(
                session_log=Path("/x.jsonl"),
                case="foo",
            ))

    # ---- prerequisite checks (still in _validate_cli_args) ------------

    def test_write_session_report_requires_session_log(self):
        with self.assertRaises(SystemExit):
            eval_runner._validate_cli_args(self._ns(
                write_session_report=True,
            ))

    def test_write_regression_report_requires_golden_diff(self):
        with self.assertRaises(SystemExit):
            eval_runner._validate_cli_args(self._ns(
                write_regression_report=True,
            ))

    def test_no_args_passes(self):
        # Plain per-dim run with no extras → no error
        eval_runner._validate_cli_args(self._ns())

    def test_session_log_alone_passes(self):
        eval_runner._validate_cli_args(self._ns(
            session_log=Path("/x.jsonl"),
        ))

    def test_golden_diff_alone_passes(self):
        eval_runner._validate_cli_args(self._ns(
            golden_diff=True,
        ))


# helpers used by TestReportDictsPreserved (removed — inlined via setUp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
