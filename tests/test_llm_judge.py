#!/usr/bin/env python3
"""test_llm_judge.py — RED-first tests for lib/llm_judge.py."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import llm_judge  # noqa: E402


class TestLlmJudge(unittest.TestCase):
    """LLM-as-judge scores 4 axes via MiniMax API."""

    def test_judge_axes_defaults(self):
        self.assertEqual(
            llm_judge.JUDGE_AXES,
            ("semantic_drift", "completeness", "correctness", "consistency"),
        )

    def test_load_config_defaults(self):
        # no env → defaults
        with patch.dict(os.environ, {
            "JUDGE_PROVIDER": "",
            "JUDGE_MODEL": "",
            "MINIMAX_API_KEY": "",
        }, clear=True):
            cfg = llm_judge.load_config(Path(tempfile.mkdtemp()))
            self.assertEqual(cfg["provider"], "minimax")
            self.assertEqual(cfg["model"], "MiniMax-M3[1m]")

    def test_load_config_env_override(self):
        with patch.dict(os.environ, {
            "JUDGE_PROVIDER": "anthropic",
            "JUDGE_MODEL": "claude-3-5-sonnet",
            "ANTHROPIC_API_KEY": "test-key",
        }, clear=True):
            cfg = llm_judge.load_config(Path(tempfile.mkdtemp()))
            self.assertEqual(cfg["provider"], "anthropic")
            self.assertEqual(cfg["model"], "claude-3-5-sonnet")

    def test_score_axes_aggregate(self):
        axes = {
            "semantic_drift": 8,
            "completeness": 9,
            "correctness": 7,
            "consistency": 10,
        }
        self.assertEqual(llm_judge.score_aggregate(axes), 8.5)

    def test_verdict_from_score(self):
        # ≥ 8 → OK; 5~7 → DRIFT_WARNING; < 5 → ROT
        self.assertEqual(llm_judge.verdict_from_score(8.5), "OK")
        self.assertEqual(llm_judge.verdict_from_score(6.0), "DRIFT_WARNING")
        self.assertEqual(llm_judge.verdict_from_score(3.0), "ROT")
        self.assertEqual(llm_judge.verdict_from_score(8.0), "OK")  # boundary

    def test_prompt_format_substitutes(self):
        # Skill judge prompt template + asset content substitution
        path = Path(tempfile.mkdtemp())
        (path / "eval").mkdir()
        (path / "eval" / "prompts").mkdir()
        (path / "eval" / "prompts" / "judge-skill.md").write_text(
            "Evaluate this skill: ${SKILL_BODY}\nName: ${SKILL_NAME}\n",
            encoding="utf-8",
        )
        out = llm_judge.format_prompt(path, "judge-skill", {
            "SKILL_BODY": "...",
            "SKILL_NAME": "demo",
        })
        self.assertIn("Name: demo", out)

    def test_parse_scores_valid_json(self):
        text = '{"semantic_drift":8, "completeness":9, "correctness":7, "consistency":10}'
        scores = llm_judge.parse_scores_json(text)
        self.assertEqual(scores["semantic_drift"], 8)

    def test_parse_scores_extract_from_text(self):
        # when JSON is wrapped in text, extract
        text = """Here is my eval:
        {"semantic_drift": 7, "completeness": 8, "correctness": 6, "consistency": 9}
        done."""
        scores = llm_judge.parse_scores_json(text)
        self.assertEqual(scores["completeness"], 8)

    def test_call_minimax_mocked(self):
        with patch.object(llm_judge, "_http_post", return_value={
            "content": [
                {"type": "text", "text": '{"semantic_drift":8,"completeness":9,"correctness":10,"consistency":9}'}
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }):
            result = llm_judge.call_judge(
                provider="minimax",
                api_key="test-key",
                model="MiniMax-M3[1m]",
                prompt="test prompt",
            )
        self.assertEqual(result["scores"]["semantic_drift"], 8)
        self.assertEqual(result["tokens_in"], 100)

    def test_call_judge_forwards_axes_to_parser(self):
        # Regression: call_judge must forward its `axes` kwarg to
        # parse_scores_json via _call_anthropic_compatible, so a per-dim
        # response (e.g. plan axes) is parsed against the per-dim keys
        # instead of falling back to the legacy JUDGE_AXES tuple (which
        # forces every per-dim score to 0.0).
        plan_response = json.dumps({
            "spec_clarity": 9,
            "step_atomicity": 8,
            "ac_executability": 7,
            "dependency_ordering": 10,
        })
        with patch.object(llm_judge, "_http_post", return_value={
            "content": [{"type": "text", "text": plan_response}],
            "usage": {"input_tokens": 42, "output_tokens": 31},
        }):
            result = llm_judge.call_judge(
                provider="minimax",
                api_key="test-key",
                model="MiniMax-M3[1m]",
                prompt="test prompt",
                axes=llm_judge.DIM_AXES["plan"],
            )
        scores = result["scores"]
        self.assertEqual(scores["spec_clarity"], 9)
        self.assertEqual(scores["step_atomicity"], 8)
        self.assertEqual(scores["ac_executability"], 7)
        self.assertEqual(scores["dependency_ordering"], 10)
        # No zero-default leakage from the legacy axes.
        self.assertEqual(len(scores), len(llm_judge.DIM_AXES["plan"]))


class TestDimAxes(unittest.TestCase):
    """Per-dim axis tuples for the new agent-behavior eval."""

    def test_dim_axes_defined_for_three_dims(self):
        for dim in ("review", "security", "plan"):
            self.assertIn(dim, llm_judge.DIM_AXES)
            self.assertIsInstance(llm_judge.DIM_AXES[dim], tuple)
            self.assertGreater(len(llm_judge.DIM_AXES[dim]), 0)

    def test_review_axes_count_is_5(self):
        self.assertEqual(len(llm_judge.DIM_AXES["review"]), 5)

    def test_security_axes_count_is_3(self):
        self.assertEqual(len(llm_judge.DIM_AXES["security"]), 3)

    def test_plan_axes_count_is_4(self):
        self.assertEqual(len(llm_judge.DIM_AXES["plan"]), 4)

    def test_review_axes_includes_code_sanity_score(self):
        self.assertIn("code_sanity_score", llm_judge.DIM_AXES["review"])

    def test_security_axes_includes_owasp_classification(self):
        self.assertIn(
            "owasp_classification_accuracy", llm_judge.DIM_AXES["security"]
        )

    def test_parse_scores_with_dim_axes(self):
        # Parse JSON using the security dim's axes, ignoring the legacy
        # JUDGE_AXES fields the model may also have emitted.
        text = json.dumps({
            "owasp_classification_accuracy": 9,
            "severity_accuracy": 8,
            "precision": 10,
            # legacy axes that should be ignored
            "semantic_drift": 5,
            "completeness": 5,
        })
        scores = llm_judge.parse_scores_json(text, axes=llm_judge.DIM_AXES["security"])
        self.assertEqual(scores["owasp_classification_accuracy"], 9)
        self.assertEqual(scores["severity_accuracy"], 8)
        self.assertEqual(scores["precision"], 10)
        # Legacy axes not in security DIM_AXES -> not returned.
        self.assertNotIn("semantic_drift", scores)
        self.assertNotIn("completeness", scores)

    def test_parse_scores_default_axis_unchanged(self):
        # Backward compat: default axes still works as before.
        text = '{"semantic_drift":7,"completeness":8,"correctness":6,"consistency":9}'
        scores = llm_judge.parse_scores_json(text)
        self.assertEqual(set(scores), set(llm_judge.JUDGE_AXES))


class TestGateDynamicDim(unittest.TestCase):
    """v1.1.0 — the gate-dynamic dim used by `lib/gate_dynamic.py`."""

    def test_dim_defined_with_three_axes(self):
        self.assertIn("gate_dynamic", llm_judge.DIM_AXES)
        self.assertEqual(
            llm_judge.DIM_AXES["gate_dynamic"],
            ("gate_skippable", "confidence", "risk_level"),
        )

    def test_risk_level_is_lower_is_better(self):
        # The orchestrator relies on `AXIS_POLARITY["gate_dynamic"]`
        # (or a per-axis entry) so the 0-10 score inverts before
        # thresholding — a low risk_score means safer, not riskier.
        # The polarity dict may declare either per-axis or per-dim;
        # what matters is that normalize_for_verdict inverts the axis.
        from llm_judge import normalize_for_verdict  # noqa: F401
        # Per-axis polarity convention:
        if "gate_dynamic.risk_level" in llm_judge.AXIS_POLARITY:
            self.assertEqual(
                llm_judge.AXIS_POLARITY["gate_dynamic.risk_level"],
                "lower_is_better",
            )
        # Either way, the invert helper must flip a low risk score
        # into a high verdict-relevant score:
        inverted = 10.0 - 2.0  # risk_level=2 → safe → high verdict score
        self.assertEqual(inverted, 8.0)

    def test_parse_scores_with_gate_dynamic_axes(self):
        text = json.dumps({
            "gate_skippable": 7,
            "confidence": 9,
            "risk_level": 2,
        })
        scores = llm_judge.parse_scores_json(
            text, axes=llm_judge.DIM_AXES["gate_dynamic"]
        )
        self.assertEqual(scores["gate_skippable"], 7)
        self.assertEqual(scores["confidence"], 9)
        self.assertEqual(scores["risk_level"], 2)


class TestCallJudgeTemperature(unittest.TestCase):
    """v1.1.0 — `call_judge` accepts `temperature` (default 1.0)."""

    def test_call_judge_default_temperature_is_1(self):
        import inspect
        sig = inspect.signature(llm_judge.call_judge)
        self.assertIn("temperature", sig.parameters)
        self.assertEqual(sig.parameters["temperature"].default, 1.0)

    def test_call_judge_passes_temperature_to_payload(self):
        # Mock _http_post and capture the payload sent to the API.
        captured = {}
        def fake_post(*, url, payload, api_key, timeout):
            captured.update(payload=payload)
            return {
                "content": [{"type": "text",
                             "text": json.dumps({"semantic_drift": 8,
                                                 "completeness": 8,
                                                 "correctness": 8,
                                                 "consistency": 8})}],
                "usage": {},
            }
        with mock.patch.object(llm_judge, "_http_post", side_effect=fake_post):
            llm_judge.call_judge(
                provider="minimax",
                api_key="fake",
                model="m",
                prompt="x",
                temperature=0,
            )
        self.assertEqual(captured["payload"]["temperature"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---- interview_ambiguity polarity (Phase 6 review finding) ----

class TestAxisPolarity:
    """The interview_ambiguity dim is 0=clear / 10=ambiguous (lower=better).
    verdict_from_score treats higher=better. normalize_for_verdict must
    invert so the aggregate path produces the correct verdict.
    """

    def test_interview_ambiguity_0_inverts_to_10(self):
        from llm_judge import normalize_for_verdict
        # A perfectly clear interview (all fields 0 ambiguity) should
        # become 10 after inversion so verdict_from_score returns OK.
        assert normalize_for_verdict("interview_ambiguity", 0.0) == 10.0

    def test_interview_ambiguity_10_inverts_to_0(self):
        from llm_judge import normalize_for_verdict
        # Maximally ambiguous (score 10) inverts to 0 -> ROT.
        assert normalize_for_verdict("interview_ambiguity", 10.0) == 0.0

    def test_normal_dim_passthrough(self):
        from llm_judge import normalize_for_verdict
        # Other dims (e.g. review, plan) are higher=better; pass through.
        for dim in ("review", "plan", "security"):
            assert normalize_for_verdict(dim, 8.5) == 8.5
            assert normalize_for_verdict(dim, 0.0) == 0.0

    def test_polarity_dict_covers_interview(self):
        from llm_judge import AXIS_POLARITY
        assert AXIS_POLARITY.get("interview_ambiguity") == "lower_is_better"
