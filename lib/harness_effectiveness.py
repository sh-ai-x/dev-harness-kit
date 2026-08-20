"""Evidence-based workflow effectiveness scoring.

This module is deliberately small: it reduces structured TraceLog events and
existing repair-coordinator events into five component reports. It never
invents evidence from prose or treats missing telemetry as a score.

Issue #663 adds a `stability` submetric nested under
`measurement_integrity`. The stability submetric reports coverage,
score/status, findings, and evidence event IDs for the harness's
behaviour across agent/model/provider swaps. It is *not* a 6th
top-level component — that would change the weighting contract, so
it lives as a submetric that can be ignored by existing 5-component
consumers. The `schema_version` is bumped to advertise the new
submetric.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from lib.trace_log import EVENT_RECORD_REQUIRED_FIELDS, read_events, validate_event

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

COMPONENT_WEIGHTS = {
    # `learning_quality` is intentionally zero until a Phase-4 shadow-mode
    # control cohort exists. The component is still rendered (visibility)
    # but contributes nothing to `overall_score`. The remaining 1.00 is
    # distributed proportionally to the four shippable components based on
    # their pre-rebalance weights (0.20 / 0.20 / 0.25 / 0.15 → 0.80),
    # scaled by 1/0.80 so they sum to 1.00.
    "prevention_quality":      0.25,
    "first_pass_quality":      0.25,
    "recovery_quality":        0.31,
    "learning_quality":        0.00,
    "measurement_integrity":   0.19,
    # NOTE: the formula no longer divides by `sum(weights)` because
    # weights sum to 1.00 by construction (the previous commit did
    # divide; that step is gone). Zero-weight components and None-scored
    # components drop out of both the numerator and the divisor so a
    # partially-scored corpus reports a meaningful overall instead of
    # collapsing to None.
}

# Identity fields an event may carry so the reducer can report on
# agent/model/provider coverage without the verdict depending on them.
# These are top-level optional fields — `validate_event` accepts any
# additional keys, so no schema_version bump is needed on the event
# side.
STABILITY_IDENTITY_FIELDS = ("agent", "provider", "model")


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return None if denominator == 0 else round(numerator / denominator * 100, 1)


def _as_int(value: Any, default: int) -> int:
    """Coerce a possibly-malformed evidence value to int.

    ``evidence_ref`` is free-form — ``trace_log.validate_event`` checks
    only that it is a dict, so a producer could record ``retry_count``
    as a string (e.g. ``"3"`` via the ``append-event`` CLI) or a
    non-numeric value. The reducer must never crash the whole report on
    one malformed field; fall back to ``default`` so the cycle-bound
    metric degrades to "not bounded" instead of raising.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _status(score: Optional[float], *, coverage: float = 1.0) -> str:
    if score is None or coverage < 0.90:
        return INSUFFICIENT_EVIDENCE
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


