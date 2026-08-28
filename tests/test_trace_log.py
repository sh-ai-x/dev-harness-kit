"""test_trace_log.py — TraceLog dataclass + JSON serialization.

Covers:
- to_dict / load round-trip
- schema_version mismatch raises
- save() creates intermediate directories and writes JSON
- unknown step fields go into `extra`
- default values are stable across versions
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.trace_log import SCHEMA_VERSION, TraceLog, TraceStep, now_utc


def test_now_utc_is_iso8601() -> None:
    """now_utc() returns an ISO-8601 string ending in 'Z'."""
    s = now_utc()
    assert s.endswith("Z")
    assert "T" in s
    assert len(s) == 27  # YYYY-MM-DDTHH:MM:SS.ffffffZ


def test_trace_step_defaults() -> None:
    """TraceStep requires only `ts`, `skill`, `phase`; everything else defaults."""
    step = TraceStep(ts="2026-07-31T00:00:00Z", skill="plan", phase="interview")
    assert step.model is None
    assert step.prompt_hash is None
    assert step.input_tokens == 0
    assert step.output_tokens == 0
    assert step.latency_ms == 0
    assert step.retries == 0
    assert step.exit_code == 0
    assert step.extra == {}


def test_trace_log_to_dict_shape() -> None:
    """to_dict() returns all 11 top-level keys with the steps as list of dicts."""
    log = TraceLog(
        case_id="fix-bug",
        started_at="2026-07-31T00:00:00Z",
        ended_at="2026-07-31T00:01:00Z",
        harness_version="0.3.175",
        agent="claude-code-4.8",
        worktree_branch="fix/bug",
        worktree_path=".worktrees/fix-bug",
    )
    d = log.to_dict()
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["case_id"] == "fix-bug"
    assert d["steps"] == []
    assert d["judge_scores"] == []
    assert d["evidence"] == {}
    # All 11 keys present
    expected_keys = {
        "schema_version", "case_id", "started_at", "ended_at",
        "harness_version", "agent", "worktree_branch", "worktree_path",
        "steps", "judge_scores", "evidence",
    }
    assert expected_keys.issubset(set(d.keys()))


def test_save_creates_directories_and_writes_json(tmp_path: Path) -> None:
    """save() should mkdir -p and write a parseable JSON file."""
    log = TraceLog(case_id="my-case")
    out = log.save(tmp_path)
    assert out.exists()
    assert out.parent == tmp_path / "eval" / "transcripts" / "my-case"
    # Round-trip via raw JSON
    raw = json.loads(out.read_text())
    assert raw["case_id"] == "my-case"
    assert raw["schema_version"] == SCHEMA_VERSION


def test_load_round_trip(tmp_path: Path) -> None:
    """save() then load() should produce an equivalent TraceLog."""
    log = TraceLog(
        case_id="round-trip",
        started_at="2026-07-31T00:00:00Z",
        ended_at="2026-07-31T00:05:00Z",
        harness_version="0.3.175",
        agent="claude-code-4.8",
        worktree_branch="feat/x",
        worktree_path=".worktrees/feat-x",
        steps=[
            TraceStep(
                ts="2026-07-31T00:00:01Z",
                skill="plan", phase="interview",
                model="claude-opus-4-8",
                prompt_hash="abc123",
                input_tokens=100, output_tokens=50,
                latency_ms=1234, retries=0, exit_code=0,
            ),
            TraceStep(
                ts="2026-07-31T00:00:02Z",
                skill="build", phase="tdd-red",
                latency_ms=500, exit_code=0,
                extra={"test_file": "tests/test_x.py"},
            ),
        ],
        judge_scores=[{"rubric": "agent-behavior", "axes": {"D1_outcome": 5}}],
        evidence={"D1_outcome": {"tests": "passed: 47/47"}},
    )
    out = log.save(tmp_path)
    loaded = TraceLog.load(out)

    assert loaded.case_id == "round-trip"
    assert loaded.harness_version == "0.3.175"
    assert len(loaded.steps) == 2
    assert loaded.steps[0].model == "claude-opus-4-8"
    assert loaded.steps[1].extra == {"test_file": "tests/test_x.py"}
    assert loaded.judge_scores == [{"rubric": "agent-behavior", "axes": {"D1_outcome": 5}}]
    assert loaded.evidence == {"D1_outcome": {"tests": "passed: 47/47"}}


def test_load_rejects_wrong_schema_version(tmp_path: Path) -> None:
    """load() should refuse files whose schema_version differs from SCHEMA_VERSION."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 99, "case_id": "x"}))
    with pytest.raises(ValueError, match="schema_version"):
        TraceLog.load(bad)


def test_load_handles_unknown_step_fields(tmp_path: Path) -> None:
    """Future versions may add fields; load() should put them in `extra`."""
    future = tmp_path / "future.json"
    future.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "case_id": "future",
        "steps": [{
            "ts": "2026-07-31T00:00:00Z",
            "skill": "plan",
            "phase": "interview",
            "future_field": "kept-as-extra",
        }],
    }))
    loaded = TraceLog.load(future)
    assert loaded.steps[0].extra == {"future_field": "kept-as-extra"}
    assert loaded.steps[0].skill == "plan"


