"""Pure chain executor for /dev-kit:ralph ATTENDED_RUN.

Walks BUILD → BABYSIT → SHIP → DONE without ever raising
``AskUserQuestion``. The state-machine layer (``RalphState`` inlined
below) enforces the ``attended_lock`` invariant for *any* call site
that consults ``can_ask_question()``; this module is the canonical
side-effect dispatcher that the unattended loop uses to actually drive
the chain.

Inlined from ``lib/ralph_state.py`` (collapsed in the
refactor/ralph-babysit-collapse branch — the chain executor is the
only lib/ consumer of the state machine, and the four CLI
subcommands shipped by the state module move here under ``state
<subcommand>``). External callers resolve both via this module:

    from lib.ralph_chain import RalphState, ATTENDED_RUN, run_attended
    python3 -m lib.ralph_chain state show --project-root . --session X
    python3 -m lib.ralph_chain run-attended --project-root . --session X

Design invariants
-----------------
* **Pure dispatch.** Side-effects are injected via ``ChainDispatch``
  so tests can run the full chain without spawning ``Skill`` /
  ``subprocess``. The default ``RealDispatch`` calls
  ``subprocess.run`` against the underlying ``/dev-kit:<skill>`` CLI
  surface.
* **Mechanical Ask-refusal.** Before every dispatch, the executor
  asserts ``state.can_ask_question()`` is False (the lock is set) and
  refuses to ask even if a sub-skill signals ambiguity. The contract
  is "babysit-pr / build / ship MUST NOT emit a human-facing question
  during ATTENDED_RUN; the chain auto-decides instead."
* **babysit-pr flag passthrough is deterministic.** The BABYSIT step
  always invokes ``babysit-pr --operator-is-only-human --rationale
  "ralph-session=<id> unattended after SHIP_CONFIRM_GATE"``. This is
  the only flag combination that enables the unattended repair loop
  per ``skills/babysit-pr/SKILL.md:71-74``.
* **Same-stage-repeat=2 safety valve.** Re-entering the same sub_stage
  twice (e.g. BABYSIT re-entered because babysit-pr emitted a 'continue'
  with no progress) flips the chain to ``RECOVERY_REQUIRED`` so an
  operator can intervene instead of looping forever.

Exit-code mapping (from ``run_attended``):

* ``0`` and last sub_stage reaches ``SHIP`` → ``DONE``.
* ``AttendedLockError`` propagates unchanged (forensic field already
  populated by ``assert_can_ask``).
* ``RecoveryRequired`` (custom exception raised by a dispatch shim) →
  ``RECOVERY_REQUIRED``.
* ``UserMergeRequired`` → ``USER_MERGE_REQUIRED`` only from ``SHIP`` (build
  green + review approved + tag pushed, but the human-merge boundary is
  intact). A child that reports this terminal earlier is rejected and lands
  in ``RECOVERY_REQUIRED``.
* Any other ``Exception`` → ``RECOVERY_REQUIRED`` with the traceback
  captured into ``state.last_action``.

Consumed by ``skills/ralph/scripts/ralph_drive.sh`` and
``tests/test_ralph_chain.py``.
"""

from __future__ import annotations

import abc
import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ============================================================================
# State machine — inlined from lib/ralph_state.py (collapsed here).
# ============================================================================

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


class RalphStateError(Exception):
    """Raised on any invalid state-machine operation."""


class AttendedLockError(RalphStateError):
    """Raised when an AskUserQuestion is attempted during ATTENDED_RUN."""


class InvalidTransitionError(RalphStateError):
    """Raised when ``transition()`` is asked for an edge that does not exist."""


@dataclass
class RalphState:
    """Mutable in-memory state. Persist via ``save()`` after every mutation."""

    session: str = "default"
    started_at: str = ""
    idea: str = ""
    current_stage: str = RESEARCH_GATE
    sub_stage: str = "AWAITING_USER"
    # Ordered evidence of sub-stages that completed successfully during the
    # current attended run. This is deliberately separate from ``sub_stage``:
    # the latter is the cursor, while this list proves which boundaries were
    # actually crossed before a terminal state was emitted.
    completed_sub_stages: List[str] = field(default_factory=list)
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
            self.completed_sub_stages.clear()
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
        self.completed_sub_stages.clear()
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
# State module-level helpers
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