def _submetric(score: Optional[float], *, submetrics: Dict[str, Any],
               evidence: Iterable[str], coverage: float = 1.0,
               findings: Iterable[str] = (),
               config_version: str = "harness-stability-v1") -> Dict[str, Any]:
    """Submetric-shaped dict used by measurement_integrity.stability.

    Mirrors the _component shape but omits weight (a submetric does
    not contribute to the top-level overall_score) and lets callers
    pick their own config_version so future nested submetrics can be
    advertised independently. See issue #663.
    """
    return {
        "coverage": round(coverage, 4),
        "score": None if score is None else round(max(0.0, min(100.0, score)), 1),
        "status": _status(score, coverage=coverage),
        "submetrics": submetrics,
        "findings": list(findings),
        "evidence_event_ids": sorted(set(evidence)),
        "config_version": config_version,
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
            cycles = _as_int(terminal["evidence_ref"].get("retry_count"), 99)
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


def _stability(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Harness-stability submetric (issue #663).

    Nested under `measurement_integrity`. Five dimensions per the
    proposal, each rendered as its own submetric so the operator can
    diagnose where the evidence is missing:

    1. ``agent_identity_coverage`` — fraction of events carrying any
       of agent / provider / model identity.
    2. ``replay_compatibility`` — event timestamps are monotonic so
       two reducers reading the same JSONL produce the same verdict.
    3. ``agent_provider_neutrality`` — fraction of evidence_ref
       payloads free of any key named like a model/provider/agent
       branch (we treat the simple substring match as a heuristic;
       real coupling is a code-level check outside the reducer).
    4. ``gate_portability`` — fraction of distinct event_types with
       at least one neutral event (no identity fields, no coupling
       fields in evidence_ref). A gate that *only* fires when
       agent/provider/model are present is not portable.
    5. ``contract_test_pass_rate`` — fraction of ``contract.test``
       events with outcome=passed. The producer is expected to emit
       these from a CI job that exercises the harness contract with
       no harness source changes.

    Returns the submetric dict with the shape the reducer uses for
    every submetric: coverage, score (None or a 0..100 number, never
    0.0 when evidence is missing), status, findings, evidence_event_ids.
    Missing identity evidence must surface as ``INSUFFICIENT_EVIDENCE``,
    never ``0.0`` — the issue #663 invariant.
    """
    total = len(events)
    identity_ids: List[str] = []
    coupling_ids: List[str] = []
    neutral_ids: List[str] = []
    neutral_event_ids_by_type: Dict[str, List[str]] = defaultdict(list)
    event_type_counts: Dict[str, int] = defaultdict(int)
    event_type_neutral: Dict[str, int] = defaultdict(int)
    contract_test_ids: List[str] = []
    contract_test_passed = 0
    replay_event_ids: List[str] = []
    replay_violations = 0
    last_ts: Optional[str] = None

    for event in events:
        event_id = event["event_id"]
        has_identity = any(bool(event.get(key)) for key in STABILITY_IDENTITY_FIELDS)
        if has_identity:
            identity_ids.append(event_id)
        evidence_ref = event.get("evidence_ref") or {}
        has_coupling = any(
            isinstance(k, str) and (
                "model" in k.lower() or "provider" in k.lower() or "agent" in k.lower()
            )
            for k in evidence_ref.keys()
        )
        if has_coupling:
            coupling_ids.append(event_id)
        event_type = event.get("event_type", "")
        event_type_counts[event_type] += 1
        if not has_identity and not has_coupling:
            neutral_ids.append(event_id)
            neutral_event_ids_by_type[event_type].append(event_id)
            event_type_neutral[event_type] += 1
        if event_type == "contract.test":
            contract_test_ids.append(event_id)
            if event.get("outcome") == "passed":
                contract_test_passed += 1
        ts = event.get("ts")
        if ts is not None:
            replay_event_ids.append(event_id)
            if last_ts is not None and ts < last_ts:
                replay_violations += 1
            last_ts = ts

    identity_coverage_pct = _ratio(len(identity_ids), total)
    neutrality_coverage_pct = _ratio(len(neutral_ids), total)
    distinct_types = set(event_type_counts.keys())
    portable_types = {t for t in distinct_types if event_type_neutral[t] > 0}
    # gate_portability evidence: real event IDs that landed in a
    # portable event_type (one with at least one neutral event), not
    # the event_type strings themselves.
    portable_event_ids: List[str] = [
        eid for t in portable_types for eid in neutral_event_ids_by_type[t]
    ]
    gate_portability_pct = (
        _ratio(len(portable_types), len(distinct_types)) if distinct_types else None
    )
    contract_test_rate_pct = (
        _ratio(contract_test_passed, len(contract_test_ids))
        if contract_test_ids else None
    )
    # Replay compatibility: every event's ts must be >= the previous
    # one. Empty corpus: coverage 1.0 (vacuously compatible).
    replay_compatible = replay_violations == 0
    replay_coverage_pct = (
        _ratio(len(replay_event_ids) - replay_violations, len(replay_event_ids))
        if replay_event_ids else 100.0
    )

    submetrics = {
        "agent_identity_coverage": _metric(
            len(identity_ids), total,
            evidence=identity_ids, value=identity_coverage_pct,
        ),
        "replay_compatibility": _metric(
            len(replay_event_ids) - replay_violations, len(replay_event_ids),
            evidence=replay_event_ids, value=replay_coverage_pct,
        ),
        "agent_provider_neutrality": _metric(
            len(neutral_ids), total,
            evidence=neutral_ids, value=neutrality_coverage_pct,
        ),
        "gate_portability": _metric(
            len(portable_types), len(distinct_types),
            evidence=portable_event_ids, value=gate_portability_pct,
        ),
        "contract_test_pass_rate": _metric(
            contract_test_passed, len(contract_test_ids),
            evidence=contract_test_ids, value=contract_test_rate_pct,
        ),
    }

    # Findings — one per missing dim so the operator can pin the gap.
    findings: List[str] = []
    if not identity_ids:
        findings.append(
            "agent/provider/model identity not recorded on any event"
        )
    elif identity_coverage_pct is not None and identity_coverage_pct < 50.0:
        findings.append(
            f"agent/provider/model identity recorded on only "
            f"{identity_coverage_pct:.1f}% of events"
        )
    if not replay_compatible:
        findings.append(
            f"event timestamps not monotonic across {replay_violations} "
            f"pair(s) — replay may diverge"
        )
    if coupling_ids:
        findings.append(
            f"{len(coupling_ids)} event(s) carry model/provider/agent "
            f"keys in evidence_ref — possible harness coupling"
        )
    if contract_test_rate_pct is None:
        findings.append("no contract.test events observed")

    # Score: only meaningful when the producer emits enough identity +
    # contract.test evidence. The 5-component overall_score is
    # independent of this submetric — see `_integrity`.
    sub_coverage_values: List[float] = [
        v for v in (
            identity_coverage_pct,
            replay_coverage_pct,
            neutrality_coverage_pct,
            gate_portability_pct,
            contract_test_rate_pct,
        ) if v is not None
    ]
    coverage_pct = min(sub_coverage_values) if sub_coverage_values else 0.0
    coverage = coverage_pct / 100.0

    if (identity_coverage_pct is not None
            and contract_test_rate_pct is not None
            and gate_portability_pct is not None
            and replay_coverage_pct is not None
            and neutrality_coverage_pct is not None
            and coverage >= 0.90):
        score = (
            identity_coverage_pct * 0.25
            + replay_coverage_pct * 0.20
            + neutrality_coverage_pct * 0.20
            + gate_portability_pct * 0.15
            + contract_test_rate_pct * 0.20
        )
    else:
        score = None

    return _submetric(
        score,
        submetrics=submetrics,
        evidence=identity_ids if identity_ids else neutral_ids,
        coverage=coverage,
        findings=findings,
    )


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
    coverage_pct = _ratio(len(started_subjects & terminal_subjects), len(started_subjects)) if started_subjects else None
    coverage = None if coverage_pct is None else coverage_pct / 100.0
    score = None if None in (schema, dedupe, attribution, coverage_pct) else (
        schema * .3 + attribution * .25 + dedupe * .2 + coverage_pct * .25
    )
    metrics = {"schema_completeness": _metric(parsed_count, len(raw_lines), evidence=valid_ids, value=schema),
               "attribution_completeness": _metric(parents, len(events), evidence=valid_ids, value=attribution),
               "dedupe_integrity": _metric(len(set(valid_ids)), parsed_count, evidence=valid_ids, value=dedupe),
               "event_coverage": _metric(len(started_subjects & terminal_subjects), len(started_subjects), evidence=valid_ids, value=coverage_pct)}
    status = INSUFFICIENT_EVIDENCE if findings else _status(
        score, coverage=0.0 if coverage is None else coverage,
    )
    # Issue #663: the stability submetric is nested here, not added as
    # a 6th top-level component (which would change COMPONENT_WEIGHTS
    # and break the 5-component overall_score contract).
    metrics["stability"] = _stability(events)
    result = _component(
        "measurement_integrity", score, submetrics=metrics, evidence=valid_ids,
        coverage=0.0 if coverage is None else coverage, findings=findings,
    )
    result["status"] = status
    return result


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(line)
        if not isinstance(data, dict) or not set(EVENT_RECORD_REQUIRED_FIELDS).issubset(data):
            return None
        return validate_event(data)
    except (ValueError, TypeError):
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
    scored = {name: item for name, item in components.items()
              if item["score"] is not None}
    if not scored:
        overall = None
    else:
        # None-scored components drop out of both numerator (their
        # contribution is 0) and divisor (subtract their weight from the
        # divisor). Zero-weight components do the same. Weights sum to
        # 1.00 by construction; the divisor below equals the sum of
        # weights for the scored set, so the result stays in [0, 100].
        # The earlier "any score is None ⇒ overall None" guard collapsed
        # the visible scorecard to INSUFFICIENT_EVIDENCE whenever a
        # single component lacked evidence; the fix is to honor the
        # actual evidence instead.
        numerator = sum(item["score"] * COMPONENT_WEIGHTS[name]
                        for name, item in scored.items())
        divisor = sum(COMPONENT_WEIGHTS[name] for name in scored)
        overall = round(numerator / divisor, 1) if divisor else None
    return {
        # Bumped from 1 → 2 in issue #663 to advertise the nested
        # stability submetric. The top-level shape is unchanged so
        # 5-component consumers still work; new consumers can opt in
        # to `components.measurement_integrity.submetrics.stability`.
        "schema_version": 2,
        "contract_version": "harness-effectiveness-v1",
        "components": components,
        "overall_score": overall,
        "status": INSUFFICIENT_EVIDENCE if overall is None else _status(overall),
        "event_count": len(events),
    }
