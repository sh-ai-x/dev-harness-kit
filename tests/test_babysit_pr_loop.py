"""Tests for the restart-safe approval-seeking babysit-pr controller."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from babysit_pr_cli import (  # noqa: E402
    persist_loop_outcome,
    persist_loop_snapshot,
)
from babysit_pr_loop import (  # noqa: E402
    CHANGE_DIRECTION,
    DONE,
    RECOVERY_REQUIRED,
    REPAIRING,
    RESET_CONTEXT,
    WAIT_FOR_APPROVAL,
    WAIT_FOR_CHECKS,
    LoopState,
    load_state,
    mark_transition_synced,
    new_state,
    next_wake_seconds,
    observe,
    record_outcome,
    save_state,
    transition_key,
)


def approved_check() -> dict[str, object]:
    return {"name": "pytest", "conclusion": "success", "databaseId": 1}


def pending_check() -> dict[str, object]:
    return {
        "name": "pytest",
        "conclusion": None,
        "databaseId": 1,
        "startedAt": "2026-08-20T12:00:00Z",
    }


def failing_check() -> dict[str, object]:
    return {"name": "pytest", "conclusion": "failure", "databaseId": 2}


def ghost_check() -> dict[str, object]:
    return {"name": "deleted-workflow", "conclusion": None}


def test_review_required_is_resumable_approval_wait_not_success() -> None:
    state = observe(
        new_state(7),
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    assert state.phase == WAIT_FOR_APPROVAL
    assert state.phase != DONE
    assert next_wake_seconds(state) == 30


def test_approved_and_green_is_only_terminal_state() -> None:
    state = observe(
        new_state(7),
        head_sha="abc",
        review_verdict="APPROVED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    assert state.phase == DONE
    assert next_wake_seconds(state) == 0


@pytest.mark.parametrize(
    ("checks", "verdict", "expected"),
    [
        ([pending_check()], "REVIEW_REQUIRED", WAIT_FOR_CHECKS),
        ([failing_check()], "REVIEW_REQUIRED", REPAIRING),
        ([approved_check()], "CHANGES_REQUESTED", REPAIRING),
        ([ghost_check()], "REVIEW_REQUIRED", RECOVERY_REQUIRED),
    ],
)
def test_snapshot_classification(checks, verdict, expected) -> None:
    state = observe(
        new_state(7),
        head_sha="abc",
        review_verdict=verdict,
        checks=checks,
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    assert state.phase == expected


def test_head_sha_change_bumps_context_epoch_and_resets_stale_strategy() -> None:
    state = LoopState(7, 7, phase=REPAIRING, head_sha="old", context_epoch=2)
    state = observe(
        state,
        head_sha="new",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    assert state.context_epoch == 3
    assert state.strategy == "continue"
    assert state.phase == WAIT_FOR_APPROVAL


def test_dynamic_skipped_defaults_to_empty_frozenset() -> None:
    # v1.1.0 — `LoopState` carries an optional `dynamic_skipped` set
    # of gate names that the LLM-judge layer recommended skipping for
    # the current head_sha. Defaults to empty so older persisted
    # state files (without the field) load cleanly.
    state = LoopState(7, 7)
    assert state.dynamic_skipped == frozenset()


def test_dynamic_skipped_persists_through_observe() -> None:
    # observe() without `dynamic_skipped` kwarg preserves the
    # existing value — the LLM-judge layer sets it on iteration N,
    # and iteration N+1 (without a new judge call) inherits it.
    state = LoopState(7, 7, dynamic_skipped=frozenset({"maintenance"}))
    state = observe(
        state,
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    assert state.dynamic_skipped == frozenset({"maintenance"})


def test_dynamic_skipped_persists_through_record_outcome() -> None:
    state = LoopState(7, 7, dynamic_skipped=frozenset({"maintenance"}))
    state = record_outcome(state, outcome="progress", now_iso="t")
    assert state.dynamic_skipped == frozenset({"maintenance"})


def test_observe_accepts_dynamic_skipped_kwarg() -> None:
    # Fresh judge decision: observer passes it explicitly.
    state = LoopState(7, 7)
    state = observe(
        state,
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
        dynamic_skipped=frozenset({"review", "maintenance"}),
    )
    assert state.dynamic_skipped == frozenset({"review", "maintenance"})


def test_dynamic_skipped_validates_known_gate_names() -> None:
    # Operator typo / stray value should fail closed at validate() time.
    with pytest.raises(ValueError):
        LoopState(7, 7, dynamic_skipped=frozenset({"lint"}))


def test_dynamic_skipped_round_trip_via_save_load() -> None:
    import tempfile
    state = LoopState(7, 7, dynamic_skipped=frozenset({"maintenance"}))
    state = observe(
        state,
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
        dynamic_skipped=frozenset({"review"}),
    )
    with tempfile.TemporaryDirectory() as td:
        from babysit_pr_loop import load_state, save_state
        path = save_state(state, f"{td}/state.json")
        loaded = load_state(path)
    assert loaded is not None
    assert loaded.dynamic_skipped == frozenset({"review"})


def test_no_information_evolves_then_resets_then_waits_for_recovery() -> None:
    state = new_state(7)
    state = record_outcome(state, outcome="unchanged", now_iso="t1")
    assert state.strategy == CHANGE_DIRECTION and state.phase == REPAIRING
    state = record_outcome(state, outcome="unchanged", now_iso="t2")
    assert state.strategy == RESET_CONTEXT and state.phase == REPAIRING
    state = record_outcome(state, outcome="unchanged", now_iso="t3")
    assert state.strategy == "recover" and state.phase == RECOVERY_REQUIRED
    assert next_wake_seconds(state) == 300


def test_partial_progress_evolves_without_resetting_the_pr() -> None:
    state = record_outcome(new_state(7), outcome="partial_progress", now_iso="t1")
    assert state.strategy == "evolve_step"
    assert state.phase == REPAIRING
    assert state.no_information == 0


def test_state_round_trips_atomically(tmp_path: Path) -> None:
    path = tmp_path / ".dev-kit" / "babysit-state.json"
    original = observe(
        new_state(7),
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    assert save_state(original, path) == path
    assert load_state(path) == original


def test_invalid_phase_is_rejected() -> None:
    with pytest.raises(ValueError):
        LoopState(1, 1, phase="finished").validate()


def test_production_snapshot_seam_loads_and_persists_state(tmp_path: Path) -> None:
    path = tmp_path / ".dev-kit" / "babysit-state.json"
    state = persist_loop_snapshot(
        parent_pr=7,
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
        github_tracker_issue=696,
        linear_issue="SHO-316",
        state_path=path,
    )
    assert state.phase == WAIT_FOR_APPROVAL
    assert state.github_tracker_issue == 696
    assert state.linear_issue == "SHO-316"
    assert load_state(path) == state


def test_production_outcome_seam_resumes_from_saved_state(tmp_path: Path) -> None:
    path = tmp_path / ".dev-kit" / "babysit-state.json"
    persist_loop_snapshot(
        parent_pr=7,
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
        state_path=path,
    )
    state = persist_loop_outcome(
        parent_pr=7,
        outcome="unchanged",
        now_iso="2026-08-20T12:01:00Z",
        state_path=path,
    )
    assert state.strategy == CHANGE_DIRECTION
    assert load_state(path) == state


def test_transition_key_and_sync_marker_are_restart_safe() -> None:
    state = observe(
        new_state(695),
        head_sha="abc",
        review_verdict="REVIEW_REQUIRED",
        checks=[approved_check()],
        now_epoch=1_700_000_000,
        now_iso="2026-08-20T12:00:00Z",
    )
    key = transition_key(state)
    synced = mark_transition_synced(state, now_iso="2026-08-20T12:01:00Z")
    assert key == "695:abc:0:wait_for_approval"
    assert synced.last_synced_transition == key
