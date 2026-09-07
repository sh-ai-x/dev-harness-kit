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


# ----------------------------------------------------------------------------
# State record
# ----------------------------------------------------------------------------


@dataclass
class RalphState:
    """Mutable in-memory state. Persist via ``save()`` after every mutation."""

    session: str = "default"
    started_at: str = ""
    idea: str = ""
    current_stage: str = RESEARCH_GATE
    sub_stage: str = "AWAITING_USER"
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
        # Reset next_action — caller is expected to populate.
        if not self.next_action:
            self.next_action = f"enter {target}"

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
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in valid})

    def save(self, project_root: Path) -> Path:
        path = self._state_path(project_root, self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, payload)
        return path

    @classmethod
    def load(cls, project_root: Path, session: str = "default") -> "RalphState":
        path = cls._state_path(project_root, session)
        if not path.exists():
            return cls(session=session)
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _state_path(project_root: Path, session: str) -> Path:
        safe = "".join(c for c in session if c.isalnum() or c in "-_") or "default"
        return project_root / ".dev-kit" / "ralph" / f"{safe}.json"


# ----------------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------------


def new_state(idea: str, *, session: str = "default") -> RalphState:
    return RalphState(
        session=session,
        started_at=_now_iso(),
        idea=idea,
        current_stage=RESEARCH_GATE,
        sub_stage="AWAITING_USER",
        last_action=f"init at {RESEARCH_GATE}",
        next_action=f"enter {RESEARCH_GATE}",
    )


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
        ns = new_state(args.idea, session=args.session)
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
