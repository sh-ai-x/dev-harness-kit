"""Tests for the harness-stability submetric (issue #663).

The stability submetric is nested under `measurement_integrity` and
reports evidence about how the harness behaves across agent/model/
provider swaps. These tests pin the contract:

1. TraceLog events accept agent/provider/model fields without making
   the verdict depend on a specific model.
2. Model/provider swap leaves the reducer verdict byte-identical.
3. Replay (same input) yields byte-identical verdicts.
4. Missing evidence is reported as INSUFFICIENT_EVIDENCE, never 0.0.
5. The submetric exposes coverage + score/status + findings +
   evidence_event_ids.
6. The 5-component `overall_score` contract is preserved (weights
   still sum to 1.0, schema_version is bumped to advertise the new
   stability evidence).
"""
from __future__ import annotations

from pathlib import Path

from lib.harness_effectiveness import (
    COMPONENT_WEIGHTS,
    INSUFFICIENT_EVIDENCE,
    build_report,
)
from lib.trace_log import EVENT_SCHEMA_VERSION, append_event, read_events


def _event(root: Path, *, event_id: str, event_type: str, subject: str,
           outcome: str, ts: str, parent=None,
           evidence=None,
           agent=None, provider=None, model=None) -> None:
    payload = {
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
        "evidence_ref": evidence or {},
    }
    if agent is not None:
        payload["agent"] = agent
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    append_event(root, payload)


def _full_event_corpus(tmp_path: Path, *, identity=None) -> None:
    """Emit the minimum event set that scores all four shippable
    components to 100.0 (and leaves learning_quality unscored).
    """

    def emit(event_id, event_type, subject, outcome, ts, parent=None, **evidence):
        payload = {
            "event_id": event_id, "run_id": "run-1", "workflow_id": "wf-1",
            "stage": "build", "event_type": event_type,
            "subject_id": subject, "parent_id": parent,
            "ts": ts, "outcome": outcome, "source": "test",
            "evidence_ref": evidence,
        }
        if identity:
            payload.update(identity)
        append_event(tmp_path, payload)

    emit("g1", "guard.blocked", "a1", "blocked", "2026-08-12T00:00:00Z",
         policy_id="scope", ground_truth="unsafe", reason="x")
    emit("g2", "guard.allowed", "a2", "allowed", "2026-08-12T00:00:01Z",
         policy_id="scope", ground_truth="unsafe", reason="x")
    emit("w1", "write.observed", "d1", "written", "2026-08-12T00:00:00Z")
    emit("vv", "verify.passed", "d1", "passed", "2026-08-12T00:00:01Z",
         parent="w1", required_checks_passed=True, retry_count=0)
    emit("vf", "verify.failed", "d1", "failed", "2026-08-12T00:00:02Z")
    emit("hh", "heal.attempted", "d1", "attempted", "2026-08-12T00:00:03Z",
         parent="vf")
    emit("vp2", "verify.passed", "d1", "passed", "2026-08-12T00:00:04Z",
         parent="hh", required_checks_passed=True, independent=True, retry_count=1)
    emit("s1", "step.started", "e1", "started", "2026-08-12T00:00:00Z")
    emit("sc", "step.completed", "e1", "completed", "2026-08-12T00:00:01Z")


# ---------------------------------------------------------------------------
# TraceLog accepts identity fields without breaking the verdict
# ---------------------------------------------------------------------------

def test_trace_event_accepts_agent_provider_model_fields(tmp_path: Path) -> None:
    """TraceLog events accept agent/provider/model top-level fields
    without raising; the reducer sees them as opaque metadata that does
    not enter the verdict calculation.
    """
    _event(tmp_path, event_id="s1", event_type="step.started", subject="e1",
           outcome="started", ts="2026-08-12T00:00:00Z",
           agent="claude", provider="anthropic", model="opus-4.1")
    events = read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["agent"] == "claude"
    assert events[0]["provider"] == "anthropic"
    assert events[0]["model"] == "opus-4.1"


def test_trace_event_without_identity_fields_still_validates(tmp_path: Path) -> None:
    """Identity fields are optional; the existing event contract is
    unchanged for events that do not carry them.
    """
    _event(tmp_path, event_id="s1", event_type="step.started", subject="e1",
           outcome="started", ts="2026-08-12T00:00:00Z")
    events = read_events(tmp_path)
    assert len(events) == 1
    assert "agent" not in events[0]
    assert "provider" not in events[0]
    assert "model" not in events[0]


# ---------------------------------------------------------------------------
# Stability submetric shape (nested under measurement_integrity)
# ---------------------------------------------------------------------------

