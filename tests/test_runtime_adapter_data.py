#!/usr/bin/env python3
"""Focused tests for canonical runtime token and session data adapters."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.runtime_adapters.base import SessionEvent as BaseSessionEvent  # noqa: E402
from lib.runtime_adapters.base import TokenLog as BaseTokenLog
from lib.runtime_adapters.sessions import SessionEvent, normalize_session_event  # noqa: E402
from lib.runtime_adapters.tokens import TokenLog, normalize_token_log  # noqa: E402


class TestRuntimeTokenData(unittest.TestCase):
    def test_base_token_log_import_is_the_canonical_class(self):
        self.assertIs(BaseTokenLog, TokenLog)
        self.assertEqual(TokenLog("7d", 11, 7, 3, 2).input_tokens, 11)

    def test_normalize_token_log_accepts_runtime_usage_aliases(self):
        result = normalize_token_log(
            {
                "usage": {
                    "input_tokens": "11",
                    "output_tokens": 7,
                    "cache_read_input_tokens": "3",
                    "cache_creation_input_tokens": 2,
                }
            },
            window="7d",
        )

        self.assertEqual(result, TokenLog("7d", 11, 7, 3, 2))

    def test_normalize_token_log_handles_codex_total_usage(self):
        result = normalize_token_log(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 80,
                            "cached_input_tokens": 30,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                        }
                    },
                },
            },
            window="today",
        )

        self.assertEqual(result, TokenLog("today", 50, 25, 30, 0))

    def test_normalize_token_log_uses_zero_for_missing_or_malformed_values(self):
        result = normalize_token_log(
            {"input_tokens": "bad", "output_tokens": -2, "cache_read_tokens": True},
            window="day",
        )

        self.assertEqual(result, TokenLog("day", 0, 0, 0, 0))


class TestRuntimeSessionData(unittest.TestCase):
    def test_base_session_event_import_is_the_canonical_class(self):
        self.assertIs(BaseSessionEvent, SessionEvent)
        event = SessionEvent(
            session_id="session-1",
            event_name="PreToolUse",
            timestamp=datetime.now(timezone.utc),
        )
        self.assertEqual(event.session_id, "session-1")

    def test_normalize_session_event_preserves_runtime_payload(self):
        result = normalize_session_event(
            {
                "session_id": "session-1",
                "event_name": "PreToolUse",
                "timestamp": "2026-07-23T10:00:00Z",
                "payload": {
                    "cwd": "/workspace/project",
                    "role": "assistant",
                    "task": "inspect files",
                },
            }
        )

        self.assertEqual(
            result,
            SessionEvent(
                session_id="session-1",
                event_name="PreToolUse",
                timestamp=datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc),
                payload={
                    "cwd": "/workspace/project",
                    "role": "assistant",
                    "task": "inspect files",
                },
            ),
        )

    def test_normalize_session_event_accepts_supplied_session_id_and_nested_type(self):
        result = normalize_session_event(
            {
                "type": "event_msg",
                "timestamp": "2026-07-23T10:00:01+00:00",
                "payload": {"type": "user_message", "message": "Review this"},
            },
            session_id="session-2",
        )

        self.assertEqual(result.session_id, "session-2")
        self.assertEqual(result.event_name, "user_message")
        self.assertEqual(dict(result.payload), {"type": "user_message", "message": "Review this"})

    def test_normalize_session_event_returns_none_for_malformed_input(self):
        self.assertIsNone(normalize_session_event({"event_name": "Stop"}))
        self.assertIsNone(
            normalize_session_event(
                {
                    "session_id": "session-1",
                    "event_name": "Stop",
                    "timestamp": "not-a-timestamp",
                }
            )
        )
        self.assertIsNone(normalize_session_event([]))


if __name__ == "__main__":
    unittest.main()
