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

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


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
) -> LoopState:
    """Apply one fresh snapshot and advance the resumable phase."""
    phase = classify_snapshot(
        review_verdict=review_verdict, checks=checks, now_epoch=now_epoch
    )
    epoch_bump = bool(state.head_sha and state.head_sha != head_sha)
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
    )
    result.validate()
    return result


def record_outcome(
    state: LoopState,
    *,
    outcome: str,
    now_iso: str,
) -> LoopState:
    """Record repair evidence and choose the next strategy.

    No-information never pretends the PR is complete.  After repeated
    unchanged outcomes it enters a resumable recovery state, allowing a new
    check/review event or a later model run to continue the lifecycle.
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
    result = replace(result, last_action=f"outcome:{outcome}", updated_at=now_iso)
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