# ============================================================================
# Chain executor (original ralph_chain.py contents)
# ============================================================================

BUILD = "BUILD"
BABYSIT = "BABYSIT"
SHIP = "SHIP"

SUB_STAGE_ORDER: List[str] = [BUILD, BABYSIT, SHIP]

# Template the chain hands to babysit-pr so the unattended repair loop
# actually engages. Per skills/babysit-pr/SKILL.md:71-74 the default
# path is the human-gate; the bypass requires BOTH flags.
BABYSIT_RATIONALE_TEMPLATE = (
    "ralph-session={session} unattended after SHIP_CONFIRM_GATE approve; "
    "operator={operator}"
)

# Error categories raised by dispatch shims (RealDispatch maps them from
# subprocess exit codes).
RECOVERY_EXIT_CODES = {1, 2, 3}      # build failure / state-machine reject / env error
USER_MERGE_EXIT_CODE = 4              # ship pre-condition failed but build green


# ----------------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------------


class ChainError(RalphStateError):
    """Base class for ralph_chain errors. Inherits RalphStateError so
    callers that already catch RalphStateError keep working."""


class RecoveryRequired(ChainError):
    """Raised when the chain must terminate at RECOVERY_REQUIRED.

    Typical causes: build 3-cycle self-fix fired, babysit-pr hit
    MAX_ITERS, sub-skill crashed, same-stage-repeat=2 guard tripped."""


class UserMergeRequired(ChainError):
    """Raised when the chain reached the human-merge boundary.

    Build is green and review verdict = Approve, but the merge to
    ``main`` must remain a human action. This is the expected terminal
    for an unattended run on a single-operator repo where the human
    is asleep."""


# ----------------------------------------------------------------------------
# Dispatch contract
# ----------------------------------------------------------------------------


class ChainDispatch(abc.ABC):
    """Injectable side-effects for the unattended chain.

    The default ``RealDispatch`` shells out to ``claude`` invoking the
    slash command. Tests inject ``RecordingDispatch`` or raise-flavoured
    variants to pin the contract.
    """

    @abc.abstractmethod
    def build(self, state: RalphState) -> "DispatchResult":
        """Execute the BUILD sub_stage. Must NOT call AskUserQuestion."""

    @abc.abstractmethod
    def babysit(self, state: RalphState) -> "DispatchResult":
        """Execute BABYSIT; success must continue to the SHIP sub_stage."""

    @abc.abstractmethod
    def ship(self, state: RalphState) -> "DispatchResult":
        """Execute the SHIP sub_stage. Returns USER_MERGE_REQUIRED when
        build is green and review is approved but human merge is the
        boundary the skill cannot cross."""


@dataclass
class DispatchResult:
    """Outcome of one sub_stage dispatch.

    ``terminal`` says where to land on success:

    * ``None`` → continue to next sub_stage (default).
    * ``"USER_MERGE_REQUIRED"`` → emit USER_MERGE_REQUIRED, but only when
      returned by the SHIP sub_stage.
    """

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    terminal: Optional[str] = None
    pr_number: Optional[int] = None
    notes: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Real dispatch — the default that shells out
# ----------------------------------------------------------------------------


