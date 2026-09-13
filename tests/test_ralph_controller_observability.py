from __future__ import annotations

import json
import os
from pathlib import Path

from lib.trace_log import read_events
from skills.ralph.lib import ralph_chain as chain
from skills.ralph.lib import ralph_state as state_module
from skills.ralph.lib.ralph_events import RalphEventAdapter
from skills.ralph.lib.ralph_metrics import reduce_metrics


def _attended(session: str = "controller") -> state_module.RalphState:
    state = state_module.new_state("controller test", session=session)
    for stage in (
        state_module.PROPOSAL_GATE,
        state_module.PLAN_GATE,
        state_module.SHIP_CONFIRM_GATE,
        state_module.ATTENDED_RUN,
    ):
        state.transition(stage, action=f"approved {stage}")
    return state


def test_controller_writes_valid_canonical_events_and_failure_sidecar(tmp_path: Path) -> None:
    state = _attended()
    dispatch = chain.RecordingDispatch(
        results={
            chain.BUILD: chain.DispatchResult(exit_code=2, stderr="MUST-37 cycle"),
        }
    )

    final = chain.run_attended(state, dispatch, project_root=tmp_path)

    assert final.current_stage == state_module.RECOVERY_REQUIRED
    events = read_events(tmp_path)
    assert events
    assert all(event["workflow_id"] == "ralph" for event in events)
    assert any(event["event_type"] == "stage.attempt.started" for event in events)
    assert any(event["event_type"] == "stage.attempt.finished" for event in events)
    assert (tmp_path / final.failure_log_path).is_file()
    report = reduce_metrics(events)
    assert report["metrics"]["stage_success_rate"]["value"] == 0.0
    assert report["metrics"]["stage_success_rate"]["status"] == "FAILED"


def test_resume_skips_completed_stage_and_keeps_attended_lock(tmp_path: Path) -> None:
    state = _attended("resume")
    failed = chain.RecordingDispatch(
        results={chain.BUILD: chain.DispatchResult(exit_code=1, stderr="retryable")}
    )
    assert chain.run_attended(state, failed, project_root=tmp_path).current_stage == state_module.RECOVERY_REQUIRED

    resumed = chain.RecordingDispatch(
        results={
            chain.BABYSIT: chain.DispatchResult(exit_code=0),
            chain.SHIP: chain.DispatchResult(exit_code=0, terminal="USER_MERGE_REQUIRED"),
        }
    )
    # The failed BUILD was not marked completed, so resume retries it. A
    # successful retry then advances to BABYSIT and SHIP in the same call.
    resumed.results[chain.BUILD] = chain.DispatchResult(exit_code=0)
    final = chain.run_attended(state, resumed, project_root=tmp_path)

    assert final.current_stage == state_module.USER_MERGE_REQUIRED
    assert final.attended_lock is True
    assert [call["sub_stage"] for call in resumed.calls] == [
        chain.BUILD, chain.BABYSIT, chain.SHIP,
    ]


def test_context_adapter_remains_idempotent_under_controller_journal(tmp_path: Path) -> None:
    adapter = RalphEventAdapter(tmp_path, run_id="run", attempt_id="attempt")
    first = adapter.emit("BUILD", "stage.started", "started", idempotency_key="same")
    second = adapter.emit("BUILD", "stage.started", "started", idempotency_key="same")
    assert first.persisted and second.duplicate
    assert len(read_events(tmp_path)) == 1


def test_live_lease_blocks_and_dead_lease_is_reclaimed(tmp_path: Path) -> None:
    state = _attended("lease")
    lease = tmp_path / ".dev-kit" / "ralph" / "lease.lease"
    lease.parent.mkdir(parents=True)
    lease.write_text(json.dumps({"pid": os.getpid()}))
    try:
        chain.run_attended(state, chain.RecordingDispatch(), project_root=tmp_path)
    except chain.ChainError as exc:
        assert "lease" in str(exc)
    else:
        raise AssertionError("a live lease must block a second controller")

    lease.write_text(json.dumps({"pid": 99999999}))
    final = chain.run_attended(
        state,
        chain.RecordingDispatch(
            results={chain.BUILD: chain.DispatchResult(exit_code=1, stderr="stop")}
        ),
        project_root=tmp_path,
    )
    assert final.current_stage == state_module.RECOVERY_REQUIRED
