"""Replay/holdout promotion contract tests."""

from __future__ import annotations

from lib.meta_eval import CaseMetaResult, MetaEvalReport, evaluate_candidate


def _report(*statuses: tuple[str, str]) -> MetaEvalReport:
    cases = tuple(
        CaseMetaResult(case_id=case_id, worktree="fixture", status=status)
        for case_id, status in statuses
    )
    return MetaEvalReport(
        cases=cases,
        total=len(cases),
        passed=sum(status == "passed" for _, status in statuses),
        failed=sum(status == "failed" for _, status in statuses),
        skipped=sum(status == "skipped" for _, status in statuses),
        errored=sum(status == "error" for _, status in statuses),
    )


def test_replay_and_holdout_can_promote_candidate() -> None:
    baseline = _report(("replay-1", "failed"), ("holdout-1", "passed"))
    candidate = _report(("replay-1", "passed"), ("holdout-1", "passed"))
    gate = evaluate_candidate(
        baseline,
        candidate,
        candidate_id="candidate-v1",
        replay_case_ids=["replay-1"],
        holdout_case_ids=["holdout-1"],
    )
    assert gate.promote is True
    assert gate.to_dict()["candidate_id"] == "candidate-v1"


def test_holdout_regression_rejects_candidate() -> None:
    baseline = _report(("holdout-1", "passed"))
    candidate = _report(("holdout-1", "failed"))
    gate = evaluate_candidate(
        baseline,
        candidate,
        candidate_id="candidate-bad",
        replay_case_ids=[],
        holdout_case_ids=["holdout-1"],
    )
    assert gate.promote is False
    assert gate.holdout_regressions == ("holdout-1",)


def test_replay_holdout_overlap_is_a_hard_failure() -> None:
    report = _report(("case-1", "passed"))
    gate = evaluate_candidate(
        report,
        report,
        candidate_id="candidate-overlap",
        replay_case_ids=["case-1"],
        holdout_case_ids=["case-1"],
    )
    assert gate.promote is False
    assert "replay_holdout_overlap:case-1" in gate.hard_gate_failures
