"""Evidence-based workflow effectiveness scoring.

This module is deliberately small: it reduces structured TraceLog events and
existing repair-coordinator events into five component reports. It never
invents evidence from prose or treats missing telemetry as a score.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from lib.trace_log import EVENT_REQUIRED_FIELDS, read_events, validate_event

COMPONENT_WEIGHTS = {
    "prevention_quality": 0.20,
    "first_pass_quality": 0.20,
    "recovery_quality": 0.25,
    "learning_quality": 0.20,
    "measurement_integrity": 0.15,
}
REQUIRED_EVENT_FIELDS = set(EVENT_REQUIRED_FIELDS) | {"schema_version"}


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return None if denominator == 0 else round(numerator / denominator * 100, 1)


def _status(score: Optional[float], *, coverage: float = 1.0) -> str:
    if score is None or coverage < 0.90:
        return "INSUFFICIENT_EVIDENCE"
    if score >= 80:
        return "OK"
    if score >= 60:
        return "DRIFT_WARNING"
    return "ROT"


def _metric(numerator: int, denominator: int, *, evidence: Iterable[str],
            coverage: float = 1.0, value: Optional[float] = None) -> Dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": _ratio(numerator, denominator) if value is None else value,
        "coverage": round(coverage, 4),
        "evidence_event_ids": sorted(set(evidence)),
    }


def _component(name: str, score: Optional[float], *, submetrics: Dict[str, Any],
               evidence: Iterable[str], coverage: float = 1.0,
               findings: Iterable[str] = ()) -> Dict[str, Any]:
    return {
        "score": None if score is None else round(max(0.0, min(100.0, score)), 1),
        "weight": COMPONENT_WEIGHTS[name],
        "status": _status(score, coverage=coverage),
        "coverage": round(coverage, 4),
        "submetrics": submetrics,
        "evidence_event_ids": sorted(set(evidence)),
        "findings": list(findings),
        "config_version": "harness-effectiveness-v1",
    }


def _events_by_subject(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["subject_id"]].append(event)
    for values in grouped.values():
        values.sort(key=lambda item: item["ts"])
    return grouped


def _prevention(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    guards = [e for e in events if e["event_type"] in {"guard.blocked", "guard.allowed"}]
    labeled = [e for e in guards if e["evidence_ref"].get("ground_truth") in {"unsafe", "legitimate"}]
    evidence_ids = [e["event_id"] for e in guards]
    complete = [e for e in guards if e["evidence_ref"].get("policy_id") and e["evidence_ref"].get("reason")]
    if not labeled:
        return _component("prevention_quality", None, coverage=0.0,
                          submetrics={"block_rate": _metric(len([e for e in guards if e["outcome"] == "blocked"]), len(guards), evidence=evidence_ids)},
                          evidence=evidence_ids,
                          findings=["ground_truth label missing for guard actions"])
    tp = sum(e["outcome"] == "blocked" and e["evidence_ref"].get("ground_truth") == "unsafe" for e in labeled)
    fp = sum(e["outcome"] == "blocked" and e["evidence_ref"].get("ground_truth") == "legitimate" for e in labeled)
    fn = sum(e["outcome"] == "allowed" and e["evidence_ref"].get("ground_truth") == "unsafe" for e in labeled)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    evidence_quality = _ratio(len(complete), len(guards))
    coverage = len(labeled) / len(guards) if guards else 0.0
    score = None if coverage < 0.90 or None in (precision, recall, evidence_quality) else precision * .5 + recall * .3 + evidence_quality * .2
    return _component("prevention_quality", score, coverage=coverage,
                      submetrics={"guard_precision": _metric(tp, tp + fp, evidence=evidence_ids, value=precision),
                                  "guard_recall": _metric(tp, tp + fn, evidence=evidence_ids, value=recall),
                                  "block_evidence_quality": _metric(len(complete), len(guards), evidence=evidence_ids, value=evidence_quality)},
                      evidence=evidence_ids)


def _first_pass(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    groups = _events_by_subject(events)
    writes = {sid: values for sid, values in groups.items() if any(e["event_type"] == "write.observed" for e in values)}
    eligible = 0
    passed = 0
    complete = 0
    no_retry = 0
    evidence_ids: List[str] = []
    for values in writes.values():
        write = next(e for e in values if e["event_type"] == "write.observed")
        verifies = [e for e in values if e["event_type"] in {"verify.passed", "verify.failed"} and e["ts"] >= write["ts"]]
        if not verifies:
            continue
        first = verifies[0]
        eligible += 1
        evidence_ids.extend([write["event_id"], first["event_id"]])
        causally_linked = first.get("parent_id") == write["event_id"]
        if (causally_linked and first["event_type"] == "verify.passed"
                and first["evidence_ref"].get("required_checks_passed", False)):
            passed += 1
        if (causally_linked
                and all(key in first["evidence_ref"] for key in ("required_checks_passed", "retry_count"))):
            complete += 1
        if not any(e["event_type"] in {"heal.attempted", "verify.failed"} and e["ts"] < first["ts"] for e in values):
            no_retry += 1
    if eligible == 0:
        return _component("first_pass_quality", None, coverage=0.0, submetrics={}, evidence=evidence_ids,
                          findings=["no write with first verification evidence"])
    metrics = {
        "first_pass_rate": _metric(passed, eligible, evidence=evidence_ids),
        "first_verify_evidence": _metric(complete, eligible, evidence=evidence_ids),
        "first_pass_no_hidden_retry": _metric(no_retry, eligible, evidence=evidence_ids),
    }
    score = metrics["first_pass_rate"]["value"] * .7 + metrics["first_verify_evidence"]["value"] * .2 + metrics["first_pass_no_hidden_retry"]["value"] * .1
    return _component("first_pass_quality", score, submetrics=metrics, evidence=evidence_ids)


def _recovery(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    groups = _events_by_subject(events)
    errors = [values for values in groups.values() if any(e["event_type"] == "verify.failed" for e in values)]
    if not errors:
        return _component("recovery_quality", None, coverage=0.0, submetrics={}, evidence=[],
                          findings=["no verification errors observed"])
    verified = clean = independent = bounded = no_regression = 0
    evidence_ids: List[str] = []
    for values in errors:
        error = next(e for e in values if e["event_type"] == "verify.failed")
        heals = [e for e in values if e["event_type"] == "heal.attempted" and e["ts"] > error["ts"]]
        verifies = [e for e in values if e["event_type"] == "verify.passed" and e["ts"] > error["ts"]]
        terminal = verifies[-1] if verifies else None
        evidence_ids.append(error["event_id"])
        if heals:
            evidence_ids.append(heals[0]["event_id"])
        if terminal:
            evidence_ids.append(terminal["event_id"])
            clean += 1
            linked = bool(heals and heals[0].get("parent_id") == error["event_id"]
                          and terminal.get("parent_id") == heals[0]["event_id"])
            independent += bool(linked and terminal["evidence_ref"].get("independent", False))
            cycles = int(terminal["evidence_ref"].get("retry_count", 99))
            bounded += bool(linked and cycles <= 3)
            no_regression += not any(e["event_type"] == "verify.failed" and e["ts"] > terminal["ts"] for e in values)
            verified += bool(linked)
    total = len(errors)
    metrics = {
        "verified_recovery_rate": _metric(verified, total, evidence=evidence_ids),
        "independent_verification_rate": _metric(independent, clean, evidence=evidence_ids),
        "cycle_bound_score": _metric(bounded, clean, evidence=evidence_ids),
        "recovery_no_regression": _metric(no_regression, clean, evidence=evidence_ids),
    }
    score = (metrics["verified_recovery_rate"]["value"] * .45
             + metrics["independent_verification_rate"]["value"] * .25
             + metrics["cycle_bound_score"]["value"] * .20
             + metrics["recovery_no_regression"]["value"] * .10) if clean else None
    return _component("recovery_quality", score, submetrics=metrics, evidence=evidence_ids)


def _learning(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    outcomes = [e for e in events if e["event_type"] == "learning.outcome"]
    treatment = [e for e in outcomes if e["evidence_ref"].get("cohort") == "treatment"]
    control = [e for e in outcomes if e["evidence_ref"].get("cohort") == "control"]
    ids = [e["event_id"] for e in outcomes]
    if not treatment or not control:
        return _component("learning_quality", None, coverage=0.0,
                          submetrics={}, evidence=ids,
                          findings=["comparable treatment and control cohorts missing"])
    tr = sum(bool(e["evidence_ref"].get("verified_recovery")) for e in treatment) / len(treatment)
    co = sum(bool(e["evidence_ref"].get("verified_recovery")) for e in control) / len(control)
    delta = round((tr - co) * 100, 1)
    yield_score = max(0.0, min(100.0, 50.0 + delta * 10.0))
    metrics = {"learning_yield_score": _metric(
                   sum(bool(e["evidence_ref"].get("verified_recovery")) for e in treatment + control),
                   len(treatment) + len(control), evidence=ids, value=yield_score),
               "control_treatment_delta_pp": {"value": delta, "evidence_event_ids": ids}}
    return _component("learning_quality", yield_score, submetrics=metrics, evidence=ids)


def _integrity(root: Path, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    path = root / ".dev-kit" / "trace" / "events.jsonl"
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    valid_ids = [e["event_id"] for e in events]
    findings: List[str] = []
    if len(valid_ids) != len(set(valid_ids)):
        findings.append("duplicate event_id")
    parsed_count = sum(1 for line in raw_lines if _parse_line(line) is not None)
    malformed = len(raw_lines) - parsed_count
    if malformed:
        findings.append(f"malformed event lines: {malformed}")
    schema = _ratio(parsed_count, len(raw_lines))
    dedupe = _ratio(len(set(valid_ids)), parsed_count) if parsed_count else None
    parents = sum(1 for e in events if e["parent_id"] is None or e["parent_id"] in set(valid_ids))
    attribution = _ratio(parents, len(events))
    started_subjects = {
        e["subject_id"] for e in events if e["event_type"] == "step.started"
    }
    terminal_subjects = {
        e["subject_id"] for e in events
        if e["event_type"] in {"step.completed", "step.failed", "step.blocked"}
    }
    coverage = _ratio(len(started_subjects & terminal_subjects), len(started_subjects)) if started_subjects else None
    score = None if None in (schema, dedupe, attribution, coverage) else schema * .3 + attribution * .25 + dedupe * .2 + coverage * .25
    metrics = {"schema_completeness": _metric(parsed_count, len(raw_lines), evidence=valid_ids, value=schema),
               "attribution_completeness": _metric(parents, len(events), evidence=valid_ids, value=attribution),
               "dedupe_integrity": _metric(len(set(valid_ids)), parsed_count, evidence=valid_ids, value=dedupe),
               "event_coverage": _metric(len(started_subjects & terminal_subjects), len(started_subjects), evidence=valid_ids, value=coverage)}
    status = "INSUFFICIENT_EVIDENCE" if findings else _status(
        score, coverage=0.0 if coverage is None else coverage,
    )
    result = _component(
        "measurement_integrity", score, submetrics=metrics, evidence=valid_ids,
        coverage=0.0 if coverage is None else coverage, findings=findings,
    )
    result["status"] = status
    return result


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(line)
        if not isinstance(data, dict) or not REQUIRED_EVENT_FIELDS.issubset(data):
            return None
        return validate_event(data)
    except (json.JSONDecodeError, TypeError):
        return None


def build_report(root: Path) -> Dict[str, Any]:
    """Build the deterministic effectiveness report for one project root."""
    events = read_events(root)
    components = {
        "prevention_quality": _prevention(events),
        "first_pass_quality": _first_pass(events),
        "recovery_quality": _recovery(events),
        "learning_quality": _learning(events),
        "measurement_integrity": _integrity(root, events),
    }
    scores = [item["score"] for item in components.values()]
    overall = None if any(score is None for score in scores) else round(sum(components[name]["score"] * COMPONENT_WEIGHTS[name] for name in components), 1)
    return {
        "schema_version": 1,
        "contract_version": "harness-effectiveness-v1",
        "components": components,
        "overall_score": overall,
        "status": "INSUFFICIENT_EVIDENCE" if overall is None else _status(overall),
        "event_count": len(events),
    }