class RealDispatch(ChainDispatch):
    """Default dispatcher. Shells out to ``claude`` invoking the skill
    by slash command. The exact command line is::

        claude --print /dev-kit:build
        claude --print /dev-kit:babysit-pr --operator-is-only-human --rationale "..."
        claude --print /dev-kit:ship --pr <N>

    ``--print`` is non-interactive (no AskUserQuestion surface), so even
    if the sub-skill tries to ask, the runtime will surface the question
    text on stderr instead of prompting the user. The chain contract
    additionally requires the state machine's ``assert_can_ask`` to
    refuse before this dispatch is reached — see ``run_attended`` below
    for the pre-dispatch guard.
    """

    def __init__(
        self,
        *,
        cli: str = "claude",
        operator: str = "ralph",
        timeout_seconds: int = 6 * 3600,
        project_root: Optional[Path] = None,
        session: str = "default",
    ) -> None:
        self.cli = cli
        self.operator = operator
        self.timeout_seconds = timeout_seconds
        self.project_root = project_root or Path.cwd()
        self.session = session

    # -- helpers ---------------------------------------------------------

    def _run(self, slash: str, *extra: str) -> DispatchResult:
        argv = [self.cli, "--print", slash, *extra]
        env = os.environ.copy()
        # Child Claude/Codex worker sessions must know that their Stop is a
        # turn boundary. The state-machine lock remains the authority; this
        # environment flag is only the thin hook adapter signal.
        env["RALPH_MODE"] = "1"
        env["RALPH_SESSION"] = self.session
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=str(self.project_root),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return DispatchResult(
                exit_code=124,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + f"\nchain: timeout after {self.timeout_seconds}s",
            )
        except FileNotFoundError as exc:
            return DispatchResult(
                exit_code=127,
                stdout="",
                stderr=f"chain: {self.cli} not found in PATH: {exc}",
            )
        return DispatchResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # -- ChainDispatch surface ------------------------------------------

    def build(self, state: RalphState) -> DispatchResult:
        return self._run("/dev-kit:build", "--push")

    def babysit(self, state: RalphState) -> DispatchResult:
        rationale = BABYSIT_RATIONALE_TEMPLATE.format(
            session=state.session,
            operator=self.operator,
        )
        result = self._run(
            "/dev-kit:babysit-pr",
            "--operator-is-only-human",
            "--rationale",
            rationale,
        )
        return result

    def ship(self, state: RalphState) -> DispatchResult:
        # ship needs the PR number from babysit-pr's output. Real babysit-pr
        # writes the PR number to its stdout last line; parse it lazily.
        pr_number = _extract_pr_number(state.babysit_state)
        extra: List[str] = []
        if pr_number is not None:
            extra.extend(["--pr", str(pr_number)])
        result = self._run("/dev-kit:ship", *extra)
        # /dev-kit:ship deliberately never auto-merges. A successful ship
        # therefore reaches the human merge boundary; this is the sole
        # production owner of USER_MERGE_REQUIRED.
        if result.exit_code == 0:
            result.terminal = "USER_MERGE_REQUIRED"
        return result


# ----------------------------------------------------------------------------
# Recording dispatch — for tests
# ----------------------------------------------------------------------------


