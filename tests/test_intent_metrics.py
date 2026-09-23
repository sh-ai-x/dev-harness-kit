#!/usr/bin/env python3
"""test_intent_metrics.py — regression suite for lib/intent_metrics.py.

The seven metrics the issue brief enumerates:

  * time_to_intent
  * intent_correction_rate
  * false_cut_rate
  * missed_cut_rate
  * client_parity
  * cut_failure_rate
  * intent_survival_rate

Each one must (a) have a definition in ``METRIC_DEFINITIONS`` and
(b) be emittable via ``emit``. The sink defaults to
``.dev-kit/cache/intent-metrics.jsonl`` but tests point at a tmp
file via the ``INTENT_METRICS_SINK`` env-var.

Pattern: same flat ``sys.path.insert(0, lib/)`` shape as
``tests/test_request_intent.py``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import intent_metrics  # noqa: E402


class TestMetricDefinitions(unittest.TestCase):
    """Every metric named in the issue has a matching definition."""

    BRIEF_METRICS = (
        "time_to_intent",
        "intent_correction_rate",
        "false_cut_rate",
        "missed_cut_rate",
        "client_parity",
        "cut_failure_rate",
        "intent_survival_rate",
    )

    def test_every_brief_metric_is_documented(self):
        for name in self.BRIEF_METRICS:
            with self.subTest(metric=name):
                self.assertIn(name, intent_metrics.METRIC_DEFINITIONS)

    def test_every_definition_has_unit_and_description(self):
        for name, defn in intent_metrics.METRIC_DEFINITIONS.items():
            with self.subTest(metric=name):
                self.assertIn("unit", defn)
                self.assertIn("description", defn)
                self.assertTrue(defn["description"])


class TestEmit(unittest.TestCase):
    """``emit`` writes a JSONL row with the documented fields."""

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

    def test_emit_writes_one_row_per_call(self):
        intent_metrics.emit("time_to_intent", 12.5, unit="seconds")
        intent_metrics.emit("false_cut_rate", 0.05)
        self.assertTrue(self.sink.exists())
        rows = intent_metrics.read_events(self.sink)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].metric, "time_to_intent")
        self.assertEqual(rows[0].unit, "seconds")
        self.assertEqual(rows[1].metric, "false_cut_rate")
        self.assertEqual(rows[1].unit, "ratio")

    def test_emit_includes_tags(self):
        intent_metrics.emit(
            "client_parity",
            1.0,
            tags={"client": "claude-code", "mode": "implementation"},
        )
        rows = intent_metrics.read_events(self.sink)
        self.assertEqual(rows[0].tags, {
            "client": "claude-code",
            "mode": "implementation",
        })

    def test_emit_disabled_returns_none(self):
        os.environ[intent_metrics._SINK_ENV] = ""
        result = intent_metrics.emit("cut_failure_rate", 0.01)
        self.assertIsNone(result)
        # Sink must NOT have been written when disabled.
        self.assertFalse(self.sink.exists())

    def test_emit_swallows_os_errors(self):
        # Point the sink at a path the parent dir can't be created
        # at (read-only system path) — must NOT raise.
        os.environ[intent_metrics._SINK_ENV] = "/dev/null/impossible/metrics.jsonl"
        # On macOS /dev/null IS a directory? No, it's a char dev.
        # But writing under it fails — verify emit returns None.
        result = intent_metrics.emit("false_cut_rate", 0.0)
        self.assertIsNone(result)


class TestReadEvents(unittest.TestCase):
    """``read_events`` parses a JSONL sink into MetricEvent rows."""

    def test_read_handles_missing_file(self):
        self.assertEqual(intent_metrics.read_events(Path("/nonexistent")), [])

    def test_read_skips_blank_and_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = Path(tmp) / "metrics.jsonl"
            sink.write_text(
                "\n"
                '{"metric":"x","value":1.0,"unit":"ratio","ts":1.0,"tags":{}}\n'
                "garbage\n"
                '{"metric":"y","value":2.0,"unit":"seconds","ts":2.0,"tags":{}}\n',
                encoding="utf-8",
            )
            rows = intent_metrics.read_events(sink)
            self.assertEqual([r.metric for r in rows], ["x", "y"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