def test_stability_submetric_nests_under_measurement_integrity(tmp_path: Path) -> None:
    """The stability submetric is part of measurement_integrity, not a
    top-level 6th component (issue #663 preferred shape)."""
    _full_event_corpus(tmp_path)
    integrity = build_report(tmp_path)["components"]["measurement_integrity"]
    assert "stability" in integrity["submetrics"], (
        "stability submetric must be nested under measurement_integrity"
    )


def test_stability_submetric_reports_required_fields(tmp_path: Path) -> None:
    """Coverage + score-or-INSUFFICIENT_EVIDENCE + status + findings +
    evidence_event_ids must all be present and well-typed."""
    _full_event_corpus(tmp_path, identity={"agent": "claude", "provider": "anthropic", "model": "opus-4.1"})
    stability = (
        build_report(tmp_path)["components"]["measurement_integrity"]["submetrics"]["stability"]
    )
    assert isinstance(stability["coverage"], float)
    assert 0.0 <= stability["coverage"] <= 1.0
    assert stability["status"] in {"OK", "DRIFT_WARNING", "ROT", INSUFFICIENT_EVIDENCE}
    assert isinstance(stability["findings"], list)
    assert isinstance(stability["evidence_event_ids"], list)
    score = stability["score"]
    if score is not None and score != INSUFFICIENT_EVIDENCE:
        assert isinstance(score, (int, float))
        assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# Model / provider / replay determinism
# ---------------------------------------------------------------------------

def test_model_swap_does_not_change_verdict(tmp_path: Path) -> None:
    """Re-running the reducer with a different model identity yields a
    byte-identical verdict for the four shippable components. The
    stability submetric may legitimately differ (it reports coverage of
    the new identity), but the 5-component `overall_score` + the four
    shippable component scores are invariant."""
    opus_path = tmp_path / "opus"
    haiku_path = tmp_path / "haiku"
    opus_path.mkdir()
    haiku_path.mkdir()

    _full_event_corpus(opus_path, identity={"agent": "claude", "provider": "anthropic", "model": "opus-4.1"})
    _full_event_corpus(haiku_path, identity={"agent": "claude", "provider": "anthropic", "model": "haiku-4.5"})

    report_opus = build_report(opus_path)
    report_haiku = build_report(haiku_path)

    for name in ("prevention_quality", "first_pass_quality",
                 "recovery_quality", "measurement_integrity"):
        a = report_opus["components"][name]
        b = report_haiku["components"][name]
        if name == "measurement_integrity":
            assert a["score"] == b["score"], (
                f"{name} score must be invariant under model swap; "
                f"got {a['score']!r} vs {b['score']!r}"
            )
            for sub_name, sub_a in a["submetrics"].items():
                if sub_name == "stability":
                    continue
                assert sub_a == b["submetrics"][sub_name], (
                    f"measurement_integrity.{sub_name} must be model-invariant"
                )
        else:
            assert a == b, f"{name} must be byte-identical under model swap"


def test_provider_swap_does_not_change_verdict(tmp_path: Path) -> None:
    """Same as test_model_swap_does_not_change_verdict but for the
    provider field. Provider identity must not enter the verdict."""
    anthropic_path = tmp_path / "anthropic"
    openai_path = tmp_path / "openai"
    anthropic_path.mkdir()
    openai_path.mkdir()

    _full_event_corpus(anthropic_path, identity={"agent": "claude", "provider": "anthropic", "model": "opus-4.1"})
    _full_event_corpus(openai_path, identity={"agent": "claude", "provider": "openai", "model": "opus-4.1"})

    report_a = build_report(anthropic_path)
    report_b = build_report(openai_path)
    assert report_a["components"]["prevention_quality"] == report_b["components"]["prevention_quality"]
    assert report_a["components"]["first_pass_quality"] == report_b["components"]["first_pass_quality"]
    assert report_a["components"]["recovery_quality"] == report_b["components"]["recovery_quality"]


def test_replay_same_input_produces_identical_verdict(tmp_path: Path) -> None:
    """Two calls to build_report with the same TraceLog yield byte-
    identical verdicts. The reducer must be deterministic."""
    _full_event_corpus(tmp_path, identity={"agent": "claude", "provider": "anthropic", "model": "opus-4.1"})
    a = build_report(tmp_path)
    b = build_report(tmp_path)
    assert a == b


