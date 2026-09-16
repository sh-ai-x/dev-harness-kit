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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from babysit_pr_reliability import classify_check

SCHEMA_VERSION = "1.0.0"
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


@dataclass(frozen=True)
class LoopState:
    """The minimal durable state needed to resume one PR safely."""

    parent_pr: int
    current_pr: int
    phase: str = WAIT_FOR_CHECKS
    head_sha: str = ""
    context_epoch: int = 0
    iteration: int = 0
    repair_attempt: int = 0
    failure_signature: str = ""
    no_information: int = 0
    strategy: str = CONTINUE
    last_action: str = ""
    next_wake_at: str = ""
    updated_at: str = ""
    github_tracker_issue: int | None = None
    linear_issue: str = ""
    last_synced_transition: str = ""
    # v1.1.0 — `dynamic_skipped` is the set of gate names the LLM-judge
    # layer (`lib/gate_dynamic.py`) recommended skipping for the current
    # head_sha. `frozenset` (not `set`) because `LoopState` is a frozen
    # dataclass and the default must be hashable. Inserted at the END
    # so existing positional constructor calls in tests still work.
    # `validate()` rejects unknown gate names so a typo can't silently
    # disable a gate. The default value (`frozenset()`) means older
    # persisted state files load cleanly via `load_state` (which pops
    # `schema_version` and feeds the rest to `LoopState(**raw)`).
    dynamic_skipped: frozenset = frozenset()

    def __post_init__(self) -> None:
        # Run `validate()` on construction so typos in `dynamic_skipped`
        # fail at the call site rather than at the next `to_dict()` /
        # `observe()` call. `load_state` validates again after
        # reconstruction — defense in depth.
        self.validate()

    def validate(self) -> None:
        if self.parent_pr <= 0 or self.current_pr <= 0:
            raise ValueError("parent_pr and current_pr must be positive")
        if self.phase not in {
            DONE, REPAIRING, WAIT_FOR_CHECKS, WAIT_FOR_APPROVAL, RECOVERY_REQUIRED
        }:
            raise ValueError(f"unknown phase: {self.phase}")
        if self.strategy not in {
            CONTINUE, EVOLVE_STEP, CHANGE_DIRECTION, RESET_CONTEXT, RECOVER
        }:
            raise ValueError(f"unknown strategy: {self.strategy}")
        if self.context_epoch < 0 or self.iteration < 0 or self.repair_attempt < 0:
            raise ValueError("state counters cannot be negative")
        if self.github_tracker_issue is not None and self.github_tracker_issue <= 0:
            raise ValueError("github_tracker_issue must be positive")
        # Known gate names — bound to gates_state.VALID_GATE_KEYS at
        # module-import time so this stays in lockstep with the schema
        # SSOT. Imported here (not at top) to dodge the circular import
        # risk during babysit_pr_loop module init.
        from gates_state import VALID_GATE_KEYS
        unknown = set(self.dynamic_skipped) - VALID_GATE_KEYS
        if unknown:
            raise ValueError(f"dynamic_skipped has unknown gate name(s): {sorted(unknown)}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        # v1.1.0 — `dynamic_skipped` is a frozenset (frozen-dataclass
        # hashable-default requirement) which `json.dump` cannot
        # serialize. Materialize to a sorted list so the persisted
        # state is JSON-stable AND round-trips through `load_state`
        # (which feeds the dict to `LoopState(**raw)` — sets/frozensets
        # are reconstructed by the `frozenset` annotation).
        d = asdict(self)
        if isinstance(d.get("dynamic_skipped"), frozenset):
            d["dynamic_skipped"] = sorted(d["dynamic_skipped"])
        return {"schema_version": SCHEMA_VERSION, **d}


def new_state(parent_pr: int, *, current_pr: int | None = None) -> LoopState:
    """Create a resumable state; it is deliberately not terminal."""
    state = LoopState(parent_pr=parent_pr, current_pr=current_pr or parent_pr)
    state.validate()
    return state


def load_state(path: str | os.PathLike[str] = STATE_FILE) -> LoopState | None:
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
    # `to_dict()`'s sorted(list) materialization). Convert back to
    # `frozenset` here so the in-memory state matches the dataclass
    # annotation and equality checks against a freshly-constructed
    # `LoopState` (with the `frozenset()` default) succeed.
    if "dynamic_skipped" in raw and isinstance(raw["dynamic_skipped"], list):
        raw["dynamic_skipped"] = frozenset(raw["dynamic_skipped"])
    state = LoopState(**raw)
    state.validate()
    return state


def save_state(state: LoopState, path: str | os.PathLike[str] = STATE_FILE) -> Path:
    """Atomically persist state so a killed worker cannot leave partial JSON."""
    state.validate()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
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
    state: LoopState,
    *,
    head_sha: str,
    review_verdict: str | None,
    checks: Iterable[Mapping[str, Any]],
    now_epoch: float,
    now_iso: str,
    failure_signature: str = "",
    dynamic_skipped: frozenset | None = None,
) -> LoopState:
    """Apply one fresh snapshot and advance the resumable phase.

    `dynamic_skipped` (v1.1.0) is the set of gate names the LLM-judge
    layer (`lib/gate_dynamic.py`) recommended skipping for this
    iteration. Passing `None` (default) preserves the existing value;
    passing a frozenset replaces it. The babysit-pr SKILL flow calls
    `select_gates_dynamic()` between SNAPSHOT and CLASSIFY and threads
    the result here.
    """
    phase = classify_snapshot(
        review_verdict=review_verdict, checks=checks, now_epoch=now_epoch
    )
    epoch_bump = bool(state.head_sha and state.head_sha != head_sha)
    new_dynamic = state.dynamic_skipped if dynamic_skipped is None else dynamic_skipped
    result = replace(
        state,
        phase=phase,
        head_sha=head_sha,
        context_epoch=state.context_epoch + int(epoch_bump),
        # A new commit invalidates the diagnosis attached to the old
        # context. Keeping it would let a restarted worker act on stale
        # evidence from a different head SHA.
        failure_signature="" if epoch_bump else failure_signature or state.failure_signature,
        iteration=state.iteration + 1,
        no_information=0 if epoch_bump else state.no_information,
        strategy=CONTINUE if epoch_bump else state.strategy,
        last_action="fresh_snapshot",
        next_wake_at="" if phase in {DONE, REPAIRING} else now_iso,
        updated_at=now_iso,
        dynamic_skipped=new_dynamic,
    )
    result.validate()
    return result


