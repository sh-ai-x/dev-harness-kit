"""Durable, approval-seeking state machine for ``babysit-pr``.

The skill prompt owns the model's repair behavior; this module owns the
restart-safe control plane around it.  Waiting for a human review, a slow
check, or new recovery evidence is a resumable state, not process success or
failure.  Only ``DONE`` means the PR is approved and required checks are green.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from lib.babysit_pr_reliability import classify_check

SCHEMA_VERSION = "1.2.0"
STATE_FILE = ".dev-kit/babysit-state.json"
DEFAULT_WAKE_SECONDS = 30
RECOVERY_WAKE_SECONDS = 300

DONE = "done"
REPAIRING = "repairing"
WAIT_FOR_CHECKS = "wait_for_checks"
WAIT_FOR_APPROVAL = "wait_for_approval"
RECOVERY_REQUIRED = "recovery_required"

CONTINUE = "continue"
EVOLVE_STEP = "evolve_step"
CHANGE_DIRECTION = "change_direction"
RESET_CONTEXT = "reset_context"
RECOVER = "recover"

# v1.1.0 — `dynamic_skipped` is the set of gate names the LLM-judge
# layer (`lib/gate_dynamic.py`) recommended skipping for the current
# head_sha. `frozenset` (not `set`) because the persisted default must
# be hashable-immutable. Inserted at the END so existing positional
# constructor calls in callers still work. `validate_loop_state` rejects
# unknown gate names so a typo can't silently disable a gate. The
# default value (`frozenset()`) means older persisted state files load
# cleanly via `load_state` (which pops `schema_version` and feeds the
# rest to `new_loop_state(**raw)`).
#
# v1.2.0 — `mergeable` and `merge_state_status` snapshot the PR's
# `gh pr view --json mergeable,mergeStateStatus` reading at the
# snapshot epoch. The classifier in `lib/babysit_pr_cli.persist_loop_snapshot`
# threads them into a `merge_conflict:<count>` failure_signature when
# the merge is conflicting, which the babysit-pr algorithm step 6.5
# (RESOLVE CONFLICT) recognizes and routes to `git fetch && git merge
# origin/<base>` rather than the normal log-fetch path. Older state
# files (no `mergeable` / `merge_state_status` keys) load cleanly via
# `load_state` because `_LOOP_STATE_DEFAULTS` fills the missing keys.
_LOOP_STATE_DEFAULTS: dict[str, Any] = {
    "parent_pr": 0,
    "current_pr": 0,
    "phase": WAIT_FOR_CHECKS,
    "head_sha": "",
    "context_epoch": 0,
    "iteration": 0,
    "repair_attempt": 0,
    "failure_signature": "",
    "no_information": 0,
    "strategy": CONTINUE,
    "last_action": "",
    "next_wake_at": "",
    "updated_at": "",
    "github_tracker_issue": None,
    "linear_issue": "",
    "last_synced_transition": "",
    "dynamic_skipped": frozenset(),
    "mergeable": "",
    "merge_state_status": "",
}


def new_loop_state(**overrides: Any) -> dict[str, Any]:
    """Build a fresh state dict with all defaults filled in.

    Validates eagerly so typos in `dynamic_skipped` fail at the call
    site rather than at the next `loop_state_to_dict()` / `observe()`
    call. `load_state` validates again after reconstruction —
    defense in depth.
    """
    out: dict[str, Any] = dict(_LOOP_STATE_DEFAULTS)
    out.update(overrides)
    validate_loop_state(out)
    return out


def validate_loop_state(state: Mapping[str, Any]) -> None:
    if state["parent_pr"] <= 0 or state["current_pr"] <= 0:
        raise ValueError("parent_pr and current_pr must be positive")
    if state["phase"] not in {
        DONE, REPAIRING, WAIT_FOR_CHECKS, WAIT_FOR_APPROVAL, RECOVERY_REQUIRED
    }:
        raise ValueError(f"unknown phase: {state['phase']}")
    if state["strategy"] not in {
        CONTINUE, EVOLVE_STEP, CHANGE_DIRECTION, RESET_CONTEXT, RECOVER
    }:
        raise ValueError(f"unknown strategy: {state['strategy']}")
    if state["context_epoch"] < 0 or state["iteration"] < 0 or state["repair_attempt"] < 0:
        raise ValueError("state counters cannot be negative")
    if state["github_tracker_issue"] is not None and state["github_tracker_issue"] <= 0:
        raise ValueError("github_tracker_issue must be positive")
    # Known gate names — bound to gates_state.VALID_GATE_KEYS at
    # module-import time so this stays in lockstep with the schema
    # SSOT. Imported here (not at top) to dodge the circular import
    # risk during babysit_pr_loop module init.
    from gates_state import VALID_GATE_KEYS
    unknown = set(state["dynamic_skipped"]) - VALID_GATE_KEYS
    if unknown:
        raise ValueError(f"dynamic_skipped has unknown gate name(s): {sorted(unknown)}")


def loop_state_to_dict(state: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize for JSON. Materialize `dynamic_skipped` to sorted list.

    `dynamic_skipped` is a `frozenset` (immutable-default requirement)
    which `json.dump` cannot serialize. Materialize to a sorted list so
    the persisted state is JSON-stable AND round-trips through
    `load_state` (which feeds the dict to `new_loop_state(**raw)` —
    sets/frozensets are reconstructed by the loader).
    """
    validate_loop_state(state)
    d: dict[str, Any] = dict(state)
    if isinstance(d.get("dynamic_skipped"), frozenset):
        d["dynamic_skipped"] = sorted(d["dynamic_skipped"])
    return {"schema_version": SCHEMA_VERSION, **d}


