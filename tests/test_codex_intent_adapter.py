#!/usr/bin/env python3
"""test_codex_intent_adapter.py — regression suite for the Codex
adapter surface (issue #845).

The brief requires "Same fixture prompt run through both Claude and
Codex adapters produces identical classification fields (modulo
client and request_id)." This test pins that parity for the
adapter entry points, in addition to the bare-classifier parity
test in ``tests/test_request_intent.py``.

The Codex adapter is currently the only place where a metric
emission happens on every classification — the
``client_parity_observed`` metric. That emission is what makes the
``client_parity`` reducer slice comparable across clients.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import codex_intent_adapter  # noqa: E402
import intent_metrics  # noqa: E402
import request_intent  # noqa: E402

# Fixture prompts the Codex adapter must classify identically to
# Claude. Each entry is the prompt — clients are passed in.
SHARED_FIXTURES = [
    "explain how the worktree hook works",
    "draft a plan for the new classifier",
    "add file foo to the project",
    "implement the foo module",
    "rename function baz to qux",
    "수정 hook 에러",
    "hello world",
    "",
]


class TestCodexAdapterParity(unittest.TestCase):
    """``classify_codex_request`` must agree with ``classify_request``
    field-for-field modulo ``client``."""

    def test_each_fixture_parity(self):
        for prompt in SHARED_FIXTURES:
            with self.subTest(prompt=prompt):
                claude = request_intent.classify_request(prompt, client="claude-code")
                codex = codex_intent_adapter.classify_codex_request(prompt)
                claude_view = claude.to_dict()
                codex_view = codex.to_dict()
                claude_view["client"] = "<both>"
                codex_view["client"] = "<both>"
                # request_id is deterministic per (client, prompt) so
                # it WILL differ between clients — strip it.
                claude_view.pop("request_id", None)
                codex_view.pop("request_id", None)
                self.assertEqual(
                    claude_view, codex_view,
                    f"Parity drift for prompt={prompt!r}: "
                    f"claude={claude_view} codex={codex_view}",
                )


class TestCodexAdapterEmitsParityMetrics(unittest.TestCase):
    """``classify_codex_request`` must emit one
    ``client_parity_observed`` row per call so the reducer can
    compute parity."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sink = self.tmp / "metrics.jsonl"
        self._env = os.environ.get(intent_metrics._SINK_ENV)
        os.environ[intent_metrics._SINK_ENV] = str(self.sink)

    def tearDown(self):
        if self._env is None:
            os.environ.pop(intent_metrics._SINK_ENV, None)
        else:
            os.environ[intent_metrics._SINK_ENV] = self._env

    def test_one_emission_per_call(self):
        for prompt in SHARED_FIXTURES:
            codex_intent_adapter.classify_codex_request(prompt)
        rows = intent_metrics.read_events(self.sink)
        # Empty-prompt calls emit too (they still classify, just
        # as read_only).
        self.assertEqual(len(rows), len(SHARED_FIXTURES))
        for row in rows:
            self.assertEqual(row.metric, "client_parity_observed")
            self.assertEqual(row.tags.get("client"), "codex")

    def test_emission_tags_carry_classification_mode(self):
        codex_intent_adapter.classify_codex_request("add file foo to the project")
        rows = intent_metrics.read_events(self.sink)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tags.get("mode"), "implementation")
        self.assertTrue(rows[0].tags.get("request_id", "").startswith("req-"))


class TestClassifyRequestAsGenericAdapter(unittest.TestCase):
    """``classify_request_as`` lets callers choose the client string
    while sharing the rest of the adapter surface. Used by the
    Claude hook so both adapters emit the same metric."""

    def test_classify_request_as_matches_direct_call(self):
        prompt = "add file foo to the project"
        direct = request_intent.classify_request(prompt, client="claude-code")
        via_adapter = codex_intent_adapter.classify_request_as(
            prompt, client="claude-code",
        )
        self.assertEqual(direct.to_dict(), via_adapter.to_dict())

    def test_classify_request_as_rejects_bad_client(self):
        """The generic adapter still goes through the fail-closed
        branch in ``classify_request`` for unknown clients —
        returns ``uncertain``, never raises."""
        result = codex_intent_adapter.classify_request_as(
            "add file foo", client="bogus",  # type: ignore[arg-type]
        )
        self.assertEqual(result.mode, "uncertain")
        self.assertFalse(result.worktree_required)


if __name__ == "__main__":
    unittest.main(verbosity=2)
