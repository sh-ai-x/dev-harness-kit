"""Contract tests for the Ralph evidence adapter."""
from __future__ import annotations

from pathlib import Path

import pytest

from lib.context_budget import MAX_EVENT_BYTES, MAX_EXCERPT_CHARS
from lib.trace_log import read_events
from skills.ralph.lib.ralph_events import (
    EVENT_STATUS_DEGRADED,
    EVENT_STATUS_DUPLICATE,
    RalphEventAdapter,
)


def test_event_contains_ralph_identity_and_trace_contract(tmp_path: Path) -> None:
    emission = RalphEventAdapter(
        tmp_path, run_id="run-1", attempt_id="attempt-1"
    ).emit("BUILD", "stage.started", "started", subject_id="build")

    assert emission.evidence_available
    event = read_events(tmp_path)[0]
    assert event["run_id"] == "run-1"
    assert event["attempt_id"] == "attempt-1"
    assert event["stage_id"] == "BUILD"
    assert event["stage"] == "BUILD"
    assert event["event_id"] == emission.event_id


def test_stdout_stderr_are_redacted_and_bounded(tmp_path: Path) -> None:
    secret = "Bearer super-secret-token"
    emission = RalphEventAdapter(
        tmp_path, run_id="run-1", attempt_id="attempt-1"
    ).emit(
        "BUILD",
        "stage.failed",
        "failed",
        stdout=secret + " x" * MAX_EXCERPT_CHARS,
        stderr="api_key=do-not-log " + "e" * MAX_EXCERPT_CHARS,
    )

    evidence = emission.record["evidence_ref"]
    assert "super-secret-token" not in evidence["stdout"]
    assert "do-not-log" not in evidence["stderr"]
    assert len(evidence["stdout"]) <= MAX_EXCERPT_CHARS
    assert len(evidence["stderr"]) <= MAX_EXCERPT_CHARS
    assert evidence["stdout_truncated"] is True
    assert evidence["stderr_truncated"] is True
    assert len(str(emission.record).encode()) < MAX_EVENT_BYTES * 2


def test_event_evidence_drops_transcript_shaped_fields(tmp_path: Path) -> None:
    result = RalphEventAdapter(
        tmp_path, run_id="run-transcript", attempt_id="attempt-1"
    ).emit(
        "BUILD",
        "stage.completed",
        "completed",
        evidence_ref={"messages": ["full transcript"], "artifact": "build.json"},
    )
    assert result.persisted
    evidence = result.record["evidence_ref"]
    assert "messages" not in evidence
    assert evidence["artifact"] == "build.json"


def test_same_idempotency_key_is_one_event_and_returns_duplicate(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    first = adapter.emit(
        "BUILD", "stage.completed", "completed", idempotency_key="build-done"
    )
    second = adapter.emit(
        "BUILD", "stage.completed", "completed", idempotency_key="build-done"
    )

    assert first.event_id == second.event_id
    assert second.duplicate is True
    assert second.status == EVENT_STATUS_DUPLICATE
    assert len(read_events(tmp_path)) == 1


def test_storage_failure_is_degraded_and_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("trace unavailable")

    monkeypatch.setattr("skills.ralph.lib.ralph_events.append_event", fail)
    emission = adapter.emit("BUILD", "stage.failed", "failed")

    assert emission.degraded is True
    assert emission.persisted is False
    assert emission.status == EVENT_STATUS_DEGRADED
    assert emission.evidence_available is False
    assert "trace unavailable" in (emission.error or "")


def test_handoff_uses_shared_context_contract(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run-1", attempt_id="attempt-1")
    handoff = {
        "schema_version": 1,
        "intent_ref": "intent-1",
        "checkpoint": "BUILD",
        "failure_signature": "",
        "artifact_refs": ["artifact-1"],
        "summary": "compact summary",
        "context_mode": "summary_ref",
        "context_fingerprint": "fingerprint",
        "context_ref_count": 1,
        "replay_tokens": 0,
        "compaction_observed": False,
    }
    emission = adapter.emit(
        "RECOVERY", "recovery.completed", "completed", handoff=handoff
    )

    assert emission.evidence_available
    assert emission.record["evidence_ref"]["handoff"]["intent_ref"] == "intent-1"


def test_missing_attempt_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="attempt_id"):
        RalphEventAdapter("/tmp/ralph-events", run_id="run-1").emit(
            "BUILD", "stage.started", "started"
        )
