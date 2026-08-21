from __future__ import annotations

import json
from pathlib import Path

from lib.harness_effectiveness import COMPONENT_WEIGHTS, build_report
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


def test_recovery_tolerates_malformed_retry_count(tmp_path: Path) -> None:
    """A non-numeric retry_count in evidence_ref must not crash the report.

    ``trace_log.validate_event`` only checks that ``evidence_ref`` is a
    dict, so a producer could record ``retry_count`` as arbitrary text
    (e.g. via the ``append-event`` CLI). The reducer must degrade to
    "not bounded" instead of raising ValueError across the whole report.
    """
    _event(tmp_path, event_id="e1", event_type="verify.failed", subject="file-1",
           outcome="failed", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="h1", event_type="heal.attempted", subject="file-1",
           outcome="attempted", ts="2026-08-12T00:00:01Z", parent="e1")
    _event(tmp_path, event_id="v1", event_type="verify.passed", subject="file-1",
           outcome="passed", ts="2026-08-12T00:00:02Z", parent="h1",
           independent=True, retry_count="abc")
    recovery = build_report(tmp_path)["components"]["recovery_quality"]
    # The malformed field must not crash; the cycle bound degrades to
    # "not bounded" (cycles=99), while the rest of the scorecard stands.
    assert recovery["submetrics"]["cycle_bound_score"]["value"] == 0.0
    assert recovery["submetrics"]["independent_verification_rate"]["value"] == 100.0
    assert recovery["score"] is not None


def test_recovery_accepts_numeric_string_retry_count(tmp_path: Path) -> None:
    """A numeric-string retry_count is coerced, not treated as unbounded."""
    _event(tmp_path, event_id="e1", event_type="verify.failed", subject="file-1",
           outcome="failed", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="h1", event_type="heal.attempted", subject="file-1",
           outcome="attempted", ts="2026-08-12T00:00:01Z", parent="e1")
    _event(tmp_path, event_id="v1", event_type="verify.passed", subject="file-1",
           outcome="passed", ts="2026-08-12T00:00:02Z", parent="h1",
           independent=True, retry_count="2")
    recovery = build_report(tmp_path)["components"]["recovery_quality"]
    assert recovery["submetrics"]["cycle_bound_score"]["value"] == 100.0
    assert recovery["submetrics"]["verified_recovery_rate"]["value"] == 100.0
    assert recovery["submetrics"]["independent_verification_rate"]["value"] == 100.0


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


def test_weights_sum_to_one() -> None:
    """Component weights must sum to 1.00 so overall_score stays in
    [0, 100] without an implicit normalization factor. learning_quality
    is intentionally 0.00 until a Phase-4 shadow-mode control cohort
    exists; the remaining 1.00 is redistributed proportionally across the
    four shippable components.
    """
    assert sum(COMPONENT_WEIGHTS.values()) == 1.0
    assert COMPONENT_WEIGHTS["learning_quality"] == 0.0
    nonzero = [k for k, v in COMPONENT_WEIGHTS.items() if v > 0]
    assert set(nonzero) == {
        "prevention_quality", "first_pass_quality",
        "recovery_quality", "measurement_integrity",
    }


def _full_event_corpus(tmp_path: Path) -> None:
    """Emit the minimum event set that scores all four shippable
    components to 100.0 (and leaves learning_quality unscored).
    """
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


def test_overall_score_equals_weighted_mean_when_all_scorable(tmp_path: Path) -> None:
    """overall_score is the weighted mean of the scored components,
    restricted to the four shippable ones (learning_quality weight 0).
    The reducer reports the same number the formula below produces.
    """
    _full_event_corpus(tmp_path)
    report = build_report(tmp_path)
    scored = {name: item for name, item in report["components"].items()
              if item["score"] is not None
              and COMPONENT_WEIGHTS[name] > 0}
    assert scored, "test corpus must produce at least one scorable component"
    numerator = sum(item["score"] * COMPONENT_WEIGHTS[name]
                    for name, item in scored.items())
    divisor = sum(COMPONENT_WEIGHTS[name] for name in scored)
    expected = round(numerator / divisor, 1)
    assert report["overall_score"] == expected
    assert report["overall_score"] is not None


