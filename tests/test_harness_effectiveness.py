from __future__ import annotations

import json
from pathlib import Path

from lib.harness_effectiveness import build_report
from lib.trace_log import append_event, read_events


def _event(root: Path, *, event_id: str, event_type: str, subject: str,
           outcome: str, ts: str, parent: str | None = None, **evidence) -> None:
    append_event(root, {
        "event_id": event_id,
        "run_id": "run-1",
        "workflow_id": "wf-1",
        "stage": "build",
        "event_type": event_type,
        "subject_id": subject,
        "parent_id": parent,
        "ts": ts,
        "outcome": outcome,
        "source": "test",
        "evidence_ref": evidence,
    })


def test_first_pass_and_recovery_require_ordered_evidence(tmp_path: Path) -> None:
    _event(tmp_path, event_id="w1", event_type="write.observed", subject="file-1",
           outcome="observed", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="v1", event_type="verify.passed", subject="file-1",
           outcome="passed", ts="2026-08-12T00:00:01Z", parent="w1",
           required_checks_passed=True, retry_count=0)
    _event(tmp_path, event_id="e1", event_type="verify.failed", subject="file-2",
           outcome="failed", ts="2026-08-12T00:00:02Z", error_signature="sig-1")
    _event(tmp_path, event_id="h1", event_type="heal.attempted", subject="file-2",
           outcome="attempted", ts="2026-08-12T00:00:03Z", parent="e1")
    _event(tmp_path, event_id="v2", event_type="verify.passed", subject="file-2",
           outcome="passed", ts="2026-08-12T00:00:04Z", parent="h1",
           required_checks_passed=True, independent=True, retry_count=1)

    report = build_report(tmp_path)
    assert report["components"]["first_pass_quality"]["score"] == 100.0
    assert report["components"]["recovery_quality"]["score"] == 100.0
    assert report["components"]["measurement_integrity"]["status"] == "OK"


def test_missing_verifier_does_not_count_as_verified_recovery(tmp_path: Path) -> None:
    _event(tmp_path, event_id="e1", event_type="verify.failed", subject="file-1",
           outcome="failed", ts="2026-08-12T00:00:00Z", error_signature="sig-1")
    _event(tmp_path, event_id="h1", event_type="heal.attempted", subject="file-1",
           outcome="attempted", ts="2026-08-12T00:00:01Z", parent="e1")
    _event(tmp_path, event_id="v1", event_type="verify.passed", subject="file-1",
           outcome="passed", ts="2026-08-12T00:00:02Z", parent="h1",
           required_checks_passed=True, independent=False, retry_count=1)

    recovery = build_report(tmp_path)["components"]["recovery_quality"]
    assert recovery["status"] == "ROT"
    assert recovery["submetrics"]["verified_recovery_rate"]["value"] == 0.0


def test_prevention_requires_ground_truth_label(tmp_path: Path) -> None:
    _event(tmp_path, event_id="g1", event_type="guard.blocked", subject="a1",
           outcome="blocked", ts="2026-08-12T00:00:00Z", policy_id="scope",
           ground_truth="unsafe", reason="out-of-scope")
    _event(tmp_path, event_id="g2", event_type="guard.blocked", subject="a2",
           outcome="blocked", ts="2026-08-12T00:00:01Z", policy_id="scope",
           reason="out-of-scope")
    prevention = build_report(tmp_path)["components"]["prevention_quality"]
    assert prevention["score"] is None
    assert prevention["status"] == "INSUFFICIENT_EVIDENCE"


def test_duplicate_event_is_integrity_finding(tmp_path: Path) -> None:
    path = tmp_path / ".dev-kit" / "trace" / "events.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "schema_version": 1, "event_id": "same", "run_id": "r",
        "workflow_id": "w", "stage": "build", "event_type": "step.started",
        "subject_id": "s", "parent_id": None, "ts": "2026-08-12T00:00:00Z",
        "outcome": "started", "source": "test", "evidence_ref": {},
    }
    path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n")
    integrity = build_report(tmp_path)["components"]["measurement_integrity"]
    assert integrity["status"] == "INSUFFICIENT_EVIDENCE"
    assert any("duplicate" in finding for finding in integrity["findings"])


def test_trace_event_round_trip(tmp_path: Path) -> None:
    _event(tmp_path, event_id="e1", event_type="step.started", subject="s",
           outcome="started", ts="2026-08-12T00:00:00Z")
    events = read_events(tmp_path)
    assert events[0]["event_id"] == "e1"
