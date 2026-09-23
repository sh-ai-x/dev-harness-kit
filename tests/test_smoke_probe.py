"""Regression tests for lib.smoke_probe — auto-populate first_pass_quality evidence.

The probe runs on session start (wired in hooks/session-start-check.sh)
and emits one write.observed → verify.passed chain per session so
lib.harness_effectiveness._first_pass scores something even in worktrees
that have never been built via /dev-kit:build.

Three things must hold:

1. The full chain is emitted (step.started, write.observed, verify.passed,
   step.completed) with correct parent_id linkage so the reducer's
   `_first_pass` accepts the chain via `causally_linked = first.parent_id
   == write.event_id`.
2. The probe file is created and then deleted — no residual state.
3. A pytest failure emits verify.failed (not verify.passed), so
   recovery_quality also gets evidence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_events(root: Path) -> list:
    path = root / ".dev-kit" / "trace" / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_smoke_probe_emits_full_pass_chain(tmp_path: Path, monkeypatch) -> None:
    """Happy path: pytest passes → step.started → write.observed → verify.passed → step.completed."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT))
    import lib.smoke_probe as sp
    # Stub the subprocess pytest so the test doesn't depend on a real
    # ``tests/`` tree under tmp_path; the smoke probe's parent linkage
    # contract is independent of pytest's actual exit code.
    monkeypatch.setattr(sp, "_run_pytest", lambda root: (True, "1 passed"))
    try:
        sp.run(tmp_path, session_id="test-session-pass")
    finally:
        sys.path.remove(str(REPO_ROOT))

    events = _read_events(tmp_path)
    smoke_events = [e for e in events if e.get("subject_id") == "smoke:test-session-pass"]
    types = [e["event_type"] for e in smoke_events]
    assert "step.started" in types, f"missing step.started in {types}"
    assert "write.observed" in types, f"missing write.observed in {types}"
    assert "verify.passed" in types, f"missing verify.passed in {types}"
    assert "step.completed" in types, f"missing step.completed in {types}"

    # Parent linkage: write.observed's parent_id should be step.started's id
    started_id = next(e["event_id"] for e in smoke_events if e["event_type"] == "step.started")
    write_id = next(e["event_id"] for e in smoke_events if e["event_type"] == "write.observed")
    completed_id = next(e["event_id"] for e in smoke_events if e["event_type"] == "step.completed")

    assert next(e for e in smoke_events if e["event_type"] == "write.observed")["parent_id"] == started_id
    assert next(e for e in smoke_events if e["event_type"] == "verify.passed")["parent_id"] == write_id
    assert next(e for e in smoke_events if e["event_type"] == "step.completed")["parent_id"] == started_id

    # Honesty contract: required_checks_passed=True, independent=True
    verify = next(e for e in smoke_events if e["event_type"] == "verify.passed")
    assert verify["evidence_ref"].get("required_checks_passed") is True
    assert verify["evidence_ref"].get("independent") is True
    assert verify["evidence_ref"].get("retry_count") == 0

    # Probe file should be deleted (cleaned up).
    probe_files = list((tmp_path / ".dev-kit" / "trace" / "measurement").glob("smoke-probe-*.txt"))
    assert not probe_files, f"probe file not cleaned up: {probe_files}"

    # Sanity: completed_id is just an opaque id — we don't assert on it.
    assert completed_id  # noqa: F841


def test_smoke_probe_emits_verify_failed_on_pytest_failure(tmp_path: Path, monkeypatch) -> None:
    """When pytest fails, verify.failed is emitted (not verify.passed)."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT))

    # Monkeypatch _run_pytest to force a failure without running subprocess.
    import lib.smoke_probe as sp
    monkeypatch.setattr(sp, "_run_pytest", lambda root: (False, "1 failed"))

    try:
        sp.run(tmp_path, session_id="test-session-fail")
    finally:
        sys.path.remove(str(REPO_ROOT))

    events = _read_events(tmp_path)
    smoke_events = [e for e in events if e.get("subject_id") == "smoke:test-session-fail"]
    types = [e["event_type"] for e in smoke_events]
    assert "verify.failed" in types, f"missing verify.failed in {types}"
    assert "verify.passed" not in types, "verify.passed must NOT be emitted on pytest failure"

    failed = next(e for e in smoke_events if e["event_type"] == "verify.failed")
    assert failed["evidence_ref"].get("required_checks_passed") is False
    assert failed["evidence_ref"].get("retry_count") == 1


def test_smoke_probe_is_noop_without_session_id(tmp_path: Path, monkeypatch) -> None:
    """Empty session_id is a no-op; no events emitted."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT))
    import lib.smoke_probe as sp
    monkeypatch.setattr(sp, "_run_pytest", lambda root: (True, "1 passed"))
    try:
        sp.run(tmp_path, session_id="")
    finally:
        sys.path.remove(str(REPO_ROOT))

    events = _read_events(tmp_path)
    assert events == [], f"no-op run leaked events: {events}"


def test_smoke_probe_uses_smoke_subject_prefix(tmp_path: Path, monkeypatch) -> None:
    """Probe events use subject_id=smoke:<session_id>, NOT session:<uuid>."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT))
    import lib.smoke_probe as sp
    monkeypatch.setattr(sp, "_run_pytest", lambda root: (True, "1 passed"))
    try:
        sp.run(tmp_path, session_id="abc-123")
    finally:
        sys.path.remove(str(REPO_ROOT))

    events = _read_events(tmp_path)
    smoke_subjects = {e["subject_id"] for e in events}
    assert "smoke:abc-123" in smoke_subjects
    # No subject should collide with session-lifecycle events.
    assert "session:abc-123" not in smoke_subjects