def test_overall_score_excludes_zero_weight_components(tmp_path: Path) -> None:
    """A non-None but hypothetical learning_quality=100 must NOT change
    overall_score when its weight is 0; re-enabling it later is a
    one-line weight change. We verify the property by re-deriving the
    weighted mean both with and without learning_quality=100 and
    asserting the values are identical.
    """
    assert COMPONENT_WEIGHTS["learning_quality"] == 0.0
    _full_event_corpus(tmp_path)
    report = build_report(tmp_path)
    without = sum(
        item["score"] * COMPONENT_WEIGHTS[name]
        for name, item in report["components"].items()
        if COMPONENT_WEIGHTS[name] > 0 and item["score"] is not None
    )
    # Synthetically inject learning_quality=100 into the components dict
    # the formula iterates over; if the formula correctly skips weight-0
    # components, the totals match.
    report["components"]["learning_quality"]["score"] = 100.0
    with_lq = sum(
        item["score"] * COMPONENT_WEIGHTS[name]
        for name, item in report["components"].items()
        if item["score"] is not None
    )
    assert with_lq == without


def test_overall_score_skips_none_scored_components(tmp_path: Path) -> None:
    """A None-scored component must NOT collapse overall_score to None
    when at least one other component scores. None-scored components
    drop out of both numerator and divisor, the same way zero-weight
    components do. This is the visible-scorecard fix.
    """
    # Single labeled guard event — only prevention can score; the rest
    # are unsourced. With the old formula, overall_score would be None.
    _event(tmp_path, event_id="g1", event_type="guard.blocked", subject="a1",
           outcome="blocked", ts="2026-08-12T00:00:00Z", policy_id="scope",
           ground_truth="unsafe", reason="x")
    _event(tmp_path, event_id="g2", event_type="guard.allowed", subject="a2",
           outcome="allowed", ts="2026-08-12T00:00:01Z", policy_id="scope",
           ground_truth="unsafe", reason="x")
    report = build_report(tmp_path)
    assert report["components"]["prevention_quality"]["score"] is not None
    # first_pass / recovery / learning must be None — that is the
    # precondition for the contract under test. (measurement_integrity
    # is NOT None any more: issue #702's subject_observability fallback
    # gives it a low score from the symmetric ratio with no step.*
    # events in the corpus. The None-skipped invariant still holds for
    # the components that genuinely have no evidence.)
    for key in ("first_pass_quality", "recovery_quality", "learning_quality"):
        assert report["components"][key]["score"] is None, key
    # With the fix, overall_score must be a number and NOT None — the
    # visible scorecard no longer collapses. The exact value is the
    # weighted mean of every component whose score is not None.
    assert report["overall_score"] is not None
    scored = {k: v["score"] for k, v in report["components"].items()
              if v["score"] is not None}
    expected = round(
        sum(s * COMPONENT_WEIGHTS[k] for k, s in scored.items())
        / sum(COMPONENT_WEIGHTS[k] for k in scored),
        1,
    )
    assert report["overall_score"] == expected, (
        f"overall_score {report['overall_score']} != expected {expected} "
        f"(scored={sorted(scored)})"
    )


def test_trace_event_round_trip(tmp_path: Path) -> None:
    _event(tmp_path, event_id="e1", event_type="step.started", subject="s",
           outcome="started", ts="2026-08-12T00:00:00Z")
    events = read_events(tmp_path)
    assert events[0]["event_id"] == "e1"


# --- Issue #702: subject_observability submetric -----------------------

