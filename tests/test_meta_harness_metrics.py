"""Formula and hard-gate tests for meta-harness promotion metrics."""

from lib.meta_harness_metrics import build_snapshot, promotion_decision, rate


def test_zero_denominator_is_not_a_perfect_score() -> None:
    assert rate(0, 0) is None
    snapshot = build_snapshot({})
    decision = promotion_decision(snapshot, high_impact=True)
    assert decision["promote"] is False
    assert "insufficient_sample:sir" in decision["failures"]


def test_safe_candidate_passes_all_thresholds() -> None:
    snapshot = build_snapshot(
        {
            "safety_incidents": 0,
            "eligible_high_risk": 10,
            "false_completions": 0,
            "certified_completions": 10,
            "evidence_required": 10,
            "evidence_orphans": 0,
            "resumed_workflows": 100,
            "eligible_resumes": 100,
            "successful_tasks": 9,
            "started_tasks": 10,
            "unnecessary_handoffs": 0,
            "legitimate_tasks": 100,
            "holdout_regressions": 0,
            "kernel_changes": 0,
            "baseline_p95_ms": 1000,
            "candidate_p95_ms": 1040,
            "hook_p95_ms": 40,
            "harness_input_tokens": 50,
            "baseline_input_tokens": 1000,
        }
    )
    assert promotion_decision(snapshot, high_impact=True)["promote"] is True


def test_security_and_thinness_regressions_cannot_be_traded_for_speed() -> None:
    snapshot = build_snapshot(
        {
            "safety_incidents": 1,
            "eligible_high_risk": 10,
            "false_completions": 0,
            "certified_completions": 10,
            "evidence_required": 10,
            "evidence_orphans": 0,
            "resumed_workflows": 100,
            "eligible_resumes": 100,
            "successful_tasks": 10,
            "started_tasks": 10,
            "unnecessary_handoffs": 0,
            "legitimate_tasks": 100,
            "holdout_regressions": 0,
            "kernel_changes": 0,
            "baseline_p95_ms": 1000,
            "candidate_p95_ms": 100,
            "hook_p95_ms": 40,
            "harness_input_tokens": 100,
            "baseline_input_tokens": 1000,
        }
    )
    decision = promotion_decision(snapshot, high_impact=True)
    assert decision["promote"] is False
    assert "safety_incident_rate" in decision["failures"]
    assert "context_overhead" in decision["failures"]
