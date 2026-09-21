"""Tests for the thin RALPH worker/session boundary adapter."""

# ruff: noqa: I001  # isort's first-party detection breaks under pre-commit's
# staged-checkout (skills.ralph.lib gets mis-classified as third-party). The
# import order below is correct in the project root where both lib/ and
# skills/ are first-party siblings.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from lib import ralph_controller
from skills.ralph.lib import ralph_state


def _attended_state(root: Path, session: str = "ralph") -> None:
    state = ralph_state.new_state("meta-harness", session=session)
    state.transition(ralph_state.PROPOSAL_GATE, action="research approved")
    state.transition(ralph_state.PLAN_GATE, action="proposal approved")
    state.transition(ralph_state.SHIP_CONFIRM_GATE, action="plan approved")
    state.transition(ralph_state.ATTENDED_RUN, action="ship confirmed")
    state.save(root)


def _records(root: Path) -> list[dict]:
    path = root / ".dev-kit" / "trace" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_checkpoint_is_resumable_and_not_terminal(tmp_path: Path) -> None:
    _attended_state(tmp_path)

    record = ralph_controller.record_checkpoint(
        tmp_path,
        session="ralph",
        reason="worker_stop",
        next_action="resume verification",
        payload_digest="abc123",
    )

    assert record["event_type"] == "ralph.worker.checkpointed"
    assert record["state"]["current_stage"] == ralph_state.ATTENDED_RUN
    assert ralph_controller.latest_checkpoint(tmp_path, "ralph")["next_action"] == (
        "resume verification"
    )
    assert (tmp_path / ".dev-kit" / "ralph" / "ralph.checkpoints.jsonl").exists()
    events = _records(tmp_path)
    assert events[-1]["event_type"] == "ralph.worker.checkpointed"
    assert events[-1]["outcome"] == "checkpointed"


def test_session_close_cannot_claim_workflow_completion(tmp_path: Path) -> None:
    _attended_state(tmp_path)

    record = ralph_controller.record_session_closed(
        tmp_path, session="ralph", reason="worker_session_end"
    )

    assert record["event_type"] == "ralph.worker.session_closed"
    assert record["outcome"] == "cancelled"
    event = _records(tmp_path)[-1]
    assert event["event_type"] == "ralph.worker.session_closed"
    assert event["evidence_ref"]["workflow_completed"] is False
    state = ralph_state.RalphState.load(tmp_path, "ralph")
    assert state.current_stage == ralph_state.ATTENDED_RUN


def test_unsafe_session_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ralph_controller.RalphControllerError):
        ralph_controller.record_checkpoint(tmp_path, session="../escape")

    # Defense-in-depth: rejection must fire BEFORE any filesystem side effect.
    # If _state_snapshot() had been called, RalphState.load would have created
    # .dev-kit/ralph/ or a state file. The checkpoint JSONL and the trace
    # events.jsonl are owned by record_checkpoint; neither must exist.
    assert not (tmp_path / ".dev-kit" / "ralph").exists()
    assert not (tmp_path / ".dev-kit" / "trace").exists()


def test_unsafe_session_close_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ralph_controller.RalphControllerError):
        ralph_controller.record_session_closed(tmp_path, session="../escape")
    assert not (tmp_path / ".dev-kit" / "ralph").exists()
    assert not (tmp_path / ".dev-kit" / "trace").exists()