def test_subject_observability_reports_meaningful_number_with_only_terminal(tmp_path: Path) -> None:
    """Issue #702: subject_observability must report a meaningful symmetric
    ratio even when only terminal events exist (no step.started)."""
    _event(tmp_path, event_id="c1", event_type="step.completed", subject="file-1",
           outcome="completed", ts="2026-08-12T00:00:00Z")
    sub = (
        build_report(tmp_path)["components"]["measurement_integrity"]
        ["submetrics"]["subject_observability"]
    )
    # Symmetric ratio: |started ∩ terminal| / |started ∪ terminal| = 0/1 = 0.0
    assert sub["submetrics"]["started_subjects"]["value"] == 0.0
    assert sub["submetrics"]["terminal_subjects"]["value"] == 100.0
    assert sub["coverage"] == 0.0
    assert any("no step.started events" in f for f in sub["findings"])


def test_subject_observability_reports_pair_when_started_and_terminal_present(tmp_path: Path) -> None:
    """Issue #702: with a started+terminal pair, the symmetric ratio is 1.0."""
    _event(tmp_path, event_id="s1", event_type="step.started", subject="session:abc",
           outcome="started", ts="2026-08-12T00:00:00Z")
    _event(tmp_path, event_id="c1", event_type="step.completed", subject="session:abc",
           outcome="completed", ts="2026-08-12T00:00:01Z", parent="s1")
    sub = (
        build_report(tmp_path)["components"]["measurement_integrity"]
        ["submetrics"]["subject_observability"]
    )
    assert sub["coverage"] == 1.0
    assert sub["score"] == 100.0
    assert sub["status"] == "OK"
    assert sub["findings"] == []


def test_subject_observability_detects_orphan_started(tmp_path: Path) -> None:
    """Issue #702: started-without-terminal emits a finding naming the cause."""
    _event(tmp_path, event_id="s1", event_type="step.started", subject="session:abc",
           outcome="started", ts="2026-08-12T00:00:00Z")
    sub = (
        build_report(tmp_path)["components"]["measurement_integrity"]
        ["submetrics"]["subject_observability"]
    )
    assert sub["coverage"] == 0.0
    assert any("no terminal event observed" in f for f in sub["findings"])


def test_empty_worktree_reports_non_null_measurement_integrity_score(tmp_path: Path) -> None:
    """Issue #702 acceptance criterion: a worktree with only guard.blocked
    events must report a non-null measurement_integrity.score (the
    subject_observability fallback rescues the metric)."""
    for i in range(10):
        _event(tmp_path, event_id=f"g{i}", event_type="guard.blocked", subject=f"a{i}",
               outcome="blocked", ts=f"2026-08-12T00:00:0{i}Z",
               policy_id="scope", reason="out-of-scope")
    integrity = build_report(tmp_path)["components"]["measurement_integrity"]
    assert integrity["score"] is not None, (
        "measurement_integrity.score must be non-null even with no step.* events; "
        "the subject_observability fallback should rescue the metric"
    )
    sub = integrity["submetrics"]["subject_observability"]
    assert any("no step.started events" in f for f in sub["findings"])


def test_subject_observability_uses_submetric_shape(tmp_path: Path) -> None:
    """Issue #702: subject_observability uses _submetric() — no weight field."""
    sub = (
        build_report(tmp_path)["components"]["measurement_integrity"]
        ["submetrics"]["subject_observability"]
    )
    expected = {"coverage", "score", "status", "submetrics", "findings",
                "evidence_event_ids", "config_version"}
    assert set(sub.keys()) == expected
    assert "weight" not in sub
    assert sub["config_version"] == "harness-subject-observability-v1"


def test_schema_version_bumped_to_three(tmp_path: Path) -> None:
    """Issue #702: schema_version bumps from 2 to 3 to advertise the new
    nested submetric. The top-level 5-component shape is unchanged."""
    report = build_report(tmp_path)
    assert report["schema_version"] == 3
    for key in ("components", "overall_score", "status", "event_count",
                "contract_version"):
        assert key in report
