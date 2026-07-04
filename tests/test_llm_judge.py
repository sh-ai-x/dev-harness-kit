#!/usr/bin/env python3
"""test_llm_judge.py — RED-first tests for lib/llm_judge.py."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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
            "MINMAX_API_KEY": "",
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
        }) as mock_post:
            result = llm_judge.call_judge(
                provider="minimax",
                api_key="test-key",
                model="MiniMax-M3[1m]",
                prompt="test prompt",
            )
        self.assertEqual(result["scores"]["semantic_drift"], 8)
        self.assertEqual(result["tokens_in"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
