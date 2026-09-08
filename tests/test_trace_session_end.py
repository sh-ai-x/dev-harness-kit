"""Tests for hooks/trace-session-end.sh — Stop vs SessionEnd semantics.

The proposal mandates:
* Stop only requests collect(); it never closes a session.
* A genuine SessionEnd records the controller close + observed terminal.
* Multiple Stops in a row must not produce multiple closures.
* Concurrent sessions must not infer closure from one another.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from effectiveness_collection import (  # noqa: E402
    TRANSITION_CONTROLLER_CLOSE,
    TRANSITION_CONTROLLER_FINAL,
    TRANSITION_ENROLL,
    TRANSITION_OBSERVED_START,
    TRANSITION_OBSERVED_TERMINAL,
    collect,
    enroll,
    measurement_dir,
    observe,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def worktree_with_lib(tmp_path: Path) -> Path:
    """A tmp directory with the lib/ package on PYTHONPATH and a fake
    ``.dev-kit/trace/measurement`` already initialised by the SessionStart
    flow (so the hook can find its journal root)."""
    real = tmp_path.resolve()
    real.mkdir(parents=True, exist_ok=True)
    return real


def _run_hook(*, cwd: Path, payload: dict) -> subprocess.CompletedProcess:
    """Invoke hooks/trace-session-end.sh with the given stdin payload."""
    hook = ROOT / "hooks" / "trace-session-end.sh"
    return subprocess.run(
        [str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
            "PYTHONPATH": str(ROOT / "lib"),
        },
        timeout=20,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_stop_event_only_requests_collect(worktree_with_lib: Path) -> None:
    """A Stop event must NOT close a unit. It only triggers a collect()."""
    root = worktree_with_lib
    # SessionStart already enrolled + observed start.
    enroll(root, run_id="s1", workflow_id="session-lifecycle", subject_id="session:s1", attempt_id="a1")
    observe(root, run_id="s1", workflow_id="session-lifecycle", subject_id="session:s1",
            attempt_id="a1", transition=TRANSITION_OBSERVED_START, outcome="started")
    res = _run_hook(cwd=root, payload={"session_id": "s1", "cwd": str(root), "hook_event_name": "Stop"})
    assert res.returncode == 0, res.stderr
    from effectiveness_collection import _iter_records
    recs = list(_iter_records(root))
    # No controller_close / controller_final emitted by Stop.
    assert not any(r.transition == TRANSITION_CONTROLLER_CLOSE for r in recs)
    assert not any(r.transition == TRANSITION_CONTROLLER_FINAL for r in recs)
    # Unit is still unresolved.
    env = collect(root)
    assert env.counts["closed"] == 0


def test_session_end_closes_unit(worktree_with_lib: Path) -> None:
    """A genuine SessionEnd must record controller_close + observed_terminal."""
    root = worktree_with_lib
    enroll(root, run_id="s1", workflow_id="session-lifecycle", subject_id="session:s1", attempt_id="a1")
    observe(root, run_id="s1", workflow_id="session-lifecycle", subject_id="session:s1",
            attempt_id="a1", transition=TRANSITION_OBSERVED_START, outcome="started")
    res = _run_hook(cwd=root, payload={"session_id": "s1", "cwd": str(root), "hook_event_name": "SessionEnd"})
    assert res.returncode == 0, res.stderr
    from effectiveness_collection import _iter_records
    recs = list(_iter_records(root))
    assert any(r.transition == TRANSITION_CONTROLLER_CLOSE for r in recs)
    assert any(r.transition == TRANSITION_OBSERVED_TERMINAL for r in recs)
    env = collect(root)
    assert env.counts["closed"] == 1
    assert env.counts["paired"] == 1


def test_multiple_stops_do_not_close(worktree_with_lib: Path) -> None:
    """Three Stop events in a row must leave the unit unresolved."""
    root = worktree_with_lib
    enroll(root, run_id="s1", workflow_id="session-lifecycle", subject_id="session:s1", attempt_id="a1")
    observe(root, run_id="s1", workflow_id="session-lifecycle", subject_id="session:s1",
            attempt_id="a1", transition=TRANSITION_OBSERVED_START, outcome="started")
    for _ in range(3):
        res = _run_hook(cwd=root, payload={"session_id": "s1", "cwd": str(root), "hook_event_name": "Stop"})
        assert res.returncode == 0, res.stderr
    from effectiveness_collection import _iter_records
    recs = list(_iter_records(root))
    closes = [r for r in recs if r.transition == TRANSITION_CONTROLLER_CLOSE]
    assert len(closes) == 0
    env = collect(root)
    assert env.counts["closed"] == 0


def test_concurrent_sessions_do_not_infer_closure(worktree_with_lib: Path) -> None:
    """A Stop for one session must not close the other session's unit."""
    root = worktree_with_lib
    # Enroll two sessions using the same attempt_id the hook uses
    # (sess-<session_id>) so the hook's own enroll is a no-op for the
    # matching (subject, attempt) tuple.
    for sid in ("s1", "s2"):
        enroll(root, run_id=f"session:{sid}", workflow_id="session-lifecycle",
               subject_id=f"session:{sid}", attempt_id=f"sess-{sid}")
        observe(root, run_id=f"session:{sid}", workflow_id="session-lifecycle",
                subject_id=f"session:{sid}", attempt_id=f"sess-{sid}",
                transition=TRANSITION_OBSERVED_START, outcome="started")
    # Stop on s1 must not touch s2.
    res = _run_hook(cwd=root, payload={"session_id": "s1", "cwd": str(root), "hook_event_name": "Stop"})
    assert res.returncode == 0, res.stderr
    env = collect(root)
    assert env.counts["closed"] == 0
    # SessionEnd on s1 closes s1; s2 stays open.
    res = _run_hook(cwd=root, payload={"session_id": "s1", "cwd": str(root), "hook_event_name": "SessionEnd"})
    assert res.returncode == 0, res.stderr
    env = collect(root)
    assert env.counts["closed"] == 1
    assert env.counts["unresolved"] == 1
