#!/usr/bin/env python3
"""Focused tests for runtime-neutral hooks and user-input boundaries."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.runtime_adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from lib.runtime_adapters.codex import CodexAdapter  # noqa: E402
from lib.runtime_adapters.hooks import HookNameNormalizer, normalize_hook_name  # noqa: E402
from lib.runtime_adapters.user_input import (  # noqa: E402
    PromptAdapter,
    UserInputAdapter,
    prompt_user,
)


class TestHookNormalization(unittest.TestCase):
    def test_normalize_hook_name_matches_both_runtime_contracts(self):
        self.assertEqual(normalize_hook_name("claude-code", "PreToolUse"), "PreToolUse")
        self.assertEqual(normalize_hook_name("codex", "PreToolUse"), "before_tool_use")
        self.assertEqual(normalize_hook_name("codex", "PostToolUse"), "after_tool_use")

    def test_normalize_hook_name_passes_unknown_names_through(self):
        self.assertEqual(normalize_hook_name("claude-code", "custom-event"), "custom-event")
        self.assertEqual(normalize_hook_name("codex", "custom-event"), "custom-event")

    def test_normalize_hook_name_rejects_unknown_runtime_deterministically(self):
        with self.assertRaisesRegex(ValueError, "unsupported runtime"):
            normalize_hook_name("unknown", "PreToolUse")

    def test_hook_name_normalizer_and_runtime_adapters_use_same_mapping(self):
        normalizer = HookNameNormalizer("codex")

        self.assertEqual(normalizer.normalize("PreToolUse"), "before_tool_use")
        self.assertEqual(ClaudeCodeAdapter().hook_event_name("PreToolUse"), "PreToolUse")
        self.assertEqual(CodexAdapter().hook_event_name("PreToolUse"), "before_tool_use")


class TestUserInputBoundary(unittest.TestCase):
    def test_prompt_user_uses_an_injected_callback(self):
        questions = []

        def callback(question: str) -> str:
            questions.append(question)
            return "yes"

        self.assertEqual(prompt_user("Continue?", callback), "yes")
        self.assertEqual(questions, ["Continue?"])

    def test_prompt_user_accepts_an_injected_prompt_adapter(self):
        class Boundary:
            def prompt_user(self, question: str) -> str:
                return f"answer:{question}"

        self.assertEqual(prompt_user("Continue?", Boundary()), "answer:Continue?")

    def test_user_input_adapter_implements_prompt_protocol(self):
        adapter = UserInputAdapter(lambda question: f"answer:{question}")

        self.assertIsInstance(adapter, PromptAdapter)
        self.assertEqual(adapter.prompt_user("Continue?"), "answer:Continue?")

    def test_missing_prompt_boundary_fails_without_tool_invocation(self):
        with self.assertRaisesRegex(RuntimeError, "prompt callback"):
            UserInputAdapter().prompt_user("Continue?")

    def test_invalid_prompt_boundary_fails_deterministically(self):
        with self.assertRaisesRegex(TypeError, "callable"):
            prompt_user("Continue?", object())


if __name__ == "__main__":
    unittest.main()