def new_state(parent_pr: int, *, current_pr: int | None = None) -> dict[str, Any]:
    """Create a resumable state; it is deliberately not terminal."""
    return new_loop_state(parent_pr=parent_pr, current_pr=current_pr or parent_pr)


def load_state(path: str | os.PathLike[str] = STATE_FILE) -> dict[str, Any] | None:
    """Load a previously persisted state, returning ``None`` when absent."""
    state_path = Path(path)
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("babysit state must be a JSON object")
    raw = dict(raw)
    raw.pop("schema_version", None)
    # v1.1.0 — JSON round-trips `dynamic_skipped` as a list (via
    # `loop_state_to_dict()`'s sorted(list) materialization). Convert
    # back to `frozenset` here so the in-memory state matches the
    # loader contract and equality checks against a freshly-constructed
    # state (with the `frozenset()` default) succeed.
    if "dynamic_skipped" in raw and isinstance(raw["dynamic_skipped"], list):
        raw["dynamic_skipped"] = frozenset(raw["dynamic_skipped"])
    state = new_loop_state(**raw)
    validate_loop_state(state)
    return state


def save_state(state: Mapping[str, Any], path: str | os.PathLike[str] = STATE_FILE) -> Path:
    """Atomically persist state so a killed worker cannot leave partial JSON."""
    validate_loop_state(state)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(loop_state_to_dict(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def classify_snapshot(
    *,
    review_verdict: str | None,
    checks: Iterable[Mapping[str, Any]],
    now_epoch: float,
) -> str:
    """Classify fresh PR state without making a terminal decision on wait."""
    check_list = list(checks)
    statuses = {classify_check(check, now_epoch) for check in check_list}
    if "ghost" in statuses:
        return RECOVERY_REQUIRED
    if review_verdict == "APPROVED" and check_list and statuses <= {"approved"}:
        return DONE
    if review_verdict == "CHANGES_REQUESTED" or "failing" in statuses:
        return REPAIRING
    if "pending" in statuses:
        return WAIT_FOR_CHECKS
    # Empty/REVIEW_REQUIRED is a healthy durable wait after all automatable work.
    return WAIT_FOR_APPROVAL


def observe(
    state: Mapping[str, Any],
    *,
    head_sha: str,
    review_verdict: str | None,
    checks: Iterable[Mapping[str, Any]],
    now_epoch: float,
    now_iso: str,
    failure_signature: str = "",
    dynamic_skipped: frozenset | None = None,
    mergeable: str = "",
    merge_state_status: str = "",
) -> dict[str, Any]:
    """Apply one fresh snapshot and advance the resumable phase.

    `dynamic_skipped` (v1.1.0) is the set of gate names the LLM-judge
    layer (`lib/gate_dynamic.py`) recommended skipping for this
    iteration. Passing `None` (default) preserves the existing value;
    passing a frozenset replaces it. The babysit-pr SKILL flow calls
    `select_gates_dynamic()` between SNAPSHOT and CLASSIFY and threads
    the result here.

    `mergeable` / `merge_state_status` (v1.2.0) snapshot the PR's
    `gh pr view --json mergeable,mergeStateStatus` reading so a
    CONFLICTING branch can be diagnosed across worker restarts. They
    default to "" (no signal) so older callers keep working.
    """
    phase = classify_snapshot(
        review_verdict=review_verdict, checks=checks, now_epoch=now_epoch
    )
    epoch_bump = bool(state["head_sha"] and state["head_sha"] != head_sha)
    new_dynamic = state["dynamic_skipped"] if dynamic_skipped is None else dynamic_skipped
    # v1.2.0 — Persist the latest merge state even when the snapshot
    # does not surface a conflict signature (a behind-but-clean rebase
    # should still bump the field on the next iteration).
    new_mergeable = mergeable or state.get("mergeable", "")
    new_merge_state = merge_state_status or state.get("merge_state_status", "")
    result = new_loop_state(
        parent_pr=state["parent_pr"],
        current_pr=state["current_pr"],
        phase=phase,
        head_sha=head_sha,
        context_epoch=state["context_epoch"] + int(epoch_bump),
        iteration=state["iteration"] + 1,
        repair_attempt=state["repair_attempt"],
        failure_signature="" if epoch_bump else failure_signature or state["failure_signature"],
        # A new commit invalidates the diagnosis attached to the old
        # context. Keeping it would let a restarted worker act on stale
        # evidence from a different head SHA.
        no_information=0 if epoch_bump else state["no_information"],
        strategy=CONTINUE if epoch_bump else state["strategy"],
        last_action="fresh_snapshot",
        next_wake_at="" if phase in {DONE, REPAIRING} else now_iso,
        updated_at=now_iso,
        github_tracker_issue=state["github_tracker_issue"],
        linear_issue=state["linear_issue"],
        last_synced_transition=state["last_synced_transition"],
        dynamic_skipped=new_dynamic,
        mergeable=new_mergeable,
        merge_state_status=new_merge_state,
    )
    return result


def record_outcome(
    state: Mapping[str, Any],
    *,
    outcome: str,
    now_iso: str,
    dynamic_skipped: frozenset | None = None,
) -> dict[str, Any]:
    """Record repair evidence and choose the next strategy.

    No-information never pretends the PR is complete.  After repeated
    unchanged outcomes it enters a resumable recovery state, allowing a new
    check/review event or a later model run to continue the lifecycle.

    `dynamic_skipped` (v1.1.0) — same semantics as in `observe()`:
    `None` preserves the existing value; a frozenset replaces it.
    """
    if outcome not in {"progress", "partial_progress", "unchanged", "regressed", "inconclusive"}:
        raise ValueError(f"unknown outcome: {outcome}")
    if outcome == "progress":
        next_strategy: str = CONTINUE
        next_phase: str = WAIT_FOR_CHECKS
        count = 0
    elif outcome == "partial_progress":
        next_strategy = EVOLVE_STEP
        next_phase = REPAIRING
        count = 0
    else:
        count = state["no_information"] + 1
        if count == 1:
            next_strategy = CHANGE_DIRECTION
            next_phase = REPAIRING
        elif count == 2:
            next_strategy = RESET_CONTEXT
            next_phase = REPAIRING
        else:
            next_strategy = RECOVER
            next_phase = RECOVERY_REQUIRED
    new_dynamic = state["dynamic_skipped"] if dynamic_skipped is None else dynamic_skipped
    result = new_loop_state(
        parent_pr=state["parent_pr"],
        current_pr=state["current_pr"],
        phase=next_phase,
        head_sha=state["head_sha"],
        context_epoch=state["context_epoch"],
        iteration=state["iteration"],
        repair_attempt=state["repair_attempt"],
        failure_signature=state["failure_signature"],
        no_information=count,
        strategy=next_strategy,
        last_action=f"outcome:{outcome}",
        next_wake_at=state["next_wake_at"],
        updated_at=now_iso,
        github_tracker_issue=state["github_tracker_issue"],
        linear_issue=state["linear_issue"],
        last_synced_transition=state["last_synced_transition"],
        dynamic_skipped=new_dynamic,
        mergeable=state.get("mergeable", ""),
        merge_state_status=state.get("merge_state_status", ""),
    )
    return result


def next_wake_seconds(state: Mapping[str, Any]) -> int:
    """Return a bounded operator-independent wake interval for resumable wait."""
    if state["phase"] == RECOVERY_REQUIRED:
        return RECOVERY_WAKE_SECONDS
    if state["phase"] in {WAIT_FOR_CHECKS, WAIT_FOR_APPROVAL}:
        return DEFAULT_WAKE_SECONDS
    return 0


def transition_key(state: Mapping[str, Any]) -> str:
    """Return the stable external-audit key for the current state."""
    return f"{state['parent_pr']}:{state['head_sha']}:{state['context_epoch']}:{state['phase']}"


def mark_transition_synced(state: Mapping[str, Any], *, now_iso: str) -> dict[str, Any]:
    """Record that the current phase transition was published externally."""
    result = new_loop_state(
        parent_pr=state["parent_pr"],
        current_pr=state["current_pr"],
        phase=state["phase"],
        head_sha=state["head_sha"],
        context_epoch=state["context_epoch"],
        iteration=state["iteration"],
        repair_attempt=state["repair_attempt"],
        failure_signature=state["failure_signature"],
        no_information=state["no_information"],
        strategy=state["strategy"],
        last_action=state["last_action"],
        next_wake_at=state["next_wake_at"],
        updated_at=now_iso,
        github_tracker_issue=state["github_tracker_issue"],
        linear_issue=state["linear_issue"],
        last_synced_transition=transition_key(state),
        dynamic_skipped=state["dynamic_skipped"],
        mergeable=state.get("mergeable", ""),
        merge_state_status=state.get("merge_state_status", ""),
    )
    return result
