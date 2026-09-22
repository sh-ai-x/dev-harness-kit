"""meta_eval.py — evaluate the evaluator (evidence-based verification loop).

This module closes the gap identified in proposal §05-meta-eval: an
"evidence-based" eval harness must itself be testable. We do that by
loading golden case fixtures from `eval/cases/agent-behavior/`, running
the eval pipeline against each, and comparing the resulting verdict
+ per-dim scores against the case's `expected` metadata.

The output is a `MetaEvalReport` (passed / failed / skipped per case),
not a `BehaviorReport`. This is a meta-layer: the case fixtures
declare what a *correct* eval verdict looks like, and we verify that
the eval system produces that verdict. If the eval system drifts
(judge prompts change, heuristics regress, weights get rebalanced),
this loop catches it.

Per proposal §05 §"Audit 매트릭스":
- D1-D4 evidence-based: yes (deterministic)
- D5-D7 evidence-based: partial (Phase 0 stubs)
- **meta-eval layer**: previously MISSING, added by this module.

Public API:
    run_meta_eval(cases_dir, worktree_root, ctx=None) -> MetaEvalReport
    MetaEvalReport.cases            tuple of CaseMetaResult
    MetaEvalReport.summary          aggregate pass/fail counts
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lib.behavior_scorers import (
    BehaviorReport,
    Context,
    score_all,
)


@dataclass(frozen=True)
class CaseExpected:
    """The verdict metadata a golden case declares as correct."""

    verdict: str
    deterministic_mean_min: float = 0.0
    weighted_mean_min: float = 0.0
    weighted_mean_max: float = 5.0
    per_dim_min: Dict[str, int] = field(default_factory=dict)
    per_dim_max: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CaseExpected":
        return cls(
            verdict=str(raw.get("verdict", "OK")),
            deterministic_mean_min=float(raw.get("deterministic_mean_min", 0.0)),
            weighted_mean_min=float(raw.get("weighted_mean_min", 0.0)),
            weighted_mean_max=float(raw.get("weighted_mean_max", 5.0)),
            per_dim_min=dict(raw.get("per_dim_min", {})),
            per_dim_max=dict(raw.get("per_dim_max", {})),
        )


@dataclass(frozen=True)
class CaseSpec:
    """A golden case fixture loaded from disk."""

    case_id: str
    dim: str
    description: str
    worktree_path: Path
    expected: CaseExpected
    schema_version: int

    @classmethod
    def load(cls, path: Path) -> "CaseSpec":
        """Read a case JSON file. Path resolution is done by the caller."""
        raw = json.loads(path.read_text())
        wt_raw = raw.get("worktree_path") or raw.get("input", {}).get("worktree_path")
        if not wt_raw:
            raise ValueError(
                f"case {path} missing worktree_path or input.worktree_path"
            )
        wt = Path(wt_raw)
        if wt.is_absolute():
            wt_resolved = wt
        else:
            # Resolve relative to the case file's directory.
            wt_resolved = (path.parent / wt).resolve()
        return cls(
            case_id=str(raw["case_id"]),
            dim=str(raw["dim"]),
            description=str(raw.get("description", "")),
            worktree_path=wt_resolved,
            expected=CaseExpected.from_dict(raw.get("expected", {})),
            schema_version=int(raw.get("schema_version", 1)),
        )


@dataclass(frozen=True)
class CaseFailure:
    """A single reason a case failed its expected metadata."""

    rule: str  # e.g. "verdict_mismatch", "D1_below_min"
    expected: Any
    actual: Any


@dataclass(frozen=True)
class CaseMetaResult:
    """Outcome of running the eval against one golden case."""

    case_id: str
    worktree: str
    status: str  # "passed" | "failed" | "skipped" | "error"
    actual_verdict: str = ""
    actual_weighted_mean: float = 0.0
    actual_deterministic_mean: float = 0.0
    actual_per_dim: Dict[str, int] = field(default_factory=dict)
    failures: Tuple[CaseFailure, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "worktree": self.worktree,
            "status": self.status,
            "actual_verdict": self.actual_verdict,
            "actual_weighted_mean": self.actual_weighted_mean,
            "actual_deterministic_mean": self.actual_deterministic_mean,
            "actual_per_dim": dict(self.actual_per_dim),
            "failures": [
                {"rule": f.rule, "expected": f.expected, "actual": f.actual}
                for f in self.failures
            ],
        }


@dataclass(frozen=True)
class MetaEvalReport:
    """Aggregate meta-eval result across all golden cases."""

    cases: Tuple[CaseMetaResult, ...]
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errored: int = 0

    @property
    def all_passed(self) -> bool:
        """True when every loaded case passed (skipped cases do not count)."""
        return self.failed == 0 and self.errored == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errored": self.errored,
            "all_passed": self.all_passed,
            "cases": [c.to_dict() for c in self.cases],
        }


@dataclass(frozen=True)
class CandidateGate:
    """Deterministic replay/holdout decision for one harness candidate."""

    candidate_id: str
    replay_case_ids: Tuple[str, ...]
    holdout_case_ids: Tuple[str, ...]
    replay_failures: Tuple[str, ...] = ()
    holdout_failures: Tuple[str, ...] = ()
    holdout_regressions: Tuple[str, ...] = ()
    hard_gate_failures: Tuple[str, ...] = ()
    promote: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "replay_case_ids": list(self.replay_case_ids),
            "holdout_case_ids": list(self.holdout_case_ids),
            "replay_failures": list(self.replay_failures),
            "holdout_failures": list(self.holdout_failures),
            "holdout_regressions": list(self.holdout_regressions),
            "hard_gate_failures": list(self.hard_gate_failures),
            "promote": self.promote,
        }


def evaluate_candidate(
    baseline: MetaEvalReport,
    candidate: MetaEvalReport,
    *,
    candidate_id: str,
    replay_case_ids: Iterable[str],
    holdout_case_ids: Iterable[str],
) -> CandidateGate:
    """Compare a candidate report without invoking a model or mutating state.

    Replay cases must pass under the candidate. Protected holdout cases must
    also pass, and a case that passed in baseline but fails in the candidate is
    recorded separately as a regression. Replay and holdout sets must be
    disjoint so a candidate cannot approve itself with the same fixture.
    """
    replay_ids = tuple(sorted(set(replay_case_ids)))
    holdout_ids = tuple(sorted(set(holdout_case_ids)))
    overlap = sorted(set(replay_ids) & set(holdout_ids))
    baseline_by_id = {case.case_id: case for case in baseline.cases}
    candidate_by_id = {case.case_id: case for case in candidate.cases}

    replay_failures = tuple(
        case_id
        for case_id in replay_ids
        if candidate_by_id.get(case_id) is None
        or candidate_by_id[case_id].status != "passed"
    )
    holdout_failures = tuple(
        case_id
        for case_id in holdout_ids
        if candidate_by_id.get(case_id) is None
        or candidate_by_id[case_id].status != "passed"
    )
    holdout_regressions = tuple(
        case_id
        for case_id in holdout_ids
        if baseline_by_id.get(case_id) is not None
        and baseline_by_id[case_id].status == "passed"
        and (
            candidate_by_id.get(case_id) is None
            or candidate_by_id[case_id].status != "passed"
        )
    )
    hard_gate_failures = tuple(
        [f"replay_holdout_overlap:{case_id}" for case_id in overlap]
        + [f"candidate_error:{case.case_id}" for case in candidate.cases if case.status == "error"]
    )
    promote = not (
        replay_failures
        or holdout_failures
        or holdout_regressions
        or hard_gate_failures
    )
    return CandidateGate(
        candidate_id=candidate_id,
        replay_case_ids=replay_ids,
        holdout_case_ids=holdout_ids,
        replay_failures=replay_failures,
        holdout_failures=holdout_failures,
        holdout_regressions=holdout_regressions,
        hard_gate_failures=hard_gate_failures,
        promote=promote,
    )


def _check_case(
    spec: CaseSpec,
    report: BehaviorReport,
) -> Tuple[CaseMetaResult, ...]:
    """Compare an actual BehaviorReport against the case's expected metadata.

    Returns a tuple of CaseFailure objects (empty = passed).
    """
    actual_dim = {s.dim: s.value for s in report.dimension_scores}
    failures: List[CaseFailure] = []

    # 1. Verdict match (exact).
    if report.verdict != spec.expected.verdict:
        failures.append(CaseFailure(
            rule="verdict_mismatch",
            expected=spec.expected.verdict,
            actual=report.verdict,
        ))

    # 2. weighted_mean within range.
    if report.weighted_mean < spec.expected.weighted_mean_min:
        failures.append(CaseFailure(
            rule="weighted_mean_below_min",
            expected=f">= {spec.expected.weighted_mean_min}",
            actual=report.weighted_mean,
        ))
    if report.weighted_mean > spec.expected.weighted_mean_max:
        failures.append(CaseFailure(
            rule="weighted_mean_above_max",
            expected=f"<= {spec.expected.weighted_mean_max}",
            actual=report.weighted_mean,
        ))

    # 3. deterministic_mean floor.
    if report.deterministic_mean < spec.expected.deterministic_mean_min:
        failures.append(CaseFailure(
            rule="deterministic_mean_below_min",
            expected=f">= {spec.expected.deterministic_mean_min}",
            actual=report.deterministic_mean,
        ))

    # 4. Per-dim min/max.
    for dim, min_v in spec.expected.per_dim_min.items():
        actual_v = actual_dim.get(dim, 0)
        if actual_v < min_v:
            failures.append(CaseFailure(
                rule=f"{dim}_below_min",
                expected=min_v,
                actual=actual_v,
            ))
    for dim, max_v in spec.expected.per_dim_max.items():
        actual_v = actual_dim.get(dim, 0)
        if actual_v > max_v:
            failures.append(CaseFailure(
                rule=f"{dim}_above_max",
                expected=max_v,
                actual=actual_v,
            ))

    return tuple(failures)


def run_meta_eval(
    cases_dir: Path,
    ctx: Optional[Context] = None,
) -> MetaEvalReport:
    """Load every `*.json` in `cases_dir` and verify the eval against it.

    Cases that load but whose `worktree_path` does not exist are marked
    `skipped` (not `failed`) — this lets fixtures be added incrementally
    before their worktree content lands.

    Cases that load and run but disagree with `expected` are `failed`.
    Cases that raise during eval are `error` (different from `failed`).
    """
    if not cases_dir.is_dir():
        return MetaEvalReport(cases=(), total=0)

    results: List[CaseMetaResult] = []
    for path in sorted(cases_dir.glob("*.json")):
        try:
            spec = CaseSpec.load(path)
        except Exception as exc:
            results.append(CaseMetaResult(
                case_id=path.stem,
                worktree="",
                status="error",
                failures=(CaseFailure(
                    rule="case_load_failed",
                    expected="loadable JSON",
                    actual=f"{type(exc).__name__}: {exc}",
                ),),
            ))
            continue

        if not spec.worktree_path.is_dir():
            results.append(CaseMetaResult(
                case_id=spec.case_id,
                worktree=str(spec.worktree_path),
                status="skipped",
            ))
            continue

        try:
            behavior_report = score_all(
                spec.worktree_path,
                case_id=spec.case_id,
                ctx=ctx,
            )
        except Exception as exc:
            results.append(CaseMetaResult(
                case_id=spec.case_id,
                worktree=str(spec.worktree_path),
                status="error",
                failures=(CaseFailure(
                    rule="eval_raised",
                    expected="BehaviorReport",
                    actual=f"{type(exc).__name__}: {exc}",
                ),),
            ))
            continue

        failures = _check_case(spec, behavior_report)
        result = CaseMetaResult(
            case_id=spec.case_id,
            worktree=str(spec.worktree_path),
            status="passed" if not failures else "failed",
            actual_verdict=behavior_report.verdict,
            actual_weighted_mean=behavior_report.weighted_mean,
            actual_deterministic_mean=behavior_report.deterministic_mean,
            actual_per_dim={s.dim: s.value for s in behavior_report.dimension_scores},
            failures=failures,
        )
        results.append(result)

    return MetaEvalReport(
        cases=tuple(results),
        total=len(results),
        passed=sum(1 for r in results if r.status == "passed"),
        failed=sum(1 for r in results if r.status == "failed"),
        skipped=sum(1 for r in results if r.status == "skipped"),
        errored=sum(1 for r in results if r.status == "error"),
    )


__all__ = [
    "CaseExpected",
    "CaseMetaResult",
    "CaseSpec",
    "MetaEvalReport",
    "CandidateGate",
    "evaluate_candidate",
    "run_meta_eval",
]
