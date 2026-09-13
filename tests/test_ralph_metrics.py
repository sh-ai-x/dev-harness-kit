"""Contract tests for Ralph evidence reduction."""
from __future__ import annotations

from pathlib import Path

from lib.trace_log import read_events
from skills.ralph.lib.ralph_events import RalphEventAdapter
from skills.ralph.lib.ralph_metrics import (
    STATUS_DEGRADED,
    STATUS_FAILED,
    reduce_metrics,
)


def _events(tmp_path: Path) -> list[dict[str, object]]:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    adapter.emit("BUILD", "stage.started", "started", subject_id="build")
    adapter.emit("BUILD", "stage.completed", "completed", subject_id="build")
    adapter.emit("TEST", "stage.started", "started", subject_id="test")
    adapter.emit("TEST", "stage.failed", "failed", subject_id="test")
    return read_events(tmp_path)


def test_report_has_evidence_linked_metric_shape(tmp_path: Path) -> None:
    report = reduce_metrics(_events(tmp_path))
    metric = report["metrics"]["stage_completion_rate"]

    assert {"numerator", "denominator", "coverage", "evidence_event_ids", "status"} <= set(metric)
    assert metric["numerator"] == 2
    assert metric["denominator"] == 2
    assert metric["coverage"] == 1.0
    assert len(metric["evidence_event_ids"]) == 4
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert "context_metrics" in report
    assert report["context_metrics"]["usage_coverage"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_missing_terminal_is_not_zero_or_success(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    adapter.emit("BUILD", "stage.started", "started", subject_id="build")

    report = reduce_metrics(read_events(tmp_path))
    metric = report["metrics"]["stage_completion_rate"]

    assert metric["numerator"] == 0
    assert metric["denominator"] == 1
    assert metric["value"] is None
    assert metric["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["score"] is None


def test_explicit_zero_is_a_measured_failure(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    adapter.emit("BUILD", "stage.started", "started", subject_id="build")
    adapter.emit("BUILD", "stage.failed", "failed", subject_id="build")

    report = reduce_metrics(read_events(tmp_path))
    metric = report["metrics"]["stage_success_rate"]

    assert metric["denominator"] == 1
    assert metric["numerator"] == 0
    assert metric["value"] == 0.0
    assert metric["status"] == STATUS_FAILED


def test_recovery_requires_recovery_terminal_evidence(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    adapter.emit("BUILD", "stage.failed", "failed", subject_id="build")

    report = reduce_metrics(read_events(tmp_path))
    metric = report["metrics"]["recovery_success_rate"]
    assert metric["value"] is None
    assert metric["status"] == "INSUFFICIENT_EVIDENCE"


def test_degraded_event_does_not_report_ok(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    adapter.emit("BUILD", "stage.started", "started", subject_id="build")
    adapter.emit(
        "BUILD",
        "stage.completed",
        "completed",
        subject_id="build",
        evidence_ref={"observability_status": "DEGRADED"},
    )

    report = reduce_metrics(read_events(tmp_path))
    assert report["status"] == STATUS_DEGRADED
    assert report["metrics"]["stage_completion_rate"]["status"] == STATUS_DEGRADED


def test_duplicate_event_id_is_not_counted_twice(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    started = adapter.emit("BUILD", "stage.started", "started", subject_id="build")
    completed = adapter.emit("BUILD", "stage.completed", "completed", subject_id="build")
    events = [dict(started.record), dict(started.record), dict(completed.record)]

    report = reduce_metrics(events)
    metric = report["metrics"]["stage_completion_rate"]
    assert metric["numerator"] == 1
    assert metric["denominator"] == 1
    assert report["event_count"] == 2


def test_expected_stage_without_evidence_remains_incomplete(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    adapter.emit("BUILD", "stage.started", "started", subject_id="build")
    adapter.emit("BUILD", "stage.completed", "completed", subject_id="build")

    report = reduce_metrics(
        read_events(tmp_path),
        expected_stage_ids=["BUILD", "TEST"],
    )
    metric = report["metrics"]["stage_completion_rate"]
    assert metric["value"] is None
    assert metric["status"] == "INSUFFICIENT_EVIDENCE"
