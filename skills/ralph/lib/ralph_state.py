"""Pure state machine for /dev-kit:ralph.

5 top-level stages (4 interactive gates + 1 attended execution) and
3 terminal states. No subprocess. No I/O at import time. Persists
to ``.dev-kit/ralph/<session>.json`` on every transition via
``atomic_write_text``.

Invariants
----------
* ``attended_lock`` is False until SHIP_CONFIRM_GATE exits Approve.
  Once True, ``can_ask_question()`` returns False forever.
* ``rewind_to(stage)`` clears all downstream state and refuses to
  rewind past the current stage (no-op if target >= current).
* Every transition writes the state file atomically. The on-disk
  copy is always the recovery source-of-truth.

This module is importable as ``from skills.ralph.lib import ralph_state``
and is consumed by both ``scripts/ralph_drive.sh`` and the regression
suite under ``tests/test_ralph_skill.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------------
# Stages (top-level)
# ----------------------------------------------------------------------------

# Interactive gates — AskUserQuestion is allowed at these stages.
RESEARCH_GATE = "RESEARCH_GATE"
PROPOSAL_GATE = "PROPOSAL_GATE"
PLAN_GATE = "PLAN_GATE"
SHIP_CONFIRM_GATE = "SHIP_CONFIRM_GATE"

# Locked execution — no AskUserQuestion allowed (attended_lock=True).
ATTENDED_RUN = "ATTENDED_RUN"

# Terminal states.
DONE = "DONE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
USER_MERGE_REQUIRED = "USER_MERGE_REQUIRED"

STATE_SCHEMA_VERSION = 2

# Ordered chain — drives gate progression and rewind validation.
GATE_ORDER: List[str] = [
    RESEARCH_GATE,
    PROPOSAL_GATE,
    PLAN_GATE,
    SHIP_CONFIRM_GATE,
    ATTENDED_RUN,
]

TERMINAL_STATES = {DONE, RECOVERY_REQUIRED, USER_MERGE_REQUIRED}
GATE_STATES = {RESEARCH_GATE, PROPOSAL_GATE, PLAN_GATE, SHIP_CONFIRM_GATE}

# Allowed transitions (current → set-of-valid-targets). SHIP_CONFIRM_GATE
# is the only state that may enter ATTENDED_RUN.
ALLOWED_TRANSITIONS: Dict[str, set] = {
    RESEARCH_GATE: {PROPOSAL_GATE, RECOVERY_REQUIRED},
    PROPOSAL_GATE: {PLAN_GATE, RECOVERY_REQUIRED},
    PLAN_GATE: {SHIP_CONFIRM_GATE, RECOVERY_REQUIRED},
    SHIP_CONFIRM_GATE: {ATTENDED_RUN, RECOVERY_REQUIRED},
    ATTENDED_RUN: {DONE, RECOVERY_REQUIRED, USER_MERGE_REQUIRED},
    DONE: set(),
    RECOVERY_REQUIRED: set(),
    USER_MERGE_REQUIRED: set(),
}


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class RalphStateError(Exception):
    """Raised on any invalid state-machine operation."""


class AttendedLockError(RalphStateError):
    """Raised when an AskUserQuestion is attempted during ATTENDED_RUN."""


class InvalidTransitionError(RalphStateError):
    """Raised when ``transition()`` is asked for an edge that does not exist."""


class StateValidationError(RalphStateError):
    """Raised when a persisted checkpoint cannot be trusted for recovery."""


# ----------------------------------------------------------------------------
# State record
# ----------------------------------------------------------------------------


@dataclass
class RalphState:
    """Mutable in-memory state. Persist via ``save()`` after every mutation."""

    session: str = "default"
    started_at: str = ""
    idea: str = ""
    schema_version: int = STATE_SCHEMA_VERSION
    run_id: str = ""
    checkpoint_seq: int = 0
    current_stage: str = RESEARCH_GATE
    sub_stage: str = "AWAITING_USER"
    # Ordered evidence of sub-stages that completed successfully during the
    # current attended run. This is deliberately separate from ``sub_stage``:
    # the latter is the cursor, while this list proves which boundaries were
    # actually crossed before a terminal state was emitted.
    completed_sub_stages: List[str] = field(default_factory=list)
    last_completed_sub_stage: str = ""
    active_attempt: Dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    retry_count: int = 0
    deadline: str = ""
    terminal_reason: str = ""
    recovery_reason: str = ""
    recovery_metadata: Dict[str, Any] = field(default_factory=dict)
    event_log_path: str = ""
    failure_log_path: str = ""
    progress_path: str = ""
    stage_evidence: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    last_event_id: str = ""
    attended_lock: bool = False
    iteration: int = 0
    ambiguity_answers: Dict[str, str] = field(default_factory=dict)
    evidence_hand_off: str = ""
    proposal_yaml: str = ""
    proposal_html: str = ""
    plan_hand_off: str = ""
    build_state: str = ""
    babysit_state: str = ""
    ship_state: str = ""
    rewind_history: List[Dict[str, Any]] = field(default_factory=list)
    last_blocked_ask: Optional[str] = None
    last_action: str = ""
    next_action: str = ""
    blockers: List[str] = field(default_factory=list)

    @property
    def checkpoint_sequence(self) -> int:
        """Readable alias for the persisted ``checkpoint_seq`` field."""
        return self.checkpoint_seq

    @checkpoint_sequence.setter
    def checkpoint_sequence(self, value: int) -> None:
        self.checkpoint_seq = value

    # ------------------------------------------------------------------
    # Pure helpers (no I/O)
    # ------------------------------------------------------------------

    def is_terminal(self) -> bool:
        return self.current_stage in TERMINAL_STATES

    def is_gate(self) -> bool:
        return self.current_stage in GATE_STATES

    def is_attended(self) -> bool:
        return self.current_stage == ATTENDED_RUN or self.attended_lock

    def can_ask_question(self) -> bool:
        """Returns False once ``attended_lock`` is set or we are in ATTENDED_RUN.

        This is the *only* check the orchestrator uses to decide whether
        to emit an AskUserQuestion. It is enforced at the state-machine
        layer (invariant), not by convention.
        """
        if self.attended_lock:
            return False
        if self.current_stage == ATTENDED_RUN:
            return False
        if self.is_terminal():
            return False
        return True

    def can_enter(self, target: str) -> bool:
        if target not in ALLOWED_TRANSITIONS.get(self.current_stage, set()):
            return False
        # Once attended_lock is set, the only allowed forward transition
        # is into the terminal states (no further gates).
        if self.attended_lock and target not in TERMINAL_STATES:
            return False
        return True

    # ------------------------------------------------------------------
    # Mutating transitions
    # ------------------------------------------------------------------

    def transition(self, target: str, *, action: str = "") -> None:
        if not self.can_enter(target):
            raise InvalidTransitionError(
                f"cannot transition {self.current_stage} -> {target}"
            )
        previous = self.current_stage
        self.current_stage = target
        self.iteration += 1
        if action:
            self.last_action = action
        # Setting attended_lock is a one-way trip: once SHIP_CONFIRM_GATE
        # approves, the lock is set on the SHIP_CONFIRM_GATE -> ATTENDED_RUN
        # edge. The reverse (REWIND) path also resets it.
        if previous == SHIP_CONFIRM_GATE and target == ATTENDED_RUN:
            self.attended_lock = True
            self.sub_stage = "BUILD"
            self.completed_sub_stages.clear()
            self.last_completed_sub_stage = ""
            self.active_attempt.clear()
            self.terminal_reason = ""
            self.recovery_reason = ""
            self.recovery_metadata.clear()
        if target == DONE:
            self.terminal_reason = action or "completed"
        elif target == USER_MERGE_REQUIRED:
            self.terminal_reason = action or "human merge required"
        elif target == RECOVERY_REQUIRED:
            self.terminal_reason = "recovery required"
            if action:
                self.recovery_reason = action
        elif target not in TERMINAL_STATES:
            self.terminal_reason = ""
        # Reset next_action — caller is expected to populate.
        if not self.next_action:
            self.next_action = f"enter {target}"

    def resume_attended(self, *, reason: str = "operator requested resume") -> None:
        """Re-enter a recoverable attended run without weakening its lock."""
        if self.current_stage == ATTENDED_RUN:
            if not self.attended_lock:
                raise StateValidationError(
                    "ATTENDED_RUN cannot resume without attended_lock=True"
                )
            return
        if self.current_stage != RECOVERY_REQUIRED:
            raise InvalidTransitionError(
                f"cannot resume attended run from {self.current_stage}"
            )
        if not self.attended_lock:
            raise AttendedLockError(
                "cannot resume attended run before the attended boundary"
            )
        if self.recovery_metadata.get("resume_allowed") is False:
            raise RalphStateError(
                "recovery requires operator reconciliation before resume"
            )
        self.current_stage = ATTENDED_RUN
        self.iteration += 1
        self.retry_count += 1
        self.terminal_reason = ""
        self.last_action = f"resume ATTENDED_RUN ({reason})"
        self.next_action = f"retry {self.sub_stage or 'next stage'}"
        self.recovery_metadata["last_resumed_at"] = _now_iso()
        self.recovery_metadata["resume_count"] = int(
            self.recovery_metadata.get("resume_count", 0)
        ) + 1

    def rewind_to(self, target: str, *, reason: str = "") -> None:
        """Edit-then-approve handler.

        ``target`` must be one of the GATE_STATES and must precede the
        current stage in GATE_ORDER. Refuses to rewind forward or to
        rewind past the current stage. Refuses to rewind once
        ``attended_lock`` is set (the user has already crossed the
        one-way boundary).
        """
        if self.attended_lock:
            raise AttendedLockError(
                "cannot rewind: attended_lock is set; the attended phase "
                "is in progress"
            )
        if target not in GATE_STATES:
            raise InvalidTransitionError(
                f"rewind target must be a gate, got {target!r}"
            )
        if target not in GATE_ORDER:
            raise InvalidTransitionError(f"unknown gate {target!r}")
        current_idx = GATE_ORDER.index(self.current_stage)
        target_idx = GATE_ORDER.index(target)
        if target_idx >= current_idx:
            raise InvalidTransitionError(
                f"cannot rewind forward: {self.current_stage} -> {target}"
            )
        self.rewind_history.append(
            {
                "from": self.current_stage,
                "to": target,
                "reason": reason,
                "at": _now_iso(),
            }
        )
        self.current_stage = target
        self.attended_lock = False
        self.sub_stage = "AWAITING_USER"
        self.completed_sub_stages.clear()
        self.last_completed_sub_stage = ""
        self.active_attempt.clear()
        self.attempt_count = 0
        self.retry_count = 0
        self.terminal_reason = ""
        self.recovery_reason = ""
        self.recovery_metadata.clear()
        self.stage_evidence.clear()
        self.last_event_id = ""
        self.ambiguity_answers.clear()
        self.plan_hand_off = ""
        self.build_state = ""
        self.babysit_state = ""
        self.ship_state = ""
        self.last_blocked_ask = None
        self.last_action = f"rewind to {target} ({reason or 'unspecified'})"
        self.next_action = f"re-render {target}"

    def record_blocked_ask(self, question_kind: str) -> None:
        """Forensic-only — fires when can_ask_question() returns False."""
        self.last_blocked_ask = (
            f"{question_kind} at {_now_iso()} "
            f"(stage={self.current_stage}, lock={self.attended_lock})"
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RalphState":
        # Tolerate extra keys (forward-compat) and missing keys (defaults).
        # Schema 1 is the pre-checkpoint format and is upgraded in memory;
        # no new field is treated as proof that work completed.
        raw = dict(raw)
        try:
            version = int(raw.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise StateValidationError("schema_version must be an integer") from exc
        if version > STATE_SCHEMA_VERSION:
            raise StateValidationError(
                f"unsupported Ralph state schema {version}; maximum supported "
                f"is {STATE_SCHEMA_VERSION}"
            )
        raw["schema_version"] = STATE_SCHEMA_VERSION
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        state = cls(**{k: v for k, v in raw.items() if k in valid})
        if not state.run_id:
            state.run_id = _run_id(state.session, state.idea)
        if state.active_attempt is None:
            state.active_attempt = {}
        state.validate()
        return state

    def save(self, project_root: Path) -> Path:
        # Preserve the constructor's convenient defaults used by hook tests
        # and legacy callers while keeping every persisted checkpoint
        # addressable for event joins and recovery.
        if not self.run_id:
            self.run_id = _run_id(self.session, self.idea)
        self.prepare_artifact_paths(project_root)
        self.validate()
        path = self._state_path(project_root, self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, payload)
        return path

    @classmethod
    def load(cls, project_root: Path, session: str = "default") -> "RalphState":
        path = cls._state_path(project_root, session)
        if not path.exists():
            return cls(
                session=session,
                schema_version=STATE_SCHEMA_VERSION,
                run_id=_run_id(session, ""),
            )
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def validate(self) -> None:
        """Validate checkpoint-critical fields before dispatch or publish."""
        valid_stages = GATE_STATES | {ATTENDED_RUN} | TERMINAL_STATES
        if self.current_stage not in valid_stages:
            raise StateValidationError(f"unknown current_stage: {self.current_stage!r}")
        if self.schema_version != STATE_SCHEMA_VERSION:
            raise StateValidationError("invalid Ralph state schema_version")
        if not self.run_id:
            raise StateValidationError("run_id must be non-empty")
        for name in ("checkpoint_seq", "iteration", "attempt_count", "retry_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise StateValidationError(f"{name} must be a non-negative integer")
        if not isinstance(self.active_attempt, dict):
            raise StateValidationError("active_attempt must be an object")
        if self.active_attempt:
            for key in ("attempt_id", "stage", "sub_stage", "started_at"):
                if not self.active_attempt.get(key):
                    raise StateValidationError(f"active_attempt.{key} must be non-empty")
            if self.active_attempt.get("stage") != ATTENDED_RUN:
                raise StateValidationError("active_attempt.stage must be ATTENDED_RUN")
        if not isinstance(self.completed_sub_stages, list) or any(
            not isinstance(item, str)
            or item not in {"BUILD", "BABYSIT", "SHIP"}
            for item in self.completed_sub_stages
        ):
            raise StateValidationError(
                "completed_sub_stages must contain only BUILD, BABYSIT, or SHIP"
            )
        if len(set(self.completed_sub_stages)) != len(self.completed_sub_stages):
            raise StateValidationError("completed_sub_stages must not contain duplicates")
        # Keep checkpoint loading forensic-friendly.  A stale or manually
        # recovered record may contain the lock bit alongside an old gate;
        # the lock still makes ``can_ask_question`` fail closed and prevents
        # forward gate transitions.  Rejecting the record here would hide the
        # evidence needed by the recovery path.

    def prepare_artifact_paths(self, project_root: Path) -> None:
        """Set safe, project-relative journal paths without writing files."""
        del project_root  # kept explicit so callers cannot forget the root
        safe = "".join(c for c in self.session if c.isalnum() or c in "-_") or "default"
        base = Path(".dev-kit") / "ralph"
        if not self.event_log_path:
            # Ralph lifecycle events use the repository-wide trace_log v1
            # journal. Per-session state remains under ralph/, but there is
            # one event source for hooks, build, and Ralph metrics.
            self.event_log_path = str(Path(".dev-kit") / "trace" / "events.jsonl")
        if not self.failure_log_path:
            self.failure_log_path = str(base / f"{safe}.failures.jsonl")
        if not self.progress_path:
            self.progress_path = str(base / f"{safe}.progress.md")

    @staticmethod
    def _state_path(project_root: Path, session: str) -> Path:
        safe = "".join(c for c in session if c.isalnum() or c in "-_") or "default"
        return project_root / ".dev-kit" / "ralph" / f"{safe}.json"


# ----------------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------------


def new_state(
    idea: str, *, session: str = "default", deadline: str = ""
) -> RalphState:
    return RalphState(
        session=session,
        started_at=_now_iso(),
        idea=idea,
        schema_version=STATE_SCHEMA_VERSION,
        run_id=_run_id(session, idea),
        deadline=deadline,
        current_stage=RESEARCH_GATE,
        sub_stage="AWAITING_USER",
        last_action=f"init at {RESEARCH_GATE}",
        next_action=f"enter {RESEARCH_GATE}",
    )


def _run_id(session: str, idea: str) -> str:
    """Derive a stable run id while keeping init/reload deterministic."""
    digest = hashlib.sha256(f"{session}\0{idea}".encode("utf-8")).hexdigest()[:20]
    return f"ralph-{digest}"


def assert_can_ask(state: RalphState, *, question_kind: str) -> None:
    """Raise AttendedLockError if AskUserQuestion is forbidden.

    The orchestrator calls this BEFORE invoking AskUserQuestion. The
    question is *not* asked when the lock fires — the state machine
    records ``last_blocked_ask`` for forensics and raises.
    """
    if not state.can_ask_question():
        state.record_blocked_ask(question_kind)
        raise AttendedLockError(
            f"AskUserQuestion forbidden during {state.current_stage} "
            f"(attended_lock={state.attended_lock})"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ralph.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------------------
# CLI surface (used by scripts/ralph_drive.sh)
# ----------------------------------------------------------------------------


def _cli(argv: List[str]) -> int:
    """Tiny CLI for inspecting state from bash."""
    import argparse

    parser = argparse.ArgumentParser(prog="ralph_state")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session", default="default")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show")
    init_p = sub.add_parser("init")
    init_p.add_argument("idea")
    init_p.add_argument("--deadline", default="")
    trans = sub.add_parser("transition")
    trans.add_argument("target")
    trans.add_argument("--action", default="")
    rewind = sub.add_parser("rewind")
    rewind.add_argument("target")
    rewind.add_argument("--reason", default="")
    sub.add_parser("can-ask")
    sub.add_parser("save")

    args = parser.parse_args(argv)
    state = RalphState.load(args.project_root, args.session)

    if args.cmd == "show":
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "init":
        ns = new_state(args.idea, session=args.session, deadline=args.deadline)
        ns.save(args.project_root)
        print(json.dumps(ns.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "transition":
        try:
            state.transition(args.target, action=args.action)
        except RalphStateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        state.save(args.project_root)
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "rewind":
        try:
            state.rewind_to(args.target, reason=args.reason)
        except RalphStateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        state.save(args.project_root)
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "can-ask":
        if state.can_ask_question():
            print("yes")
            return 0
        print("no")
        return 1
    if args.cmd == "save":
        state.save(args.project_root)
        print(state._state_path(args.project_root, args.session))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