def test_event_order_invariance_under_replay(tmp_path: Path) -> None:
    """The reducer must produce the same verdict regardless of file
    write order. Two writers that append the same events in different
    orders must agree on the report."""
    _full_event_corpus(tmp_path)
    forward = build_report(tmp_path)

    path = tmp_path / ".dev-kit" / "trace" / "events.jsonl"
    path.unlink()
    reversed_order = [
        ("sc", "step.completed", "e1", "completed", "2026-08-12T00:00:01Z", "s1", {}),
        ("s1", "step.started", "e1", "started", "2026-08-12T00:00:00Z", None, {}),
        ("vp2", "verify.passed", "d1", "passed", "2026-08-12T00:00:04Z", "hh",
         {"required_checks_passed": True, "independent": True, "retry_count": 1}),
        ("hh", "heal.attempted", "d1", "attempted", "2026-08-12T00:00:03Z", "vf", {}),
        ("vf", "verify.failed", "d1", "failed", "2026-08-12T00:00:02Z", None, {}),
        ("vv", "verify.passed", "d1", "passed", "2026-08-12T00:00:01Z", "w1",
         {"required_checks_passed": True, "retry_count": 0}),
        ("w1", "write.observed", "d1", "written", "2026-08-12T00:00:00Z", None, {}),
        ("g2", "guard.allowed", "a2", "allowed", "2026-08-12T00:00:01Z", None,
         {"policy_id": "scope", "ground_truth": "unsafe", "reason": "x"}),
        ("g1", "guard.blocked", "a1", "blocked", "2026-08-12T00:00:00Z", None,
         {"policy_id": "scope", "ground_truth": "unsafe", "reason": "x"}),
    ]
    for entry in reversed_order:
        event_id, event_type, subject, outcome, ts, parent, evidence = entry
        payload = {
            "event_id": event_id, "run_id": "run-1", "workflow_id": "wf-1",
            "stage": "build", "event_type": event_type,
            "subject_id": subject, "parent_id": parent,
            "ts": ts, "outcome": outcome, "source": "test",
            "evidence_ref": evidence,
        }
        append_event(tmp_path, payload)
    reversed_report = build_report(tmp_path)

    for name in ("prevention_quality", "first_pass_quality",
                 "recovery_quality"):
        assert forward["components"][name]["score"] == reversed_report["components"][name]["score"], (
            f"{name} must be replay-invariant"
        )


# ---------------------------------------------------------------------------
# Missing evidence is INSUFFICIENT_EVIDENCE, never zero
# ---------------------------------------------------------------------------

def test_missing_identity_evidence_emits_insufficient_evidence(tmp_path: Path) -> None:
    """When no events carry agent/provider/model identity, the stability
    submetric's status is INSUFFICIENT_EVIDENCE and its score is the
    INSUFFICIENT_EVIDENCE sentinel (not 0.0)."""
    _full_event_corpus(tmp_path)
    stability = (
        build_report(tmp_path)["components"]["measurement_integrity"]["submetrics"]["stability"]
    )
    assert stability["status"] == INSUFFICIENT_EVIDENCE
    assert stability["score"] is None or stability["score"] == INSUFFICIENT_EVIDENCE
    assert stability["score"] != 0.0
    assert any("identity" in finding.lower() or "agent" in finding.lower()
               or "provider" in finding.lower() or "model" in finding.lower()
               for finding in stability["findings"]), (
        "missing-identity case must emit at least one finding"
    )


def test_insufficient_evidence_constant_is_distinct_from_zero() -> None:
    """The INSUFFICIENT_EVIDENCE sentinel must not be 0.0 or 0; it is a
    string that downstream status logic compares against."""
    assert INSUFFICIENT_EVIDENCE != 0.0
    assert INSUFFICIENT_EVIDENCE != 0
    assert isinstance(INSUFFICIENT_EVIDENCE, str)


def test_overall_score_remains_number_when_only_stability_missing(tmp_path: Path) -> None:
    """A missing stability submetric must NOT collapse the 5-component
    overall_score to None; the four shippable components still produce
    a meaningful score."""
    _full_event_corpus(tmp_path)
    report = build_report(tmp_path)
    assert report["overall_score"] is not None
    stability = report["components"]["measurement_integrity"]["submetrics"]["stability"]
    assert stability["status"] == INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Regression tests pinning the 2 review-major findings
# ---------------------------------------------------------------------------

def test_gate_portability_evidence_event_ids_contains_real_event_ids(tmp_path):
    """Regression: gate_portability.evidence_event_ids must be real event IDs,
    not event_type strings (PR #677 review major #1)."""
    _full_event_corpus(tmp_path, identity={'agent': 'claude', 'provider': 'anthropic', 'model': 'opus-4.1'})
    stability = (
        build_report(tmp_path)['components']['measurement_integrity']['submetrics']['stability']
    )
    gate_port = stability['submetrics']['gate_portability']
    events = read_events(tmp_path)
    real_event_ids = {e['event_id'] for e in events}
    event_types_in_corpus = {e['event_type'] for e in events}
    evidence = gate_port['evidence_event_ids']
    for eid in evidence:
        assert eid in real_event_ids, (
            f'gate_portability.evidence_event_ids entry {eid!r} is not a real event_id'
        )
    for et in event_types_in_corpus:
        assert et not in evidence, (
            f'gate_portability.evidence_event_ids leaked event_type {et!r}; '
            'must be event IDs, not event_type strings'
        )

