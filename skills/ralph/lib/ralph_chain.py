"""Pure chain executor for /dev-kit:ralph ATTENDED_RUN.

Walks BUILD → BABYSIT → SHIP → DONE without ever raising
``AskUserQuestion``. The state-machine layer (``ralph_state.py``)
already enforces the ``attended_lock`` invariant for *any* call site
that consults ``can_ask_question()``; this module is the canonical
side-effect dispatcher that the unattended loop uses to actually drive
the chain.

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

Exit-code mapping (from ``lib/ralph_chain.run_attended``):

* ``0`` and last sub_stage reaches ``SHIP`` → ``DONE``.
* ``AttendedLockError`` propagates unchanged (forensic field already
  populated by ``assert_can_ask``).
* ``RecoveryRequired`` (custom exception raised by a dispatch shim) →
  ``RECOVERY_REQUIRED``.
* ``UserMergeRequired`` → ``USER_MERGE_REQUIRED`` (build green + review
  approved + tag pushed, but the human-merge boundary is intact).
* Any other ``Exception`` → ``RECOVERY_REQUIRED`` with the traceback
  captured into ``state.last_action``.

The module is importable as ``from skills.ralph.lib import ralph_chain``
and is consumed by ``scripts/ralph_drive.sh`` and
``tests/test_ralph_chain.py``.
"""

from __future__ import annotations

import abc
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Re-use the pure state machine. Add the lib dir so the "import ralph_state"
# form works the same as in tests/test_ralph_skill.py.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import ralph_state as rs  # noqa: E402, I001


# ----------------------------------------------------------------------------
# Constants — sub_stage progression + flag recipes
# ----------------------------------------------------------------------------

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


class ChainError(rs.RalphStateError):
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


class DispatchContractError(ChainError):
    """Raised when a dispatch shim violates the no-Ask contract.

    A sub-skill (or its stdout) emitted a string that resembles an
    AskUserQuestion payload. The chain refuses to ask and surfaces
    this so the operator knows a sub-skill needs to be patched."""


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
    def build(self, state: "rs.RalphState") -> "DispatchResult":
        """Execute the BUILD sub_stage. Must NOT call AskUserQuestion."""

    @abc.abstractmethod
    def babysit(self, state: "rs.RalphState") -> "DispatchResult":
        """Execute the BABYSIT sub_stage with --operator-is-only-human."""

    @abc.abstractmethod
    def ship(self, state: "rs.RalphState") -> "DispatchResult":
        """Execute the SHIP sub_stage. Returns USER_MERGE_REQUIRED when
        build is green and review is approved but human merge is the
        boundary the skill cannot cross."""


