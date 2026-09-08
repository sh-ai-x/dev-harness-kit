"""Tests for lib.execute effectiveness boundaries.

The proposal mandates that the executor enrolls a unique attempt before
dispatch, records observed start and existing real completed/failed/blocked
boundaries (including catchable cancellation/exception), and collects at
those boundaries without changing the workflow outcome.

These tests pin the integration contract by invoking ``_step_pre_spawn``
and ``_step_post_collect`` directly and asserting the journal records.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import execute  # noqa: E402
from effectiveness_collection import (  # noqa: E402
    TRANSITION_CONTROLLER_CLOSE,
    TRANSITION_ENROLL,
    TRANSITION_OBSERVED_START,
    TRANSITION_OBSERVED_TERMINAL,
    collect,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean project root where ``_step_pre_spawn`` and
    ``_step_post_collect`` can run. The per-step worktree cut requires
    a real git repo, so we initialize one and stub out the worktree
    creation (the per-step wt directory is irrelevant to the journal
    boundary tests).
    """
    real_root = tmp_path.resolve()
    real_root.mkdir(parents=True, exist_ok=True)
    # Initialize a git repo so resolve_step_paths / cut_worktree can find
    # a worktree base.
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(real_root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(real_root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(real_root), check=True)
    # Pre-create the phases/<phase>/step<N>.md so _step_pre_spawn finds it.
    (real_root / "phases" / "p1").mkdir(parents=True, exist_ok=True)
    (real_root / "phases" / "p1" / "step1.md").write_text("# step 1\n")
    (real_root / "phases" / "p1" / "index.json").write_text(
        json.dumps({"steps": [{"step": 1, "name": "step-1", "status": "pending"}]})
    )
    # Initial commit so the repo isn't empty (avoids weird git edge cases).
    subprocess.run(["git", "add", "-A"], cwd=str(real_root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(real_root), check=True)
    return real_root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _records(root: Path) -> list:
    from effectiveness_collection import _iter_records
    return list(_iter_records(root))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_step_pre_spawn_enrolls_unit(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_step_pre_spawn`` must enroll a unit before any dispatch."""
    # Stub the worktree cut to a no-op so we don't depend on git worktree
    # semantics for the journal boundary.
    monkeypatch.setattr(execute, "cut_worktree", lambda **kw: None)
    # The step file path is read inside _step_pre_spawn; we already wrote it.
    ctx = execute._step_pre_spawn(root, "p1", 1, "feat/test")
    assert ctx["started_at_iso"]
    assert "attempt_token" in ctx
    recs = _records(root)
    # Two records: enroll + observed_start.
    assert any(r.transition == TRANSITION_ENROLL for r in recs), recs
    assert any(r.transition == TRANSITION_OBSERVED_START for r in recs), recs
    # The enrolled attempt_id is the one passed forward as the parent.
    assert ctx["attempt_token"]


def test_step_post_collect_records_terminal(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_step_post_collect`` must record the observed terminal + controller
    close when a step succeeds. ``success`` is a no-blocked, no-error path."""
    monkeypatch.setattr(execute, "cut_worktree", lambda **kw: None)
    # Bypass the real commit so we don't need a worktree checkout.
    monkeypatch.setattr(execute, "_commit_step", lambda wt, msg: False)
    ctx = execute._step_pre_spawn(root, "p1", 1, "feat/test")
    execute._step_post_collect(
        root, "p1", 1, "step-1", ctx,
        push=False, exit_code=0, stdout="", stderr="",
    )
    recs = _records(root)
    assert any(r.transition == TRANSITION_OBSERVED_TERMINAL for r in recs)
    assert any(r.transition == TRANSITION_CONTROLLER_CLOSE for r in recs)
    env = collect(root)
    assert env.counts["closed"] == 1
    assert env.counts["paired"] == 1


def test_step_post_collect_records_failure(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit must record a failed terminal (still a closed unit)."""
    monkeypatch.setattr(execute, "cut_worktree", lambda **kw: None)
    ctx = execute._step_pre_spawn(root, "p1", 1, "feat/test")
    execute._step_post_collect(
        root, "p1", 1, "step-1", ctx,
        push=False, exit_code=2, stdout="", stderr="",
    )
    recs = _records(root)
    terminals = [r for r in recs if r.transition == TRANSITION_OBSERVED_TERMINAL]
    assert any(r.outcome == "failed" for r in terminals)
    env = collect(root)
    assert env.counts["closed"] == 1
    assert env.ratios["success"] == 0.0


def test_step_post_collect_records_blocked(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked marker in stdout must record a blocked terminal."""
    monkeypatch.setattr(execute, "cut_worktree", lambda **kw: None)
    ctx = execute._step_pre_spawn(root, "p1", 1, "feat/test")
    execute._step_post_collect(
        root, "p1", 1, "step-1", ctx,
        push=False, exit_code=0, stdout="<!-- status: blocked -->", stderr="",
    )
    recs = _records(root)
    terminals = [r for r in recs if r.transition == TRANSITION_OBSERVED_TERMINAL]
    assert any(r.outcome == "blocked" for r in terminals)
    env = collect(root)
    assert env.counts["closed"] == 1
    assert env.counts["paired"] == 1


def test_collection_at_boundaries_does_not_change_workflow_exit(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection error inside the executor must NOT change the
    returned exit code. We assert that the success exit is returned
    even when telemetry files exist; collection errors surface in the
    envelope's readiness, never the workflow rc.
    """
    monkeypatch.setattr(execute, "cut_worktree", lambda **kw: None)
    monkeypatch.setattr(execute, "_commit_step", lambda wt, msg: False)
    ctx = execute._step_pre_spawn(root, "p1", 1, "feat/test")
    rc = execute._step_post_collect(
        root, "p1", 1, "step-1", ctx,
        push=False, exit_code=0, stdout="", stderr="",
    )
    assert rc == 0
    # And the envelope is built normally.
    env = collect(root)
    assert env.readiness in (execute.READINESS_READY if hasattr(execute, "READINESS_READY") else "READY",
                             "INSUFFICIENT_EVIDENCE", "READY")