def record_outcome(
    state: LoopState,
    *,
    outcome: str,
    now_iso: str,
    dynamic_skipped: frozenset | None = None,
) -> LoopState:
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
        result = replace(state, no_information=0, strategy=CONTINUE, phase=WAIT_FOR_CHECKS)
    elif outcome == "partial_progress":
        result = replace(state, no_information=0, strategy=EVOLVE_STEP, phase=REPAIRING)
    else:
        count = state.no_information + 1
        if count == 1:
            strategy = CHANGE_DIRECTION
            phase = REPAIRING
        elif count == 2:
            strategy = RESET_CONTEXT
            phase = REPAIRING
        else:
            strategy = RECOVER
            phase = RECOVERY_REQUIRED
        result = replace(state, no_information=count, strategy=strategy, phase=phase)
    new_dynamic = state.dynamic_skipped if dynamic_skipped is None else dynamic_skipped
    result = replace(
        result,
        last_action=f"outcome:{outcome}",
        updated_at=now_iso,
        dynamic_skipped=new_dynamic,
    )
    result.validate()
    return result


def next_wake_seconds(state: LoopState) -> int:
    """Return a bounded operator-independent wake interval for resumable wait."""
    if state.phase == RECOVERY_REQUIRED:
        return RECOVERY_WAKE_SECONDS
    if state.phase in {WAIT_FOR_CHECKS, WAIT_FOR_APPROVAL}:
        return DEFAULT_WAKE_SECONDS
    return 0


def transition_key(state: LoopState) -> str:
    """Return the stable external-audit key for the current state."""
    return f"{state.parent_pr}:{state.head_sha}:{state.context_epoch}:{state.phase}"


def mark_transition_synced(state: LoopState, *, now_iso: str) -> LoopState:
    """Record that the current phase transition was published externally."""
    result = replace(
        state,
        last_synced_transition=transition_key(state),
        updated_at=now_iso,
    )
    result.validate()
    return result
