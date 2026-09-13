"""Deterministic metrics reducer for Ralph evidence events.

The reducer consumes events produced by :mod:`ralph_events` (or compatible
``lib.trace_log`` records). It deliberately distinguishes three cases:

* a measured zero: denominator and terminal evidence exist, but the
  numerator is zero;
* incomplete evidence: a start/failure exists without its terminal evidence;
* degraded observability: the event producer recorded that telemetry was
  degraded.

Only the first case receives a numeric ``0.0``. Incomplete evidence receives
``value=None`` and ``INSUFFICIENT_EVIDENCE`` so a missing callback cannot make
the Ralph loop look successful or merely score zero.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from lib.context_budget import context_metrics
from lib.harness_effectiveness import INSUFFICIENT_EVIDENCE
from lib.trace_log import read_events

METRICS_SCHEMA_VERSION = 1
METRICS_CONTRACT_VERSION = "ralph-metrics-v1"
STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_DEGRADED = "DEGRADED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

STAGE_START_EVENTS = frozenset({
    "stage.started", "stage.attempt.started", "step.started",
})
STAGE_TERMINAL_EVENTS = frozenset(
    {
        "stage.completed", "stage.failed", "stage.blocked",
        "stage.attempt.finished", "stage.attempt.interrupted",
        "step.completed", "step.failed", "step.blocked",
    }
)
RUN_START_EVENTS = frozenset({"run.started", "ralph.started"})
RUN_TERMINAL_EVENTS = frozenset(
    {
        "run.completed", "run.failed", "run.blocked", "ralph.completed",
        "ralph.failed", "ralph.blocked", "terminal.reached",
    }
)
FAILURE_EVENTS = frozenset(
    {
        "stage.failed", "stage.blocked", "stage.attempt.interrupted",
        "step.failed", "step.blocked", "run.failed", "run.blocked",
        "ralph.failed", "recovery.required",
    }
)
RECOVERY_TERMINAL_EVENTS = frozenset(
    {
        "recovery.completed", "recovery.succeeded", "recovery.failed",
        "recovery.required", "recovery.blocked", "heal.completed", "heal.failed",
    }
)
SUCCESS_OUTCOMES = frozenset({
    "completed", "passed", "success", "succeeded", "ok", "done",
    # A healthy single-operator run intentionally stops at the SHIP-owned
    # human merge boundary. It is converged workflow evidence, not a failed
    # stage or an automatic merge claim.
    "human_merge", "user_merge_required",
})
_DEGRADED_VALUES = frozenset({"degraded", "collection_error", "missing"})


def _event_id(event: Mapping[str, Any]) -> str:
    value = event.get("event_id")
    return value if isinstance(value, str) and value else ""


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get("event_type")
    return value.strip().lower() if isinstance(value, str) else ""


def _stage_id(event: Mapping[str, Any]) -> str:
    value = event.get("stage_id") or event.get("stage")
    return value.strip() if isinstance(value, str) else ""


def _identity(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    evidence = event.get("evidence_ref")
    evidence_ref = evidence if isinstance(evidence, Mapping) else {}
    run = event.get("run_id") or evidence_ref.get("run_id") or ""
    attempt = event.get("attempt_id") or evidence_ref.get("attempt_id") or ""
    stage = _stage_id(event) or evidence_ref.get("stage_id") or ""
    subject = event.get("subject_id") or ""
    return (
        run.strip() if isinstance(run, str) else "",
        attempt.strip() if isinstance(attempt, str) else "",
        stage.strip() if isinstance(stage, str) else "",
        subject.strip() if isinstance(subject, str) else "",
    )


def _is_degraded(event: Mapping[str, Any]) -> bool:
    evidence = event.get("evidence_ref")
    evidence_ref = evidence if isinstance(evidence, Mapping) else {}
    values = (
        event.get("observability_status"),
        evidence_ref.get("observability_status"),
        event.get("status"),
    )
    return any(isinstance(value, str) and value.lower() in _DEGRADED_VALUES for value in values)


def _outcome(event: Mapping[str, Any]) -> str:
    value = event.get("outcome")
    return value.strip().lower() if isinstance(value, str) else ""


def _metric(
    numerator: int,
    denominator: int,
    *,
    coverage: float,
    evidence_event_ids: Iterable[str],
    degraded: bool = False,
    findings: Iterable[str] = (),
    force_incomplete: bool = False,
) -> dict[str, Any]:
    """Build the common metric shape and enforce the missing-evidence rule."""
    numerator = max(0, int(numerator))
    denominator = max(0, int(denominator))
    coverage = max(0.0, min(1.0, float(coverage)))
    ids = sorted({item for item in evidence_event_ids if isinstance(item, str) and item})
    incomplete = denominator == 0 or not ids or force_incomplete or coverage < 1.0
    value: Optional[float]
    if incomplete:
        value = None
        status = INSUFFICIENT_EVIDENCE
    else:
        value = round(numerator / denominator * 100.0, 1)
        status = STATUS_DEGRADED if degraded else (
            STATUS_OK if numerator == denominator else STATUS_FAILED
        )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "coverage": round(coverage, 4),
        "evidence_event_ids": ids,
        "status": status,
        "findings": list(findings),
    }


def _dedupe(events: Iterable[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int, int]:
    """Drop duplicate event ids without double-counting idempotent retries."""
    unique: list[Mapping[str, Any]] = []
    seen: dict[str, Mapping[str, Any]] = {}
    invalid = 0
    conflicts = 0
    for event in events:
        if not isinstance(event, Mapping):
            invalid += 1
            continue
        event_id = _event_id(event)
        if not event_id:
            invalid += 1
            continue
        if event_id in seen:
            if dict(seen[event_id]) != dict(event):
                conflicts += 1
            continue
        seen[event_id] = event
        unique.append(event)
    return unique, invalid, conflicts


def _paired(
    starts: Sequence[Mapping[str, Any]],
    terminals: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str, str], Mapping[str, Any]], dict[tuple[str, str, str, str], Mapping[str, Any]]]:
    started_by_key: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for event in starts:
        started_by_key.setdefault(_identity(event), event)
    terminal_by_key: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for event in terminals:
        key = _identity(event)
        if key in started_by_key:
            terminal_by_key.setdefault(key, event)
    return started_by_key, terminal_by_key


def _stage_metrics(
    events: Sequence[Mapping[str, Any]], *, expected_stage_ids: Optional[Iterable[str]] = None, degraded: bool
) -> dict[str, dict[str, Any]]:
    starts = [event for event in events if _event_type(event) in STAGE_START_EVENTS]
    terminals = [event for event in events if _event_type(event) in STAGE_TERMINAL_EVENTS]
    started_by_key, terminal_by_key = _paired(starts, terminals)
    expected = sorted({item for item in (expected_stage_ids or ()) if isinstance(item, str) and item})

    if expected:
        expected_keys = {
            key for key in started_by_key
            if key[2] in expected
        }
        denominator = len(expected)
        completed_keys = {
            key for key in terminal_by_key
            if key[2] in expected
        }
        evidence = [
            _event_id(event)
            for key, event in started_by_key.items()
            if key[2] in expected
        ] + [
            _event_id(event)
            for key, event in terminal_by_key.items()
            if key[2] in expected
        ]
        # Expected stage ids without a corresponding run/attempt are missing
        # evidence, even if an unrelated stage happened to complete.
        force_incomplete = len(expected_keys) < denominator
    else:
        denominator = len(started_by_key)
        completed_keys = set(terminal_by_key)
        evidence = [
            _event_id(event) for event in started_by_key.values()
        ] + [_event_id(event) for event in terminal_by_key.values()]
        force_incomplete = False

    completion = _metric(
        len(completed_keys),
        denominator,
        coverage=(len(completed_keys) / denominator) if denominator else 0.0,
        evidence_event_ids=evidence,
        degraded=degraded,
        force_incomplete=force_incomplete,
        findings=(
            ["stage terminal evidence missing"]
            if denominator and len(completed_keys) < denominator
            else []
        ),
    )

    paired_terminals = [
        event for key, event in terminal_by_key.items()
        if not expected or key[2] in expected
    ]
    successful = sum(_outcome(event) in SUCCESS_OUTCOMES for event in paired_terminals)
    success_evidence = [_event_id(event) for event in paired_terminals]
    success_force_incomplete = force_incomplete or not paired_terminals
    success_coverage = (
        len(paired_terminals) / denominator if denominator else 0.0
    )
    success = _metric(
        successful,
        len(paired_terminals),
        coverage=success_coverage,
        evidence_event_ids=success_evidence,
        degraded=degraded,
        force_incomplete=success_force_incomplete,
        findings=(
            ["stage start evidence missing"]
            if not starts and terminals
            else []
        ),
    )
    return {
        "stage_completion_rate": completion,
        "stage_success_rate": success,
    }


def _run_metric(events: Sequence[Mapping[str, Any]], *, degraded: bool) -> dict[str, Any]:
    starts = [event for event in events if _event_type(event) in RUN_START_EVENTS]
    terminals = [event for event in events if _event_type(event) in RUN_TERMINAL_EVENTS]
    # Run terminal events may carry the final attempt/stage identity while
    # ``run.started`` intentionally has a run-level attempt. Pair only on
    # run_id so a valid SHIP terminal cannot disappear from convergence.
    started_by_key: dict[str, Mapping[str, Any]] = {}
    for event in starts:
        run_id = _identity(event)[0]
        if run_id:
            started_by_key.setdefault(run_id, event)
    terminal_by_key: dict[str, Mapping[str, Any]] = {}
    for event in terminals:
        run_id = _identity(event)[0]
        if run_id in started_by_key:
            terminal_by_key.setdefault(run_id, event)
    evidence = [_event_id(event) for event in started_by_key.values()]
    evidence += [_event_id(event) for event in terminal_by_key.values()]
    return _metric(
        sum(_outcome(event) in SUCCESS_OUTCOMES for event in terminal_by_key.values()),
        len(started_by_key),
        coverage=(len(terminal_by_key) / len(started_by_key)) if started_by_key else 0.0,
        evidence_event_ids=evidence,
        degraded=degraded,
        findings=["run terminal evidence missing"] if started_by_key and not terminal_by_key else [],
    )


def _recovery_metric(events: Sequence[Mapping[str, Any]], *, degraded: bool) -> dict[str, Any]:
    failures = [event for event in events if _event_type(event) in FAILURE_EVENTS]
    if not failures:
        # No failure is a valid completed-run condition, not missing recovery
        # evidence. Keep the metric visible as N/A so it does not turn a
        # healthy BUILD → BABYSIT → SHIP run into an incomplete report.
        return {
            "numerator": 0,
            "denominator": 0,
            "value": None,
            "coverage": 1.0,
            "evidence_event_ids": [],
            "status": STATUS_NOT_APPLICABLE,
            "findings": [],
        }
    recoveries = [event for event in events if _event_type(event) in RECOVERY_TERMINAL_EVENTS]
    recovery_by_key: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    failure_keys: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for failure in failures:
        failure_keys.setdefault(_identity(failure), failure)
    for recovery in recoveries:
        key = _identity(recovery)
        if key in failure_keys:
            recovery_by_key.setdefault(key, recovery)
        parent_id = recovery.get("parent_id")
        if isinstance(parent_id, str):
            for key, failure in failure_keys.items():
                if failure.get("event_id") == parent_id:
                    recovery_by_key.setdefault(key, recovery)
    evidence = [_event_id(event) for event in failure_keys.values()]
    evidence += [_event_id(event) for event in recovery_by_key.values()]
    recovered = sum(
        _event_type(event) in {"recovery.completed", "recovery.succeeded", "heal.completed"}
        or _outcome(event) in SUCCESS_OUTCOMES
        for event in recovery_by_key.values()
    )
    denominator = len(failure_keys)
    observed = len(recovery_by_key)
    return _metric(
        recovered,
        denominator,
        coverage=(observed / denominator) if denominator else 0.0,
        evidence_event_ids=evidence,
        degraded=degraded,
        findings=["recovery terminal evidence missing"] if denominator and observed < denominator else [],
    )


def _observability_metric(
    events: Sequence[Mapping[str, Any]], *, invalid_count: int, conflict_count: int
) -> dict[str, Any]:
    denominator = len(events) + invalid_count
    healthy = sum(not _is_degraded(event) for event in events)
    coverage = len(events) / denominator if denominator else 0.0
    evidence = [_event_id(event) for event in events]
    findings: list[str] = []
    if invalid_count:
        findings.append(f"invalid events omitted: {invalid_count}")
    if conflict_count:
        findings.append(f"conflicting duplicate event ids: {conflict_count}")
    if any(_is_degraded(event) for event in events):
        findings.append("one or more events report degraded observability")
    return _metric(
        healthy,
        denominator,
        coverage=coverage,
        evidence_event_ids=evidence,
        degraded=any(_is_degraded(event) for event in events),
        findings=findings,
        force_incomplete=bool(invalid_count or conflict_count),
    )


def reduce_metrics(
    events_or_root: Iterable[Mapping[str, Any]] | Path,
    *,
    expected_stage_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Reduce Ralph events into a deterministic, evidence-linked report."""
    if isinstance(events_or_root, Path):
        raw_events: Iterable[Mapping[str, Any]] = read_events(events_or_root)
    else:
        raw_events = events_or_root
    events, invalid_count, conflict_count = _dedupe(raw_events)
    degraded = any(_is_degraded(event) for event in events)
    metrics = _stage_metrics(
        events, expected_stage_ids=expected_stage_ids, degraded=degraded
    )
    metrics["run_convergence_rate"] = _run_metric(events, degraded=degraded)
    metrics["recovery_success_rate"] = _recovery_metric(events, degraded=degraded)
    metrics["observability_coverage"] = _observability_metric(
        events, invalid_count=invalid_count, conflict_count=conflict_count
    )
    context_rows: list[dict[str, Any]] = []
    for event in events:
        evidence = event.get("evidence_ref")
        if not isinstance(evidence, Mapping):
            continue
        handoff = evidence.get("handoff")
        row = dict(handoff) if isinstance(handoff, Mapping) else {}
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "replay_tokens"):
            if key in evidence:
                row[key] = evidence[key]
        row["payload_bytes"] = len(str(event).encode("utf-8"))
        context_rows.append(row)

    statuses = [metric["status"] for metric in metrics.values()]
    if STATUS_DEGRADED in statuses:
        status = STATUS_DEGRADED
    elif INSUFFICIENT_EVIDENCE in statuses:
        status = INSUFFICIENT_EVIDENCE
    elif STATUS_FAILED in statuses:
        status = STATUS_FAILED
    else:
        status = STATUS_OK
    complete_values = [metric["value"] for metric in metrics.values() if metric["value"] is not None]
    score = (
        round(sum(complete_values) / len(complete_values), 1)
        if complete_values and status in {STATUS_OK, STATUS_FAILED}
        else None
    )
    evidence_ids = sorted({_event_id(event) for event in events if _event_id(event)})
    coverage_values = [metric["coverage"] for metric in metrics.values()]
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "contract_version": METRICS_CONTRACT_VERSION,
        "status": status,
        "score": score,
        "coverage": round(min(coverage_values), 4) if coverage_values else 0.0,
        "evidence_event_ids": evidence_ids,
        "event_count": len(events),
        "invalid_event_count": invalid_count,
        "conflicting_event_count": conflict_count,
        "metrics": metrics,
        "context_metrics": context_metrics(context_rows, expected=len(events)),
    }


def reduce(events_or_root: Iterable[Mapping[str, Any]] | Path, **kwargs: Any) -> dict[str, Any]:
    """Short alias for :func:`reduce_metrics`."""
    return reduce_metrics(events_or_root, **kwargs)


build_report = reduce_metrics


class MetricsReducer:
    """Object wrapper for callers that keep reducer configuration."""

    def __init__(self, *, expected_stage_ids: Optional[Iterable[str]] = None) -> None:
        self.expected_stage_ids = tuple(expected_stage_ids or ())

    def reduce(self, events_or_root: Iterable[Mapping[str, Any]] | Path) -> dict[str, Any]:
        return reduce_metrics(
            events_or_root, expected_stage_ids=self.expected_stage_ids
        )


__all__ = [
    "METRICS_CONTRACT_VERSION",
    "METRICS_SCHEMA_VERSION",
    "MetricsReducer",
    "STATUS_DEGRADED",
    "STATUS_FAILED",
    "STATUS_NOT_APPLICABLE",
    "STATUS_OK",
    "build_report",
    "reduce",
    "reduce_metrics",
]