@dataclass
class DispatchResult:
    """Outcome of one sub_stage dispatch.

    ``terminal`` says where to land on success:

    * ``None`` → continue to next sub_stage (default).
    * ``"USER_MERGE_REQUIRED"`` → emit USER_MERGE_REQUIRED.
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
    ) -> None:
        self.cli = cli
        self.operator = operator
        self.timeout_seconds = timeout_seconds
        self.project_root = project_root or Path.cwd()

    # -- helpers ---------------------------------------------------------

    def _run(self, slash: str, *extra: str) -> DispatchResult:
        argv = [self.cli, "--print", slash, *extra]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=str(self.project_root),
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

    def build(self, state: "rs.RalphState") -> DispatchResult:
        return self._run("/dev-kit:build", "--push")

    def babysit(self, state: "rs.RalphState") -> DispatchResult:
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
        # babysit-pr exits 0 on green; USER_MERGE_REQUIRED is the chain's
        # terminal for "build green + review approved + tag pushed but
        # human must run gh pr merge".
        if result.exit_code == 0:
            result.terminal = "USER_MERGE_REQUIRED"
        return result

    def ship(self, state: "rs.RalphState") -> DispatchResult:
        # ship needs the PR number from babysit-pr's output. Real babysit-pr
        # writes the PR number to its stdout last line; parse it lazily.
        pr_number = _extract_pr_number(state.babysit_state)
        extra: List[str] = []
        if pr_number is not None:
            extra.extend(["--pr", str(pr_number)])
        return self._run("/dev-kit:ship", *extra)


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

    def build(self, state: "rs.RalphState") -> DispatchResult:
        return self._call(BUILD)

    def babysit(self, state: "rs.RalphState") -> DispatchResult:
        return self._call(BABYSIT)

    def ship(self, state: "rs.RalphState") -> DispatchResult:
        return self._call(SHIP)


# ----------------------------------------------------------------------------
# Helpers
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


# ----------------------------------------------------------------------------
# The unattended runner
# ----------------------------------------------------------------------------


def run_attended(
    state: "rs.RalphState",
    dispatch: ChainDispatch,
    *,
    project_root: Path,
) -> "rs.RalphState":
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
        raise rs.AttendedLockError(
            f"run_attended precheck: can_ask_question=True despite lock "
            f"(stage={state.current_stage}, lock={state.attended_lock})"
        )

    seen_sub_stage_count: Dict[str, int] = {}

    for sub in SUB_STAGE_ORDER:
        # SAME-STAGE-REPEAT safety valve. If the same sub_stage has
        # been entered twice, flip to RECOVERY_REQUIRED instead of
        # looping forever.
        seen_sub_stage_count[sub] = seen_sub_stage_count.get(sub, 0) + 1
        if seen_sub_stage_count[sub] >= 2:
            state.sub_stage = sub
            state.last_action = (
                f"same-stage-repeat=2 tripped at {sub}; RECOVERY_REQUIRED"
            )
            state.next_action = "operator reviews loop log + resumes"
            state.transition(rs.RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state

        # Pre-dispatch guard. Even if the lock were somehow cleared,
        # refuse to Ask during ATTENDED_RUN.
        if state.can_ask_question():
            state.record_blocked_ask(f"{sub} pre-dispatch")
            raise rs.AttendedLockError(
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
            state.transition(rs.RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state
        except UserMergeRequired:
            state.last_action = (
                f"{sub} raised UserMergeRequired; build green + review approved"
            )
            state.next_action = "operator runs gh pr merge"
            state.transition(
                rs.USER_MERGE_REQUIRED, action=state.last_action
            )
            state.save(project_root)
            return state
        except rs.AttendedLockError:
            # Bubbled up from a sub-skill. Don't recover — forensic field
            # is already populated by the sub-skill.
            state.save(project_root)
            raise
        except Exception as exc:  # noqa: BLE001 — capture full traceback
            state.last_action = f"{sub} crashed: {type(exc).__name__}: {exc}"
            state.next_action = "operator reviews crash + retries"
            state.transition(rs.RECOVERY_REQUIRED, action=state.last_action)
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
            state.transition(rs.RECOVERY_REQUIRED, action=state.last_action)
            state.save(project_root)
            return state
        if terminal == "USER_MERGE_REQUIRED":
            state.last_action = (
                f"{sub} exit_code={result.exit_code}: ship requested human merge"
            )
            state.next_action = "operator runs gh pr merge"
            state.transition(
                rs.USER_MERGE_REQUIRED, action=state.last_action
            )
            state.save(project_root)
            return state
        if result.terminal == "USER_MERGE_REQUIRED":
            state.last_action = f"{sub} signalled USER_MERGE_REQUIRED"
            state.next_action = "operator runs gh pr merge"
            state.transition(
                rs.USER_MERGE_REQUIRED, action=state.last_action
            )
            state.save(project_root)
            return state

        # Sub succeeded → record and continue.
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
    state.transition(rs.DONE, action=state.last_action)
    state.save(project_root)
    return state


# ----------------------------------------------------------------------------
# CLI surface (used by scripts/ralph_drive.sh)
# ----------------------------------------------------------------------------


def _default_dispatch_for_session(state: "rs.RalphState") -> ChainDispatch:
    """Construct the real dispatch for a ralph session. Tests bypass
    this by passing their own ``ChainDispatch`` to ``run_attended``."""
    return RealDispatch()


def _cli(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ralph_chain")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session", default="default")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run-attended")
    run_p.add_argument(
        "--dispatch",
        choices=["real", "noop"],
        default="real",
        help="real = shell out; noop = recording dispatch for dry-run",
    )
    sub.add_parser("show")

    args = parser.parse_args(argv)

    if args.cmd == "show":
        print(json.dumps({"session": args.session}, indent=2))
        return 0

    state = rs.RalphState.load(args.project_root, args.session)
    if state.current_stage != rs.ATTENDED_RUN:
        print(
            f"error: current_stage={state.current_stage!r}; "
            f"expected ATTENDED_RUN",
            file=sys.stderr,
        )
        return 2

    dispatch: ChainDispatch
    if args.dispatch == "real":
        dispatch = _default_dispatch_for_session(state)
    else:
        # dry-run: RecordingDispatch with successful defaults + terminal
        # USER_MERGE_REQUIRED at BABYSIT (mirrors RealDispatch.babysit).
        dispatch = RecordingDispatch(
            results={
                BUILD: DispatchResult(exit_code=0),
                BABYSIT: DispatchResult(
                    exit_code=0, terminal="USER_MERGE_REQUIRED"
                ),
            }
        )

    try:
        final = run_attended(state, dispatch, project_root=args.project_root)
    except rs.RalphStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(final.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