# ---------------------------------------------------------------------------
# issue #663 — identity auto-stamp + dedupe-on-write guard
# ---------------------------------------------------------------------------

def test_default_identity_shape_is_dict_of_str() -> None:
    """_default_identity() returns the three string fields the stability
    submetric reads. Every field defaults to empty string so callers can
    opt in via DEV_KIT_AGENT / DEV_KIT_PROVIDER / DEV_KIT_MODEL without
    changing the schema. The empty-default prevents a non-Claude runner
    from being silently mis-attributed to claude-code (A06-1)."""
    from lib.trace_log import _default_identity

    identity = _default_identity()
    assert isinstance(identity, dict)
    assert set(identity.keys()) == {"agent", "provider", "model"}
    for key, value in identity.items():
        assert isinstance(value, str), f"{key} must be a string, got {type(value)!r}"
    assert identity["agent"] == "", (
        "agent must default to empty (no auto-attribution to claude-code); "
        "the stability reducer treats empty/missing as 'no identity recorded'"
    )


def test_default_identity_respects_env_overrides(monkeypatch) -> None:
    """Setting DEV_KIT_AGENT / DEV_KIT_PROVIDER / DEV_KIT_MODEL overrides the
    defaults so the reducer can attribute events to a specific producer."""
    from lib.trace_log import _default_identity

    monkeypatch.setenv("DEV_KIT_AGENT", "codex")
    monkeypatch.setenv("DEV_KIT_PROVIDER", "openai")
    monkeypatch.setenv("DEV_KIT_MODEL", "gpt-5")
    identity = _default_identity()
    assert identity == {"agent": "codex", "provider": "openai", "model": "gpt-5"}


def test_append_event_auto_stamps_identity(tmp_path: Path) -> None:
    """append_event fills missing agent/provider/model from env defaults.

    With no env override, every field defaults to empty string — the
    stability reducer treats empty as 'no identity recorded' so a
    non-Claude runner is NOT silently mis-attributed to claude-code.
    A previous revision asserted ``stored["agent"] == "claude-code"``;
    that made the metric self-justifying on every non-Claude producer
    (A06-1 / insecure-design).
    """
    from lib.trace_log import append_event, read_events

    event = {
        "event_id": "evt-1",
        "run_id": "run-1",
        "workflow_id": "wf-1",
        "stage": "build",
        "event_type": "step.started",
        "subject_id": "subj",
        "parent_id": None,
        "ts": now_utc(),
        "outcome": "started",
        "source": "test",
        "evidence_ref": {},
    }
    append_event(tmp_path, event)
    [stored] = read_events(tmp_path)
    assert stored["agent"] == ""
    assert stored["provider"] == ""
    assert stored["model"] == ""


def test_append_event_preserves_explicit_identity(tmp_path: Path) -> None:
    """Caller-supplied agent/provider/model must win over the env default."""
    from lib.trace_log import append_event, read_events

    event = {
        "event_id": "evt-1",
        "run_id": "run-1",
        "workflow_id": "wf-1",
        "stage": "build",
        "event_type": "step.started",
        "subject_id": "subj",
        "parent_id": None,
        "ts": now_utc(),
        "outcome": "started",
        "source": "test",
        "evidence_ref": {},
        "agent": "codex",
        "provider": "openai",
        "model": "gpt-5",
    }
    append_event(tmp_path, event)
    [stored] = read_events(tmp_path)
    assert stored["agent"] == "codex"
    assert stored["provider"] == "openai"
    assert stored["model"] == "gpt-5"


def test_append_event_dedupe_on_write_collision(tmp_path: Path) -> None:
    """Two consecutive append_event() calls with the same hardcoded event_id
    must produce two distinct stored event_ids — the dedupe-on-write guard
    in append_event regenerates the second one. The hot path stays O(1) on
    a healthy file (only the last 64 lines are scanned)."""
    from lib.trace_log import append_event, read_events

    base = {
        "run_id": "run-1",
        "workflow_id": "wf-1",
        "stage": "build",
        "event_type": "step.started",
        "subject_id": "subj",
        "parent_id": None,
        "ts": now_utc(),
        "outcome": "started",
        "source": "test",
        "evidence_ref": {},
    }
    event_a = dict(base, event_id="collision-uuid")
    event_b = dict(base, event_id="collision-uuid")
    append_event(tmp_path, event_a)
    append_event(tmp_path, event_b)

    stored = read_events(tmp_path)
    assert len(stored) == 2
    assert stored[0]["event_id"] == "collision-uuid"
    # Second must differ — the dedupe guard regenerated it.
    assert stored[1]["event_id"] != "collision-uuid"
    assert stored[0]["event_id"] != stored[1]["event_id"]
