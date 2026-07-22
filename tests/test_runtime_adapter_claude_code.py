#!/usr/bin/env python3
"""Focused behavior tests for the Claude Code runtime adapter."""
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
from lib.runtime_adapters.claude_code import ClaudeCodeAdapter  # noqa: E402


class TestClaudeCodeAdapter(unittest.TestCase):
    def test_name_and_protocol_contract(self):
        adapter = ClaudeCodeAdapter()

        self.assertEqual(adapter.name(), "claude-code")
        self.assertIsInstance(adapter, RuntimeAdapter)

    def test_is_current_requires_claude_environment_and_binary(self):
        adapter = ClaudeCodeAdapter()

        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
            with mock.patch("lib.runtime_adapters.claude_code.shutil.which", return_value="/usr/bin/claude"):
                self.assertTrue(adapter.is_current())

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("lib.runtime_adapters.claude_code.shutil.which", return_value="/usr/bin/claude"):
                self.assertFalse(adapter.is_current())

        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/workspace"}, clear=True):
            with mock.patch("lib.runtime_adapters.claude_code.shutil.which", return_value=None):
                self.assertFalse(adapter.is_current())

    def test_read_token_log_aggregates_claude_session_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            records = [
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 7,
                            "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 3,
                        }
                    },
                },
                {"type": "user", "message": {"usage": {"input_tokens": 1000}}},
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": "4",
                            "output_tokens": 2,
                            "cache_read_input_tokens": "invalid",
                        }
                    },
                },
            ]
            lines = [json.dumps(record) for record in records]
            lines.extend(["not-json", json.dumps(["not", "an", "object"])])
            (sessions / "7d.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = ClaudeCodeAdapter(project_root=root).read_token_log("7d")

        self.assertEqual(
            result,
            TokenLog(
                window="7d",
                input_tokens=15,
                output_tokens=9,
                cache_read_tokens=5,
                cache_creation_tokens=3,
            ),
        )

    def test_read_token_log_returns_zero_for_missing_or_unsafe_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ClaudeCodeAdapter(project_root=Path(tmp))

            self.assertEqual(adapter.read_token_log("missing"), TokenLog("missing", 0, 0))
            self.assertEqual(adapter.read_token_log("../outside"), TokenLog("../outside", 0, 0))

    def test_read_session_events_normalizes_valid_records_and_skips_malformed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_dir = root / ".claude" / "session-events"
            events_dir.mkdir(parents=True)
            records = [
                {
                    "session_id": "untrusted-id",
                    "event_name": "PreToolUse",
                    "timestamp": "2026-07-22T10:00:00Z",
                    "payload": {"tool_name": "Read"},
                },
                {
                    "hook_event_name": "PostToolUse",
                    "timestamp": "2026-07-22T10:00:01+00:00",
                    "payload": {"tool_name": "Read", "ok": True},
                },
                {
                    "event_name": "Stop",
                    "timestamp": "not-a-timestamp",
                    "payload": {},
                },
                {
                    "event_name": "Stop",
                    "timestamp": "2026-07-22T10:00:02Z",
                    "payload": ["not", "a", "mapping"],
                },
            ]
            lines = [json.dumps(record) for record in records]
            lines.append("not-json")
            (events_dir / "session-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = ClaudeCodeAdapter(project_root=root).read_session_events("session-1")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].session_id, "session-1")
        self.assertEqual(result[0].event_name, "PreToolUse")
        self.assertEqual(result[0].timestamp, datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(dict(result[0].payload), {"tool_name": "Read"})
        self.assertEqual(result[1].event_name, "PostToolUse")
        self.assertEqual(dict(result[1].payload), {"tool_name": "Read", "ok": True})

    def test_read_session_events_returns_empty_for_missing_or_unsafe_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ClaudeCodeAdapter(project_root=Path(tmp))

            self.assertEqual(adapter.read_session_events("missing"), [])
            self.assertEqual(adapter.read_session_events("../outside"), [])

    def test_hook_event_name_passes_names_through(self):
        adapter = ClaudeCodeAdapter()

        self.assertEqual(adapter.hook_event_name("PreToolUse"), "PreToolUse")
        self.assertEqual(adapter.hook_event_name("custom-event"), "custom-event")

    def test_prompt_user_uses_injected_callback(self):
        questions = []

        def prompt(question: str) -> str:
            questions.append(question)
            return "yes"

        adapter = ClaudeCodeAdapter(prompt_callback=prompt)

        self.assertEqual(adapter.prompt_user("Continue?"), "yes")
        self.assertEqual(questions, ["Continue?"])

    def test_prompt_user_without_callback_fails_without_subprocess(self):
        adapter = ClaudeCodeAdapter()

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

            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(from_env)}, clear=True):
                with mock.patch("lib.runtime_adapters.claude_code.Path.cwd", return_value=cwd):
                    self.assertEqual(ClaudeCodeAdapter(project_root=explicit).workspace_root(), explicit.resolve())
                    self.assertEqual(ClaudeCodeAdapter().workspace_root(), from_env.resolve())

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("lib.runtime_adapters.claude_code.Path.cwd", return_value=cwd):
                    self.assertEqual(ClaudeCodeAdapter().workspace_root(), cwd.resolve())

    def test_install_skill_uses_injected_installer(self):
        calls = []

        def install(skill_name: str, skill_dir: Path) -> None:
            calls.append((skill_name, skill_dir))

        source = Path("/marketplace/dev-kit/skills/review")
        adapter = ClaudeCodeAdapter(skill_installer=install)

        adapter.install_skill("review", source)

        self.assertEqual(calls, [("review", source)])

    def test_install_skill_without_installer_fails_deterministically(self):
        adapter = ClaudeCodeAdapter()

        with self.assertRaisesRegex(RuntimeError, "skill installer"):
            adapter.install_skill("review", Path("skills/review"))


if __name__ == "__main__":
    unittest.main()