class RecordingDispatch(ChainDispatch):
    """Test double. Each method appends to ``calls`` and returns the
    pre-loaded result from ``results`` (defaulting to a successful
    DispatchResult). Pass ``results`` as a dict keyed by sub_stage.
    """

    def __init__(
        self,
        *,
        results: Optional[Dict[str, DispatchResult]] = None,
        raise_map: Optional[Dict[str, BaseException]] = None,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.results = results or {}
        self.raise_map = raise_map or {}

    def _call(self, sub_stage: str) -> DispatchResult:
        self.calls.append({"sub_stage": sub_stage})
        if sub_stage in self.raise_map:
            raise self.raise_map[sub_stage]
        return self.results.get(sub_stage, DispatchResult())

    def build(self, state: RalphState) -> DispatchResult:
        return self._call(BUILD)

    def babysit(self, state: RalphState) -> DispatchResult:
        return self._call(BABYSIT)

    def ship(self, state: RalphState) -> DispatchResult:
        return self._call(SHIP)


# ----------------------------------------------------------------------------
# Chain helpers
# ----------------------------------------------------------------------------


def _extract_pr_number(text: str) -> Optional[int]:
    """Best-effort PR-number extractor. babysit-pr's stdout ends with
    ``PR=<N>`` on success; fall back to a ``gh pr view`` URL last
    segment if present."""
    if not text:
        return None
    for marker in ("PR=", "pr_number="):
        idx = text.find(marker)
        if idx == -1:
            continue
        tail = text[idx + len(marker):].strip()
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        if digits:
            try:
                return int(digits)
            except ValueError:
                pass
    return None


def _coerce_recovery_or_merge(
    result: DispatchResult, sub_stage: str
) -> Optional[str]:
    """Map a non-zero dispatch exit code to a terminal target. Returns
    None if the chain should continue."""
    if result.exit_code == 0:
        return None
    if sub_stage == SHIP and result.exit_code == USER_MERGE_EXIT_CODE:
        return "USER_MERGE_REQUIRED"
    return "RECOVERY_REQUIRED"


def _record_completed_sub_stage(state: RalphState, sub_stage: str) -> None:
    """Record a successful boundary once, preserving execution order."""
    if sub_stage not in state.completed_sub_stages:
        state.completed_sub_stages.append(sub_stage)


# ----------------------------------------------------------------------------
# The unattended runner
# ----------------------------------------------------------------------------


def run_attended(
    state: RalphState,
    dispatch: ChainDispatch,
    *,
    project_root: Path,
) -> RalphState:
    """Drive ATTENDED_RUN to a terminal state.

    Pre-conditions (caller enforces):

    * ``state.current_stage == ATTENDED_RUN`` and ``state.attended_lock``.
    * ``state.can_ask_question() is False`` (asserted again here).

    Post-conditions:

    * ``state`` is in a terminal stage (``DONE``, ``RECOVERY_REQUIRED``,
      or ``USER_MERGE_REQUIRED``).
    * ``state.last_action`` and ``state.next_action`` describe the
      terminal landing for forensics.
    * State file is persisted on every sub_stage transition via
      ``state.save(project_root)``.
    """
    if not state.attended_lock:
        raise ChainError(
            "run_attended requires attended_lock=True; "
            f"current state: {state.current_stage}"
        )
    # The mechanical Ask-refusal guard. Even if a sub-skill tries to ask,
    # we never want the LLM to halt ATTENDED_RUN with a question. Record
    # the forensic field and refuse.
    if state.can_ask_question():
        state.record_blocked_ask("run_attended precheck")
        raise AttendedLockError(
            f"run_attended precheck: can_ask_question=True despite lock "
            f"(stage={state.current_stage}, lock={state.attended_lock})"
        )

    seen_sub_stage_count: Dict[str, int] = {}

    for sub in SUB_STAGE_ORDER:
        # Same-stage-repeat=2 safety valve. When the orchestrator
        # re-enters the same sub_stage (e.g. a future babysit-pr
        # ``continue`` semantic re-iterating the BABYSIT phase), flip
        # to RECOVERY_REQUIRED instead of looping forever. Today the
        # chain iterates SUB_STAGE_ORDER exactly once, so this branch
        # is reachable only when a caller passes a custom SUB_STAGE_ORDER
        # that revisits a phase — see ``test_repeated_substage_visits_
        # land_recovery_required`` for the regression guard.
        seen_sub_stage_count[sub] = seen_sub_stage_count.get(sub, 0) + 1
        if seen_sub_stage_count[sub] >= 2:
            state.sub_stage = sub
            state.last_action = (
                f"same-stage-repeat=2 tripped at {sub}; RECOVERY_REQUIRED"
            )
            state.next_action = "operator reviews loop log + resumes"
            state.transition(RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state

        # Pre-dispatch guard. Even if the lock were somehow cleared,
        # refuse to Ask during ATTENDED_RUN.
        if state.can_ask_question():
            state.record_blocked_ask(f"{sub} pre-dispatch")
            raise AttendedLockError(
                f"can_ask_question=True before {sub} dispatch "
                f"(stage={state.current_stage})"
            )

        state.sub_stage = sub
        state.save(project_root)

        try:
            if sub == BUILD:
                result = dispatch.build(state)
            elif sub == BABYSIT:
                result = dispatch.babysit(state)
            elif sub == SHIP:
                result = dispatch.ship(state)
            else:  # pragma: no cover — defensive
                raise ChainError(f"unknown sub_stage: {sub}")
        except RecoveryRequired as exc:
            state.last_action = f"{sub} raised RecoveryRequired: {exc}"
            state.next_action = "operator resumes after manual fix"
            state.transition(RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state
        except UserMergeRequired:
            if sub != SHIP:
                state.last_action = (
                    f"{sub} raised UserMergeRequired before SHIP; "
                    "invalid terminal boundary"
                )
                state.next_action = "operator reviews child contract + resumes"
                state.transition(
                    RECOVERY_REQUIRED, action=state.last_action
                )
                state.save(project_root)
                return state
            _record_completed_sub_stage(state, sub)
            state.last_action = (
                f"{sub} raised UserMergeRequired; build green + review approved"
            )
            state.next_action = "operator runs gh pr merge"
            state.transition(
                USER_MERGE_REQUIRED, action=state.last_action
            )
            state.save(project_root)
            return state
        except AttendedLockError:
            # Bubbled up from a sub-skill. Don't recover — forensic field
            # is already populated by the sub-skill.
            state.save(project_root)
            raise
        except Exception as exc:  # noqa: BLE001 — capture full traceback
            state.last_action = f"{sub} crashed: {type(exc).__name__}: {exc}"
            state.next_action = "operator reviews crash + retries"
            state.transition(RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state

        # Persist sub-skill output for forensics + PR-number extraction.
        if sub == BUILD:
            state.build_state = result.stdout
        elif sub == BABYSIT:
            state.babysit_state = result.stdout
        elif sub == SHIP:
            state.ship_state = result.stdout
        if result.pr_number is not None:
            state.babysit_state = (
                f"{state.babysit_state}\nPR={result.pr_number}"
            )

        # Map exit code → terminal (None means continue to next sub).
        terminal = _coerce_recovery_or_merge(result, sub)
        if terminal == "RECOVERY_REQUIRED":
            state.last_action = (
                f"{sub} exit_code={result.exit_code}: {result.stderr.strip()[:200]}"
            )
            state.next_action = "operator investigates + retries"
            state.transition(RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state
        if terminal == "USER_MERGE_REQUIRED":
            state.last_action = (
                f"{sub} exit_code={result.exit_code}: ship requested human merge"
            )
            state.next_action = "operator runs gh pr merge"
            state.transition(
                USER_MERGE_REQUIRED, action=state.last_action
            )
            state.save(project_root)
            return state
        if result.terminal == "USER_MERGE_REQUIRED":
            if sub != SHIP:
                state.last_action = (
                    f"{sub} returned USER_MERGE_REQUIRED before SHIP; "
                    "invalid terminal boundary"
                )
                state.next_action = "operator reviews child contract + resumes"
                state.transition(
                    RECOVERY_REQUIRED, action=state.last_action
                )
                state.save(project_root)
                return state
            _record_completed_sub_stage(state, sub)
            state.last_action = f"{sub} signalled USER_MERGE_REQUIRED"
            state.next_action = "operator runs gh pr merge"
            state.transition(
                USER_MERGE_REQUIRED, action=state.last_action
            )
            state.save(project_root)
            return state

        # Sub succeeded → record and continue.
        _record_completed_sub_stage(state, sub)
        state.last_action = f"{sub} exit_code=0; continuing"
        try:
            next_sub = SUB_STAGE_ORDER[SUB_STAGE_ORDER.index(sub) + 1]
            state.next_action = f"enter {next_sub}"
        except IndexError:
            state.next_action = "emit terminal"
        state.save(project_root)

    # All three sub_stages green → DONE. In practice RealDispatch.babysit
    # flips to USER_MERGE_REQUIRED on success (babysit-pr never auto-merges),
    # so this branch is reached only when a custom dispatch yields a clean
    # exit through ship too.
    state.last_action = "build green + review approved + tag pushed"
    state.next_action = "operator reviews final state"
    state.transition(DONE, action=state.last_action)
    state.save(project_root)
    return state


# ----------------------------------------------------------------------------
# CLI surface (used by skills/ralph/scripts/ralph_drive.sh and the hook)
#
# Two top-level subcommands: `state <sub>` for the former ralph_state
# module surface (show / init / transition / rewind / can-ask / save);
# `run-attended` for the chain executor. The bare `show` from the old
# chain CLI is dropped — it only echoed the session name and added
# no forensic value.
# ----------------------------------------------------------------------------


def _default_dispatch_for_session(
    state: RalphState, *, project_root: Path
) -> ChainDispatch:
    """Construct the real dispatch for a ralph session. Tests bypass
    this by passing their own ``ChainDispatch`` to ``run_attended``.

    ``project_root`` is threaded explicitly so subprocess cwd is pinned
    to the repo root, never to the host's cwd. A CLI invocation from
    a non-repo directory (e.g. a script wrapper) would otherwise land
    ``git push`` / ``gh pr merge`` against the wrong remote.
    """
    return RealDispatch(project_root=project_root, session=state.session)


def _state_cli(state: RalphState, cmd: str, args: argparse.Namespace) -> int:
    """Dispatch the former ralph_state subcommands against a loaded state."""
    if cmd == "show":
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if cmd == "init":
        ns = new_state(args.idea, session=state.session)
        ns.save(args.project_root)
        print(json.dumps(ns.to_dict(), indent=2, sort_keys=True))
        return 0
    if cmd == "transition":
        try:
            state.transition(args.target, action=args.action)
        except RalphStateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        state.save(args.project_root)
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if cmd == "rewind":
        try:
            state.rewind_to(args.target, reason=args.reason)
        except RalphStateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        state.save(args.project_root)
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if cmd == "can-ask":
        if state.can_ask_question():
            print("yes")
            return 0
        print("no")
        return 1
    if cmd == "save":
        state.save(args.project_root)
        print(state._state_path(args.project_root, state.session))
        return 0
    print(f"error: unknown state subcommand {cmd!r}", file=sys.stderr)
    return 2


def _run_attended_cli(args: argparse.Namespace) -> int:
    state = RalphState.load(args.project_root, args.session)
    if state.current_stage != ATTENDED_RUN:
        print(
            f"error: current_stage={state.current_stage!r}; "
            f"expected ATTENDED_RUN",
            file=sys.stderr,
        )
        return 2
    if not state.attended_lock:
        print(
            "error: current_stage=ATTENDED_RUN but "
            "attended_lock=False (expected True). "
            "State may have been rewound out of the attended phase; "
            "refusing to dispatch.",
            file=sys.stderr,
        )
        return 2

    dispatch: ChainDispatch
    if args.dispatch == "real":
        dispatch = _default_dispatch_for_session(
            state, project_root=args.project_root
        )
    else:
        # dry-run: RecordingDispatch with successful defaults + terminal
        # USER_MERGE_REQUIRED at SHIP (mirrors RealDispatch.ship).
        dispatch = RecordingDispatch(
            results={
                BUILD: DispatchResult(exit_code=0),
                BABYSIT: DispatchResult(exit_code=0),
                SHIP: DispatchResult(
                    exit_code=0, terminal="USER_MERGE_REQUIRED"
                ),
            }
        )

    try:
        final = run_attended(state, dispatch, project_root=args.project_root)
    except RalphStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(final.to_dict(), indent=2, sort_keys=True))
    return 0


def _cli(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="ralph")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session", default="default")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # State subcommands (former ralph_state surface).
    state_p = sub.add_parser("state", help="state-machine operations (show / init / transition / rewind / can-ask / save)")
    state_sub = state_p.add_subparsers(dest="state_cmd", required=True)

    state_sub.add_parser("show")
    init_p = state_sub.add_parser("init")
    init_p.add_argument("idea")
    trans = state_sub.add_parser("transition")
    trans.add_argument("target")
    trans.add_argument("--action", default="")
    rewind = state_sub.add_parser("rewind")
    rewind.add_argument("target")
    rewind.add_argument("--reason", default="")
    state_sub.add_parser("can-ask")
    state_sub.add_parser("save")

    # Chain subcommand.
    run_p = sub.add_parser("run-attended", help="drive ATTENDED_RUN to a terminal state")
    run_p.add_argument(
        "--dispatch",
        choices=["real", "noop"],
        default="real",
        help="real = shell out; noop = recording dispatch for dry-run",
    )

    args = parser.parse_args(argv)

    if args.cmd == "state":
        state = RalphState.load(args.project_root, args.session)
        return _state_cli(state, args.state_cmd, args)

    if args.cmd == "run-attended":
        return _run_attended_cli(args)

    print(f"error: unknown command {args.cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
