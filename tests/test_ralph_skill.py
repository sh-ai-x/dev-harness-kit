"""Regression suite for /dev-kit:ralph.

Covers the chain order, 4-gate contract, Edit-then-approve rewind,
attended_lock invariant, --dry-run determinism, and Ralph-loop
safety valves. Imports ``skills.ralph.lib.ralph_state`` as a pure
module — no subprocess, no gh CLI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make ``skills.ralph.lib`` importable when pytest is invoked from
# the project root.
ROOT = Path(__file__).resolve().parents[1]
SKILL_LIB = ROOT / "skills" / "ralph" / "lib"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SKILL_LIB))

import ralph_state as rs  # noqa: E402


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".dev-kit").mkdir()
    return tmp_path


# ============================================================================
# 1. Chain ordering
# ============================================================================


def test_chain_order_is_linear_until_ship_confirm():
    assert rs.GATE_ORDER == [
        rs.RESEARCH_GATE,
        rs.PROPOSAL_GATE,
        rs.PLAN_GATE,
        rs.SHIP_CONFIRM_GATE,
        rs.ATTENDED_RUN,
    ]


def test_each_gate_can_only_advance_to_the_next():
    state = rs.new_state("hello")
    assert state.current_stage == rs.RESEARCH_GATE

    state.transition(rs.PROPOSAL_GATE)
    assert state.current_stage == rs.PROPOSAL_GATE

    state.transition(rs.PLAN_GATE)
    assert state.current_stage == rs.PLAN_GATE

    state.transition(rs.SHIP_CONFIRM_GATE)
    assert state.current_stage == rs.SHIP_CONFIRM_GATE

    state.transition(rs.ATTENDED_RUN)
    assert state.current_stage == rs.ATTENDED_RUN
    assert state.attended_lock is True


def test_skipping_a_gate_is_rejected():
    state = rs.new_state("hello")
    with pytest.raises(rs.InvalidTransitionError):
        state.transition(rs.PLAN_GATE)  # skipping PROPOSAL_GATE


def test_backwards_transition_is_rejected():
    state = rs.new_state("hello")
    state.transition(rs.PROPOSAL_GATE)
    with pytest.raises(rs.InvalidTransitionError):
        state.transition(rs.RESEARCH_GATE)


def test_terminal_states_have_no_outgoing_transitions():
    for terminal in rs.TERMINAL_STATES:
        state = rs.RalphState(current_stage=terminal)
        assert state.can_enter(rs.RESEARCH_GATE) is False


# ============================================================================
# 2. 4-gate contract
# ============================================================================


@pytest.mark.parametrize("gate", list(rs.GATE_STATES))
def test_gates_allow_ask(gate):
    state = rs.RalphState(current_stage=gate, attended_lock=False)
    assert state.is_gate() is True
    assert state.can_ask_question() is True


def test_attended_run_forbids_ask():
    state = rs.RalphState(current_stage=rs.ATTENDED_RUN)
    assert state.can_ask_question() is False


def test_terminal_states_forbid_ask():
    for terminal in rs.TERMINAL_STATES:
        state = rs.RalphState(current_stage=terminal)
        assert state.can_ask_question() is False


def test_assert_can_ask_raises_during_attended_run():
    state = rs.RalphState(current_stage=rs.ATTENDED_RUN)
    with pytest.raises(rs.AttendedLockError):
        rs.assert_can_ask(state, question_kind="final-approval")


# ============================================================================
# 3. attended_lock invariant (the headline test)
# ============================================================================


def test_attended_lock_set_on_ship_confirm_to_attended():
    state = rs.new_state("hello")
    state.transition(rs.PROPOSAL_GATE)
    state.transition(rs.PLAN_GATE)
    state.transition(rs.SHIP_CONFIRM_GATE)
    assert state.attended_lock is False
    state.transition(rs.ATTENDED_RUN, action="user approved ship-confirm")
    assert state.attended_lock is True


def test_attended_lock_blocks_ask_even_at_ship_confirm_during_attended_run():
    """Once locked, even a hypothetical SHIP_CONFIRM_GATE->GATE rollback is blocked."""
    state = rs.RalphState(
        current_stage=rs.ATTENDED_RUN, attended_lock=True
    )
    # Even if someone asks "can we go back to PLAN_GATE?" the lock forbids
    # forward gate transitions.
    assert state.can_enter(rs.PLAN_GATE) is False


def test_attended_lock_blocks_rewind():
    state = rs.RalphState(
        current_stage=rs.ATTENDED_RUN, attended_lock=True
    )
    with pytest.raises(rs.AttendedLockError):
        state.rewind_to(rs.PLAN_GATE, reason="user changed mind")


def test_attended_lock_emits_forensic_field():
    state = rs.RalphState(current_stage=rs.ATTENDED_RUN, attended_lock=True)
    with pytest.raises(rs.AttendedLockError):
        rs.assert_can_ask(state, question_kind="mid-build-prompt")
    # record_blocked_ask fires BEFORE the raise, so the forensic field
    # is populated even though the assertion raises.
    assert state.last_blocked_ask is not None
    assert "mid-build-prompt" in state.last_blocked_ask
    assert "ATTENDED_RUN" in state.last_blocked_ask


# ============================================================================
# 4. Edit-then-approve rewind
# ============================================================================


def test_rewind_to_proposal_from_plan_clears_downstream():
    state = rs.new_state("hello")
    state.transition(rs.PROPOSAL_GATE)
    state.transition(rs.PLAN_GATE)
    state.plan_hand_off = "phases/foo/index.json"
    state.ambiguity_answers["A2"] = "use pytest parametrize"

    state.rewind_to(rs.PROPOSAL_GATE, reason="user edits ambiguity A2")

    assert state.current_stage == rs.PROPOSAL_GATE
    assert state.attended_lock is False
    assert state.plan_hand_off == ""
    assert state.ambiguity_answers == {}
    assert len(state.rewind_history) == 1
    assert state.rewind_history[0]["from"] == rs.PLAN_GATE
    assert state.rewind_history[0]["to"] == rs.PROPOSAL_GATE


def test_rewind_forward_is_rejected():
    state = rs.new_state("hello")
    state.transition(rs.PROPOSAL_GATE)
    with pytest.raises(rs.InvalidTransitionError):
        state.rewind_to(rs.PLAN_GATE, reason="user wants to skip ahead")


def test_rewind_to_unknown_gate_is_rejected():
    state = rs.new_state("hello")
    with pytest.raises(rs.InvalidTransitionError):
        state.rewind_to("NOT_A_GATE", reason="user typo")


def test_rewind_to_attended_run_is_rejected():
    """ATTENDED_RUN is past the gate boundary; it is not a rewind target."""
    state = rs.new_state("hello")
    state.transition(rs.PROPOSAL_GATE)
    with pytest.raises(rs.InvalidTransitionError):
        state.rewind_to(rs.ATTENDED_RUN, reason="user confused")


# ============================================================================
# 5. Ralph-loop safety valves
# ============================================================================


def test_recovery_required_terminal_is_well_formed():
    state = rs.new_state("hello")
    state.transition(rs.RECOVERY_REQUIRED, action="build 3-cycle guard fired")
    assert state.is_terminal() is True
    assert state.can_ask_question() is False


def test_user_merge_required_terminal_is_well_formed():
    state = rs.RalphState(current_stage=rs.ATTENDED_RUN, attended_lock=True)
    state.transition(rs.USER_MERGE_REQUIRED, action="ship surfaced merge boundary")
    assert state.is_terminal() is True


def test_iteration_counter_increments_on_transition():
    state = rs.new_state("hello")
    assert state.iteration == 0
    state.transition(rs.PROPOSAL_GATE)
    assert state.iteration == 1
    state.transition(rs.PLAN_GATE)
    assert state.iteration == 2


# ============================================================================
# 6. Persistence (atomic write + reload)
# ============================================================================


def test_save_load_round_trip(project_root: Path):
    state = rs.new_state("hello world", session="alpha")
    state.proposal_html = "docs/proposals/review/foo/main.html"
    state.ambiguity_answers["A1"] = "ok"
    state.transition(rs.PROPOSAL_GATE)
    path = state.save(project_root)

    assert path.exists()
    loaded = rs.RalphState.load(project_root, session="alpha")
    assert loaded.idea == "hello world"
    assert loaded.proposal_html == "docs/proposals/review/foo/main.html"
    assert loaded.ambiguity_answers == {"A1": "ok"}
    assert loaded.current_stage == rs.PROPOSAL_GATE


def test_save_creates_dev_kit_dir(project_root: Path):
    state = rs.new_state("hello", session="beta")
    target = project_root / ".dev-kit" / "ralph" / "beta.json"
    assert target.parent.parent.exists()  # .dev-kit from fixture
    state.save(project_root)
    assert target.exists()


def test_load_returns_fresh_state_when_missing(project_root: Path):
    state = rs.RalphState.load(project_root, session="nonexistent")
    assert state.current_stage == rs.RESEARCH_GATE
    assert state.session == "nonexistent"


def test_save_is_atomic_no_partial_writes(project_root: Path):
    """The atomic_write_text helper must not leave .tmp files behind."""
    state = rs.new_state("hello", session="gamma")
    state.save(project_root)
    leftover = list((project_root / ".dev-kit" / "ralph").glob(".ralph.*.tmp"))
    assert leftover == []


# ============================================================================
# 7. Determinism (orchestrator is pure; --dry-run stability surface)
# ============================================================================


def test_new_state_is_deterministic_for_same_idea_and_session():
    a = rs.new_state("hello", session="d1")
    b = rs.new_state("hello", session="d1")
    # started_at differs by clock; everything else must match.
    a.started_at = b.started_at = "<fixed>"
    assert a.to_dict() == b.to_dict()


def test_transition_records_action_and_next_action():
    state = rs.new_state("hello")
    state.transition(rs.PROPOSAL_GATE, action="user approved research")
    assert state.last_action == "user approved research"


def test_state_round_trip_preserves_all_fields(project_root: Path):
    state = rs.new_state("hello", session="rt")
    state.sub_stage = "BUILD_STEP_3_OF_5"
    state.ambiguity_answers["A3"] = "no"
    state.rewind_history.append(
        {"from": "PLAN_GATE", "to": "PROPOSAL_GATE", "reason": "edit", "at": "x"}
    )
    state.last_blocked_ask = "test"
    state.save(project_root)
    loaded = rs.RalphState.load(project_root, session="rt")
    assert loaded.sub_stage == "BUILD_STEP_3_OF_5"
    assert loaded.ambiguity_answers == {"A3": "no"}
    assert loaded.rewind_history[0]["from"] == "PLAN_GATE"
    assert loaded.last_blocked_ask == "test"


# ============================================================================
# 8. Smoke — the chain runs end-to-end in pure Python (no subprocess)
# ============================================================================


def test_chain_runs_to_done_in_pure_python(project_root: Path):
    """The headline smoke test: full chain, no subprocess, no gh."""
    state = rs.new_state("hello world", session="smoke")
    state.save(project_root)

    state.transition(rs.PROPOSAL_GATE, action="research approved")
    state.transition(rs.PLAN_GATE, action="proposal approved")
    state.transition(rs.SHIP_CONFIRM_GATE, action="plan approved")
    state.transition(rs.ATTENDED_RUN, action="ship-confirm approved")
    state.transition(rs.DONE, action="build green + review approved + tag pushed")

    assert state.is_terminal()
    assert state.attended_lock is True
    path = state.save(project_root)
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["current_stage"] == rs.DONE
