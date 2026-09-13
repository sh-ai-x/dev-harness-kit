"""Static guards for event-driven LLM judge verdict extraction."""
from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


class TestJudgeVerdictExtractionFailClosed(unittest.TestCase):
    CASES = (
        (PROJECT_ROOT / ".github/workflows/review.yml", "review"),
        (PROJECT_ROOT / ".github/workflows/review.yml", "security"),
        (PROJECT_ROOT / ".github/workflows/maintenance.yml", "maintenance"),
    )

    def test_event_driven_missing_verdict_fails_each_judge(self):
        for workflow, judge in self.CASES:
            with self.subTest(workflow=workflow, judge=judge):
                text = workflow.read_text(encoding="utf-8")
                marker = (
                    f"{judge} verdict missing; refusing to report a successful judge job"
                )
                self.assertIn(marker, text)
                self.assertIn(
                    '&& [ "${{ github.event_name }}" != "workflow_dispatch" ]',
                    text,
                )

    def test_manual_dispatch_override_remains_explicit(self):
        for workflow, _judge in self.CASES:
            with self.subTest(workflow=workflow):
                text = workflow.read_text(encoding="utf-8")
                self.assertIn('!= "workflow_dispatch"', text)