def test_replay_compatibility_evidence_event_ids_match_events_with_ts(tmp_path):
    """Regression: replay_compatibility.evidence_event_ids must list the events
    whose ts contributed to the monotonic-timestamp check (PR #677 review minor #3)."""
    _full_event_corpus(tmp_path, identity={'agent': 'claude', 'provider': 'anthropic', 'model': 'opus-4.1'})
    stability = (
        build_report(tmp_path)['components']['measurement_integrity']['submetrics']['stability']
    )
    replay = stability['submetrics']['replay_compatibility']
    events = read_events(tmp_path)
    expected_ids = sorted({e['event_id'] for e in events if e.get('ts') is not None})
    assert replay['evidence_event_ids'] == expected_ids, (
        f'replay_compatibility.evidence_event_ids must list the events with ts; '
        f"got {replay['evidence_event_ids']!r}, expected {expected_ids!r}"
    )

def test_stability_submetric_top_level_shape_uses_submetric_helper(tmp_path):
    """The stability submetric uses _submetric(): same field set as _component()
    minus weight (PR #677 review minor #6)."""
    _full_event_corpus(tmp_path, identity={'agent': 'claude', 'provider': 'anthropic', 'model': 'opus-4.1'})
    stability = (
        build_report(tmp_path)['components']['measurement_integrity']['submetrics']['stability']
    )
    expected = {
        'coverage', 'score', 'status', 'submetrics', 'findings',
        'evidence_event_ids', 'config_version',
    }
    assert set(stability.keys()) == expected, (
        f'stability submetric must use _submetric() shape; got keys {set(stability.keys())}'
    )
    assert 'weight' not in stability, 'stability submetric must NOT carry a weight field'

# ---------------------------------------------------------------------------
# Backward compatibility for the 5-component contract
# ---------------------------------------------------------------------------

def test_compact_weights_still_sum_to_one() -> None:
    """The 5-component contract is preserved: weights sum to 1.0, no
    new top-level weight is added for stability."""
    assert sum(COMPONENT_WEIGHTS.values()) == 1.0
    assert set(COMPONENT_WEIGHTS.keys()) == {
        "prevention_quality", "first_pass_quality",
        "recovery_quality", "learning_quality", "measurement_integrity",
    }


def test_schema_version_is_bumped_to_advertise_stability(tmp_path: Path) -> None:
    """build_report bumps schema_version so consumer code can detect the
    new stability evidence and opt in. Existing 5-component consumers
    continue to work because the top-level shape (components /
    overall_score / status / event_count / contract_version) is intact."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        report = build_report(path)
        assert report["schema_version"] > 1
        for key in ("components", "overall_score", "status", "event_count",
                    "contract_version"):
            assert key in report


def test_event_schema_version_unchanged() -> None:
    """The TraceLog event schema_version stays at 1; identity fields are
    additive and validated by the same contract."""
    assert EVENT_SCHEMA_VERSION == 1


# --- Issue #702: schema_version bump + submetric nesting pin -----------

def test_schema_version_is_three_after_subject_observability(tmp_path: Path) -> None:
    """Issue #702: schema_version bumped 2 -> 3 to advertise the nested
    subject_observability submetric. Top-level shape is unchanged."""
    from lib.harness_effectiveness import build_report
    report = build_report(tmp_path)
    assert report["schema_version"] == 3
    for key in ("components", "overall_score", "status", "event_count",
                "contract_version"):
        assert key in report


def test_compact_weights_still_sum_to_one_after_subject_observability(tmp_path: Path) -> None:
    """Issue #702: subject_observability is a nested submetric, not a
    6th top-level component. Weights still sum to 1.0 and the key set
    is unchanged (matches the issue #663 precedent)."""
    from lib.harness_effectiveness import COMPONENT_WEIGHTS, build_report
    assert sum(COMPONENT_WEIGHTS.values()) == 1.0
    assert set(COMPONENT_WEIGHTS.keys()) == {
        "prevention_quality", "first_pass_quality",
        "recovery_quality", "learning_quality", "measurement_integrity",
    }
    report = build_report(tmp_path)
    assert "subject_observability" in (
        report["components"]["measurement_integrity"]["submetrics"]
    )
    # Nested submetric has no weight, just like stability (issue #663).
    sub = (report["components"]["measurement_integrity"]
           ["submetrics"]["subject_observability"])
    assert "weight" not in sub
