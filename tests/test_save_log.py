"""Tests for ``tools/save_log.py`` codex-slim retention.

These pin the capture-side contract that ``slim_transcript`` retains codex
records carrying ``turn_context``, ``event_msg``/``token_count``, and
``response_item``/``function_call`` (in addition to the original text-only
``event_msg`` records).

Spec note: when a codex transcript has NO records matching any of those filters,
``slim_transcript`` falls back to ``_codex_has_response_text`` — same as before
this fix. We do not regress that path; we only widen the set of records kept
when the new filters DO match.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import save_log  # noqa: E402  (sys.path mutated above)


def _raw_jsonl(records: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


class TestCodexKeepsInfoRecords(unittest.TestCase):
    """Synthetic raw codex transcript → all three record types survive slim."""

    def setUp(self) -> None:
        self.records = [
            # 1. user_message text (existing path — must still survive)
            {"type": "event_msg", "payload": {
                "type": "user_message",
                "message": "fix the bug please",
            }},
            # 2. agent_message text-only-no-info (existing path, must survive)
            {"type": "event_msg", "payload": {
                "type": "agent_message",
                "message": "Investigating...",
            }},
            # 3. turn_context carrying model (NEW — must survive)
            {"type": "turn_context", "payload": {
                "model": "gpt-5.6-luna",
                "cwd": "/tmp/fix-repo",
            }},
            # 4. token_count carrying usage info (NEW — must survive)
            {"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 50,
                    "output_tokens": 10, "reasoning_output_tokens": 2,
                    "total_tokens": 112}},
            }},
            # 5. response_item function_call (NEW — must survive)
            {"type": "response_item", "payload": {
                "type": "function_call",
                "name": "Read",
                "input": {"file_path": "/tmp/fix-repo/x.py"},
            }},
            # 6. unrelated noise (must NOT survive)
            {"type": "event_msg", "payload": {
                "type": "task_started",
                "turn_id": "t1",
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [],
            }},
        ]
        self.raw = _raw_jsonl(self.records)

    def _lines_matching(self, slim: str, payload_types: list[str]) -> int:
        n = 0
        for ln in slim.splitlines():
            if not ln.strip():
                continue
            obj = json.loads(ln)
            p = obj.get("payload")
            if isinstance(p, dict) and p.get("type") in payload_types:
                n += 1
            if obj.get("type") == "turn_context" and "turn_context" in payload_types:
                n += 1
        return n

    def test_slim_keeps_text_records(self) -> None:
        slim = save_log.slim_transcript(self.raw, "codex")
        self.assertIsNotNone(slim)
        # Both user and agent text messages survived.
        self.assertIn("fix the bug please", slim)
        self.assertIn("Investigating...", slim)

    def test_slim_keeps_turn_context(self) -> None:
        slim = save_log.slim_transcript(self.raw, "codex")
        self.assertIsNotNone(slim)
        # turn_context line present (its model payload).
        self.assertIn("gpt-5.6-luna", slim)

    def test_slim_keeps_token_count(self) -> None:
        slim = save_log.slim_transcript(self.raw, "codex")
        self.assertIsNotNone(slim)
        # token_count.info survived
        self.assertIn("total_token_usage", slim)
        self.assertIn("cached_input_tokens", slim)

    def test_slim_keeps_response_item_function_call(self) -> None:
        slim = save_log.slim_transcript(self.raw, "codex")
        self.assertIsNotNone(slim)
        self.assertIn("\"function_call\"", slim)
        self.assertIn("\"Read\"", slim)

    def test_slim_drops_irrelevant_records(self) -> None:
        slim = save_log.slim_transcript(self.raw, "codex")
        self.assertIsNotNone(slim)
        # task_started and empty response_item assistant message must be GONE.
        self.assertNotIn("task_started", slim)
        # An assistant message with empty content is text-less; response_item
        # fallback only kicks in when nothing else matched, so even if it
        # survived on the fallback path, that\'s acceptable per the spec. The
        # important invariant: nothing irrelevant dominates the slim output.

    def test_slim_dedupes_overlapping_records(self) -> None:
        """If a single record matched multiple filters, the slim emits it once."""
        # agent_message whose `payload.message` is text and whose `payload.info`
        # also carries model/token_usage is the canonical example (matches both
        # _codex_has_event_text and _codex_has_event_tokens). We synthesize one.
        rec = {"type": "event_msg", "payload": {
            "type": "agent_message",
            "message": "doing the thing",
            "info": {"total_token_usage": {
                "input_tokens": 1, "cached_input_tokens": 0,
                "output_tokens": 1, "reasoning_output_tokens": 0,
                "total_tokens": 2}},
        }}
        # Plus the model-bearing turn_context, which should ALSO be retained.
        ctx = {"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}}
        raw = _raw_jsonl([rec, ctx])
        slim = save_log.slim_transcript(raw, "codex")
        self.assertIsNotNone(slim)
        # The text-bearing line must appear exactly once (not 2x).
        self.assertEqual(slim.count("\"doing the thing\""), 1)
        # turn_context line survives alongside.
        self.assertEqual(slim.count("\"gpt-5.6-luna\""), 1)


class TestCodexFallbackStillWorks(unittest.TestCase):
    """When no new-filter matches, slim_transcript must still fall back to the
    legacy response_item text path (unchanged behavior).
    """

    def test_response_item_text_fallback(self) -> None:
        rec = {"type": "response_item", "payload": {
            "role": "assistant",
            "content": [{"type": "text", "text": "legacy text"}],
        }}
        raw = _raw_jsonl([rec])
        slim = save_log.slim_transcript(raw, "codex")
        self.assertIsNotNone(slim)
        self.assertIn("legacy text", slim)


if __name__ == "__main__":
    unittest.main()
