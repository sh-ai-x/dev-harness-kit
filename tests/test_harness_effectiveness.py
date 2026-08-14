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
    _event(tmp_path, event_id="s1", event_type="step.started", subject="file-1",
           outcome="started", ts="2026-08-11T23:59:59Z")
    _event(tmp_path, event_id="c1", event_type="step.completed", subject="file-1",
           outcome="completed", ts="2026-08-12T00:00:05Z", parent="s1")
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


def test_recovery_verified_and_independent_are_distinct(tmp_path: Path) -> None:
    _event(tmp_path, event_id="e1", event_type="verify.failed", subject="file-1",
           outcome="failed", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="h1", event_type="heal.attempted", subject="file-1",
           outcome="attempted", ts="2026-08-12T00:00:01Z", parent="e1")
    _event(tmp_path, event_id="v1", event_type="verify.passed", subject="file-1",
           outcome="passed", ts="2026-08-12T00:00:02Z", parent="h1",
           independent=False, retry_count=1)
    recovery = build_report(tmp_path)["components"]["recovery_quality"]
    assert recovery["submetrics"]["verified_recovery_rate"]["value"] == 100.0
    assert recovery["submetrics"]["independent_verification_rate"]["value"] == 0.0


def test_integrity_coverage_requires_terminal_lifecycle_event(tmp_path: Path) -> None:
    _event(tmp_path, event_id="s1", event_type="step.started", subject="file-1",
           outcome="started", ts="2026-08-12T00:00:00Z")
    integrity = build_report(tmp_path)["components"]["measurement_integrity"]
    assert integrity["status"] == "INSUFFICIENT_EVIDENCE"
    assert integrity["submetrics"]["event_coverage"]["value"] == 0.0


def test_integrity_partial_coverage_is_insufficient(tmp_path: Path) -> None:
    _event(tmp_path, event_id="s1", event_type="step.started", subject="file-1",
           outcome="started", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="c1", event_type="step.completed", subject="file-1",
           outcome="completed", ts="2026-08-12T00:00:01Z", parent="s1")
    _event(tmp_path, event_id="s2", event_type="step.started", subject="file-2",
           outcome="started", ts="2026-08-12T00:00:02Z")
    integrity = build_report(tmp_path)["components"]["measurement_integrity"]
    assert integrity["status"] == "INSUFFICIENT_EVIDENCE"
    assert integrity["coverage"] == 0.5


def test_schema_drift_is_counted_not_raised(tmp_path: Path) -> None:
    path = tmp_path / ".dev-kit" / "trace" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 2}) + "\n")
    integrity = build_report(tmp_path)["components"]["measurement_integrity"]
    assert any("malformed" in finding for finding in integrity["findings"])


def test_missing_verifier_does_not_count_as_verified_recovery(tmp_path: Path) -> None:
    _event(tmp_path, event_id="e1", event_type="verify.failed", subject="file-1",
           outcome="failed", ts="2026-08-12T00:00:00Z", error_signature="sig-1")
    _event(tmp_path, event_id="h1", event_type="heal.attempted", subject="file-1",
           outcome="attempted", ts="2026-08-12T00:00:01Z", parent="e1")
    _event(tmp_path, event_id="v1", event_type="verify.passed", subject="file-1",
           outcome="passed", ts="2026-08-12T00:00:02Z", parent="h1",
           required_checks_passed=True, independent=False, retry_count=1)

    recovery = build_report(tmp_path)["components"]["recovery_quality"]
    assert recovery["status"] == "DRIFT_WARNING"
    assert recovery["submetrics"]["verified_recovery_rate"]["value"] == 100.0


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


def test_weights_sum_to_one(tmp_path: Path) -> None:
    """Component weights must sum to 1.00 so overall_score stays in
    [0, 100] without an implicit normalization factor. learning_quality
    is intentionally 0.00 until a Phase-4 shadow-mode control cohort
    exists; the remaining 1.00 is redistributed proportionally across the
    four shippable components.
    """
    from lib.harness_effectiveness import COMPONENT_WEIGHTS
    assert sum(COMPONENT_WEIGHTS.values()) == 1.0
    assert COMPONENT_WEIGHTS["learning_quality"] == 0.0
    nonzero = [k for k, v in COMPONENT_WEIGHTS.items() if v > 0]
    assert set(nonzero) == {
        "prevention_quality", "first_pass_quality",
        "recovery_quality", "measurement_integrity",
    }


def test_overall_score_excludes_zero_weight_components(tmp_path: Path) -> None:
    """learning_quality must contribute 0 to overall_score when its
    weight is 0; re-enabling it later is a one-line weight change.
    """
    from lib.harness_effectiveness import COMPONENT_WEIGHTS, build_report
    zero_keys = [k for k, v in COMPONENT_WEIGHTS.items() if v == 0.0]
    assert zero_keys == ["learning_quality"]
    _event(tmp_path, event_id="g1", event_type="guard.blocked", subject="a1",
           outcome="blocked", ts="2026-08-12T00:00:00Z", policy_id="scope",
           ground_truth="unsafe", reason="x")
    _event(tmp_path, event_id="g2", event_type="guard.allowed", subject="a2",
           outcome="allowed", ts="2026-08-12T00:00:01Z", policy_id="scope",
           ground_truth="unsafe", reason="x")
    _event(tmp_path, event_id="w1", event_type="write.observed", subject="d1",
           outcome="written", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="vv", event_type="verify.passed", subject="d1",
           outcome="passed", parent="w1", ts="2026-08-12T00:00:01Z",
           required_checks_passed=True, retry_count=0)
    _event(tmp_path, event_id="vf", event_type="verify.failed", subject="d1",
           outcome="failed", ts="2026-08-12T00:00:02Z")
    _event(tmp_path, event_id="hh", event_type="heal.attempted", subject="d1",
           outcome="attempted", parent="vf", ts="2026-08-12T00:00:03Z")
    _event(tmp_path, event_id="vp2", event_type="verify.passed", subject="d1",
           outcome="passed", parent="hh", ts="2026-08-12T00:00:04Z",
           required_checks_passed=True, independent=True, retry_count=1)
    _event(tmp_path, event_id="s1", event_type="step.started", subject="e1",
           outcome="started", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="sc", event_type="step.completed", subject="e1",
           outcome="completed", ts="2026-08-12T00:00:01Z")

    report = build_report(tmp_path)
    for key in ("prevention_quality", "first_pass_quality",
                "recovery_quality", "measurement_integrity"):
        assert report["components"][key]["score"] is not None, key


def test_trace_event_round_trip(tmp_path: Path) -> None:
    _event(tmp_path, event_id="e1", event_type="step.started", subject="s",
           outcome="started", ts="2026-08-12T00:00:00Z")
    events = read_events(tmp_path)
    assert events[0]["event_id"] == "e1"
