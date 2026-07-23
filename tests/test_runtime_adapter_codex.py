#!/usr/bin/env python3
"""Focused behavior tests for the Codex runtime adapter."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.runtime_adapters.base import RuntimeAdapter, TokenLog  # noqa: E402
from lib.runtime_adapters.codex import CodexAdapter  # noqa: E402


class TestCodexAdapter(unittest.TestCase):
    def test_name_and_protocol_contract(self):
        adapter = CodexAdapter()

        self.assertEqual(adapter.name(), "codex")
        self.assertIsInstance(adapter, RuntimeAdapter)

    def test_is_current_requires_codex_environment_and_binary(self):
        adapter = CodexAdapter()

        with mock.patch.dict(os.environ, {"CODEX_HOME": "/Users/test/.codex"}, clear=True):
            with mock.patch("lib.runtime_adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                self.assertTrue(adapter.is_current())

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("lib.runtime_adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                self.assertFalse(adapter.is_current())

        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "session-1"}, clear=True):
            with mock.patch("lib.runtime_adapters.codex.shutil.which", return_value=None):
                self.assertFalse(adapter.is_current())

    def test_read_token_log_uses_codex_cumulative_token_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            records = [
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-23T10:00:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 12,
                                "cached_input_tokens": 2,
                                "output_tokens": 4,
                                "reasoning_output_tokens": 1,
                            }
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-23T10:00:01Z",
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
                {
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "Read"},
                },
            ]
            lines = [json.dumps(record) for record in records]
            lines.extend(["not-json", json.dumps(["not", "an", "object"])])
            (sessions / "7d.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = CodexAdapter(project_root=root).read_token_log("7d")

        self.assertEqual(
            result,
            TokenLog(
                window="7d",
                input_tokens=50,
                output_tokens=25,
                cache_read_tokens=30,
                cache_creation_tokens=0,
            ),
        )

    def test_read_token_log_returns_zero_for_missing_or_unsafe_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CodexAdapter(project_root=Path(tmp))

            self.assertEqual(adapter.read_token_log("missing"), TokenLog("missing", 0, 0))
            self.assertEqual(adapter.read_token_log("../outside"), TokenLog("../outside", 0, 0))

    def test_read_session_events_normalizes_codex_records_and_skips_malformed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_dir = root / ".codex" / "session-events"
            events_dir.mkdir(parents=True)
            records = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-23T10:00:00Z",
                    "payload": {"session_id": "untrusted-id", "cwd": "/workspace"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-23T10:00:01Z",
                    "payload": {"type": "user_message", "message": "Review this"},
                },
                {
                    "type": "turn_context",
                    "timestamp": "2026-07-23T10:00:02+00:00",
                    "payload": {"model": "gpt-5.3-codex"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-23T10:00:03Z",
                    "payload": {"type": "function_call", "name": "Read"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "not-a-timestamp",
                    "payload": {"type": "token_count", "info": {}},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-23T10:00:04Z",
                    "payload": ["not", "a", "mapping"],
                },
            ]
            lines = [json.dumps(record) for record in records]
            lines.append("not-json")
            (events_dir / "session-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = CodexAdapter(project_root=root).read_session_events("session-1")

        self.assertEqual(len(result), 4)
        self.assertEqual([event.session_id for event in result], ["session-1"] * 4)
        self.assertEqual(
            [event.event_name for event in result],
            ["session_meta", "user_message", "turn_context", "function_call"],
        )
        self.assertEqual(result[0].timestamp, datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(dict(result[1].payload), {"type": "user_message", "message": "Review this"})
        self.assertEqual(dict(result[3].payload), {"type": "function_call", "name": "Read"})

    def test_read_session_events_returns_empty_for_missing_or_unsafe_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CodexAdapter(project_root=Path(tmp))

            self.assertEqual(adapter.read_session_events("missing"), [])
            self.assertEqual(adapter.read_session_events("../outside"), [])

    def test_hook_event_name_maps_codex_native_names(self):
        adapter = CodexAdapter()

        self.assertEqual(adapter.hook_event_name("PreToolUse"), "before_tool_use")
        self.assertEqual(adapter.hook_event_name("PostToolUse"), "after_tool_use")
        self.assertEqual(adapter.hook_event_name("custom-event"), "custom-event")

    def test_prompt_user_uses_injected_callback(self):
        questions = []

        def prompt(question: str) -> str:
            questions.append(question)
            return "yes"

        adapter = CodexAdapter(prompt_callback=prompt)

        self.assertEqual(adapter.prompt_user("Continue?"), "yes")
        self.assertEqual(questions, ["Continue?"])

    def test_prompt_user_without_callback_fails_without_subprocess(self):
        adapter = CodexAdapter()

        with self.assertRaisesRegex(RuntimeError, "prompt callback"):
            adapter.prompt_user("Continue?")

    def test_workspace_root_precedence_is_explicit_then_environment_then_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            explicit = base / "explicit"
            from_env = base / "environment"
            cwd = base / "cwd"
            explicit.mkdir()
            from_env.mkdir()
            cwd.mkdir()

            with mock.patch.dict(os.environ, {"CODEX_PROJECT_DIR": str(from_env)}, clear=True):
                with mock.patch("lib.runtime_adapters.codex.Path.cwd", return_value=cwd):
                    self.assertEqual(CodexAdapter(project_root=explicit).workspace_root(), explicit.resolve())
                    self.assertEqual(CodexAdapter().workspace_root(), from_env.resolve())

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("lib.runtime_adapters.codex.Path.cwd", return_value=cwd):
                    self.assertEqual(CodexAdapter().workspace_root(), cwd.resolve())

    def test_install_skill_uses_injected_installer(self):
        calls = []

        def install(skill_name: str, skill_dir: Path) -> None:
            calls.append((skill_name, skill_dir))

        source = Path("/marketplace/dev-kit/skills/review")
        adapter = CodexAdapter(skill_installer=install)

        adapter.install_skill("review", source)

        self.assertEqual(calls, [("review", source)])

    def test_install_skill_without_installer_fails_deterministically(self):
        adapter = CodexAdapter()

        with self.assertRaisesRegex(RuntimeError, "skill installer"):
            adapter.install_skill("review", Path("skills/review"))


if __name__ == "__main__":
    unittest.main()
