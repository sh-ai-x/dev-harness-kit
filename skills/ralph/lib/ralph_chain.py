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
* ``UserMergeRequired`` → ``USER_MERGE_REQUIRED`` only from ``SHIP`` (build
  green + review approved + tag pushed, but the human-merge boundary is
  intact). A child that reports this terminal earlier is rejected and lands
  in ``RECOVERY_REQUIRED``.
* Any other ``Exception`` → ``RECOVERY_REQUIRED`` with the traceback
  captured into ``state.last_action``.

The module is importable as ``from skills.ralph.lib import ralph_chain``
and is consumed by ``scripts/ralph_drive.sh`` and
``tests/test_ralph_chain.py``.
"""

from __future__ import annotations

import abc
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Re-use the pure state machine. Add the lib dir so the "import ralph_state"
# form works the same as in tests/test_ralph_skill.py.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import ralph_state as rs  # noqa: E402, I001

try:  # Works both as a package import and in the legacy top-level test path.
    from ralph_events import RalphEventAdapter
except ImportError:  # pragma: no cover - package import fallback
    from skills.ralph.lib.ralph_events import RalphEventAdapter
from lib.context_budget import redact_excerpt  # noqa: E402, I001
from lib.trace_log import resolve_trace_root  # noqa: E402, I001


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

# Public CLI semantics: 0 is a completed run, 4 is the expected human
# merge hand-off, 5 is recoverable/unknown execution, and 2/3 remain usage
# and environment/observability errors respectively.
EXIT_DONE = 0
EXIT_USER_MERGE_REQUIRED = 4
EXIT_RECOVERY_REQUIRED = 5
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3

MAX_EVIDENCE_CHARS = 4096
MAX_PROGRESS_CHARS = 64 * 1024
MAX_FAILURE_LOG_BYTES = 64 * 1024


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
        """Execute BABYSIT; success must continue to the SHIP sub_stage."""

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
    * ``"USER_MERGE_REQUIRED"`` → emit USER_MERGE_REQUIRED, but only when
      returned by the SHIP sub_stage.
    """

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    terminal: Optional[str] = None
    pr_number: Optional[int] = None
    failure_class: Optional[str] = None
    artifact_refs: List[str] = field(default_factory=list)
    resume_allowed: Optional[bool] = None
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
        return result

    def ship(self, state: "rs.RalphState") -> DispatchResult:
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


def _record_completed_sub_stage(state: "rs.RalphState", sub_stage: str) -> None:
    """Record a successful boundary once, preserving execution order."""
    if sub_stage not in state.completed_sub_stages:
        state.completed_sub_stages.append(sub_stage)
    state.last_completed_sub_stage = sub_stage


def _bounded(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    """Return a bounded, minimally redacted evidence excerpt."""
    value = text or ""
    for marker in ("token=", "TOKEN=", "Authorization:", "authorization:"):
        while marker in value:
            start = value.find(marker) + len(marker)
            end = value.find("\n", start)
            if end == -1:
                end = len(value)
            value = value[:start] + "[REDACTED]" + value[end:]
    return value[:limit]


def _artifact_path(project_root: Path, relative: str) -> Path:
    """Resolve a Ralph artifact and reject paths outside the project root."""
    root = project_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ChainError(f"Ralph artifact escapes project root: {relative!r}") from exc
    return path


def _event_path(state: "rs.RalphState", project_root: Path) -> Path:
    state.prepare_artifact_paths(project_root)
    if state.event_log_path == str(Path(".dev-kit") / "trace" / "events.jsonl"):
        return resolve_trace_root(project_root) / ".dev-kit" / "trace" / "events.jsonl"
    return _artifact_path(project_root, state.event_log_path)


def _read_events(state: "rs.RalphState", project_root: Path) -> List[Dict[str, Any]]:
    path = _event_path(state, project_root)
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainError(f"Ralph event log corrupt at line {line_no}: {exc}") from exc
        if isinstance(event, dict):
            # The repository-wide trace stream also contains hook and other
            # workflow events. A Ralph checkpoint/report must be scoped to
            # this run, otherwise another session can advance the sequence or
            # make an unrelated stage look completed.
            if event.get("run_id") == state.run_id:
                events.append(event)
    return events


def _append_failure_log(
    state: "rs.RalphState", project_root: Path, event: Dict[str, Any]
) -> None:
    """Persist a bounded, human-readable failure record beside the state.

    The event journal remains canonical for joins and metrics. This sidecar is
    intentionally a small forensic view so an operator can inspect stderr
    without searching a large trace stream; it never acts as completion
    evidence.
    """
    if not event.get("failure_class") and event.get("outcome") not in {
        "failure", "recovery", "interrupted"
    }:
        return
    path = _artifact_path(project_root, state.failure_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence = event.get("evidence_ref") or {}
    failure_class = event.get("failure_class") or evidence.get("failure_class", "")
    record = {
        "schema_version": 1,
        "event_id": event.get("event_id", ""),
        "run_id": state.run_id,
        "attempt_id": event.get("attempt_id", ""),
        "stage": event.get("stage", rs.ATTENDED_RUN),
        "sub_stage": event.get("stage_id", event.get("stage", "")),
        "event_type": event.get("event_type", ""),
        "outcome": event.get("outcome", ""),
        "failure_class": redact_excerpt(failure_class, limit=256),
        "stderr": redact_excerpt(evidence.get("stderr", ""), limit=MAX_EVIDENCE_CHARS),
        "ts": event.get("ts", rs._now_iso()),
        "evidence_ref": {"event_id": event.get("event_id", "")},
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    # Keep the sidecar bounded. It is diagnostic only, so an old log is
    # rotated by truncating to the newest record rather than blocking the
    # lifecycle journal.
    try:
        existing = path.read_bytes() if path.exists() else b""
        records = [item for item in existing.decode("utf-8", errors="replace").splitlines() if item]
        records.append(line.rstrip("\n"))
        while records and len(("\n".join(records) + "\n").encode("utf-8")) > MAX_FAILURE_LOG_BYTES:
            records.pop(0)
        payload = ("\n".join(records) + "\n").encode("utf-8")
        rs._atomic_write_text(path, payload.decode("utf-8", errors="replace"))
    except OSError:
        # The canonical event has already been persisted. The controller
        # retains the failure outcome; the missing sidecar is visible via
        # the event's evidence and must not be treated as success.
        return


def _append_event(
    state: "rs.RalphState",
    project_root: Path,
    event_type: str,
    *,
    attempt_id: str = "",
    sub_stage: str = "",
    outcome: str = "",
    exit_code: Optional[int] = None,
    failure_class: str = "",
    stdout: str = "",
    stderr: str = "",
    duration_ms: Optional[int] = None,
    artifact_refs: Optional[List[str]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Append one bounded lifecycle event in the canonical trace journal."""
    path = _event_path(state, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = _read_events(state, project_root)
    previous_seq = state.checkpoint_seq
    highest_seq = max(
        (int((event.get("evidence_ref") or {}).get("checkpoint_seq", 0)) for event in events),
        default=0,
    )
    sequence = max(previous_seq, highest_seq) + 1
    attempt = attempt_id or str(state.active_attempt.get("attempt_id", "")) or f"{state.run_id}:run"
    stage = sub_stage or state.sub_stage or rs.ATTENDED_RUN
    evidence: Dict[str, Any] = {
        "checkpoint_seq": sequence,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "failure_class": failure_class,
        "stdout_ref": state.event_log_path if stdout else "",
        "stderr_ref": state.failure_log_path if stderr else "",
        "stdout": _bounded(stdout),
        "stderr": _bounded(stderr),
        "artifact_refs": (artifact_refs or [])[:32],
        "policy": {"owner": "ralph", "fail_closed": True},
        **extra,
    }
    adapter = RalphEventAdapter(
        project_root,
        run_id=state.run_id,
        attempt_id=attempt,
        workflow_id="ralph",
        source="ralph:chain",
    )
    emission = adapter.emit(
        stage,
        event_type,
        outcome or "unknown",
        subject_id=f"{state.run_id}:{stage}",
        attempt_id=attempt,
        parent_id=state.last_event_id or None,
        evidence_ref=evidence,
        idempotency_key=f"{state.run_id}:event:{sequence}",
    )
    if not emission.evidence_available:
        state.checkpoint_seq = previous_seq
        raise ChainError(
            f"Ralph event persistence degraded for {event_type}: "
            f"{emission.error or 'unknown storage error'}"
        )
    event = dict(emission.record)
    state.checkpoint_seq = sequence
    state.last_event_id = str(event["event_id"])
    _append_failure_log(state, project_root, event)
    return event


def _render_progress(state: "rs.RalphState", events: List[Dict[str, Any]]) -> str:
    """Render a compact operator view from the canonical event journal."""
    from skills.ralph.lib.ralph_metrics import reduce_metrics

    report = reduce_metrics(events)
    lines = [
        "# /dev-kit:ralph — derived progress",
        "",
        f"run_id: {state.run_id}",
        f"session: {state.session}",
        f"current_stage: {state.current_stage}",
        f"sub_stage: {state.sub_stage}",
        f"checkpoint_seq: {state.checkpoint_seq}",
        f"active_attempt: {state.active_attempt.get('attempt_id', '')}",
        f"terminal_reason: {state.terminal_reason}",
        f"recovery_reason: {state.recovery_reason}",
        f"next_action: {state.next_action}",
        f"metric_status: {report['status']}",
        f"metric_coverage: {report['coverage']}",
        "",
        "## Evidence journal",
        "",
    ]
    for event in events[-80:]:
        evidence = event.get("evidence_ref") or {}
        lines.append(
            "- "
            f"{event.get('event_id', '')} "
            f"{event.get('event_type', '')} "
            f"attempt={event.get('attempt_id', '')} "
            f"outcome={event.get('outcome', '')} "
            f"failure_class={event.get('failure_class') or evidence.get('failure_class', '')}"
        )
    return ("\n".join(lines) + "\n")[:MAX_PROGRESS_CHARS]


def _checkpoint(state: "rs.RalphState", project_root: Path) -> None:
    """Publish state after its preceding event is durable."""
    state.save(project_root)
    progress = _artifact_path(project_root, state.progress_path)
    rs._atomic_write_text(progress, _render_progress(state, _read_events(state, project_root)))


def _deadline_expired(state: "rs.RalphState") -> bool:
    if not state.deadline:
        return False
    try:
        deadline = datetime.fromisoformat(state.deadline.replace("Z", "+00:00"))
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline


def _failure_class(result: DispatchResult) -> str:
    if result.failure_class:
        return result.failure_class
    if result.exit_code == 124:
        return "child_timeout"
    if result.exit_code == 127:
        return "environment"
    if result.exit_code:
        return "child_exit"
    return ""

# ----------------------------------------------------------------------------
# Durable unattended runner
# ----------------------------------------------------------------------------


def _land_recovery(
    state: "rs.RalphState", project_root: Path, reason: str, *,
    failure_class: str = "unknown", attempt_id: str = "",
    resume_allowed: bool = True,
) -> "rs.RalphState":
    state.prepare_artifact_paths(project_root)
    state.last_action = reason
    state.recovery_reason = reason
    state.next_action = "operator reviews failure log + resumes"
    state.recovery_metadata.update({
        "failure_class": failure_class,
        "attempt_id": attempt_id,
        "resume_allowed": resume_allowed,
        "last_verified_checkpoint": state.checkpoint_seq,
        "failure_log": state.failure_log_path,
    })
    state.transition(rs.RECOVERY_REQUIRED, action=reason)
    _append_event(
        state, project_root, "recovery.required", attempt_id=attempt_id,
        sub_stage=state.sub_stage, outcome="recovery",
        failure_class=failure_class, stderr=reason,
    )
    _append_event(
        state, project_root, "terminal.reached", attempt_id=attempt_id,
        sub_stage=state.sub_stage, outcome=rs.RECOVERY_REQUIRED,
        failure_class=failure_class, terminal=rs.RECOVERY_REQUIRED,
    )
    _checkpoint(state, project_root)
    return state


def _land_user_merge(
    state: "rs.RalphState", project_root: Path, reason: str, attempt_id: str
) -> "rs.RalphState":
    state.last_action = reason
    state.next_action = "operator runs gh pr merge"
    state.transition(rs.USER_MERGE_REQUIRED, action=reason)
    _append_event(
        state, project_root, "terminal.reached", attempt_id=attempt_id,
        sub_stage=SHIP, outcome=rs.USER_MERGE_REQUIRED,
        terminal=rs.USER_MERGE_REQUIRED, failure_class="human_merge_boundary",
    )
    _checkpoint(state, project_root)
    return state


def _land_done(state: "rs.RalphState", project_root: Path) -> "rs.RalphState":
    state.last_action = "build green + review approved + tag pushed"
    state.next_action = "operator reviews final state"
    state.transition(rs.DONE, action=state.last_action)
    _append_event(
        state, project_root, "terminal.reached", sub_stage=SHIP,
        outcome=rs.DONE, terminal=rs.DONE,
    )
    _checkpoint(state, project_root)
    return state


def _start_attempt(
    state: "rs.RalphState", project_root: Path, sub: str
) -> tuple[str, str]:
    state.sub_stage = sub
    state.attempt_count += 1
    attempt_id = f"{state.run_id}:attempt:{state.attempt_count}"
    started_at = rs._now_iso()
    state.active_attempt = {
        "attempt_id": attempt_id,
        "stage": rs.ATTENDED_RUN,
        "sub_stage": sub,
        "started_at": started_at,
        "deadline": state.deadline,
        "attempt_number": state.attempt_count,
        "started_event_id": "",
        "dispatch_accepted": False,
        "started_monotonic": time.monotonic(),
    }
    started = _append_event(
        state, project_root, "stage.attempt.started", attempt_id=attempt_id,
        sub_stage=sub, outcome="started",
    )
    state.active_attempt["started_event_id"] = started["event_id"]
    _checkpoint(state, project_root)
    _append_event(
        state, project_root, "dispatch.accepted", attempt_id=attempt_id,
        sub_stage=sub, outcome="accepted", idempotency_key=attempt_id,
    )
    state.active_attempt["dispatch_accepted"] = True
    _checkpoint(state, project_root)
    return attempt_id, started_at


def _record_finish(
    state: "rs.RalphState", project_root: Path, result: DispatchResult,
    *, sub: str, attempt_id: str, started_at: str, outcome: str,
    failure_class: str = "", terminal: str = "",
) -> Dict[str, Any]:
    started_monotonic = state.active_attempt.get("started_monotonic", time.monotonic())
    duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))
    event = _append_event(
        state, project_root, "stage.attempt.finished", attempt_id=attempt_id,
        sub_stage=sub, outcome=outcome, exit_code=result.exit_code,
        failure_class=failure_class, stdout=result.stdout, stderr=result.stderr,
        duration_ms=duration_ms, artifact_refs=result.artifact_refs,
        terminal=terminal, notes=result.notes,
    )
    summary = {
        "attempt_id": attempt_id,
        "started_at": started_at,
        "finished_at": event["ts"],
        "event_id": event["event_id"],
        "outcome": outcome,
        "exit_code": result.exit_code,
        "duration_ms": duration_ms,
        "failure_class": failure_class,
        "artifact_refs": result.artifact_refs[:32],
    }
    state.stage_evidence.setdefault(sub, []).append(summary)
    state.stage_evidence[sub] = state.stage_evidence[sub][-20:]
    state.active_attempt.clear()
    if sub == BUILD:
        state.build_state = _bounded(result.stdout)
    elif sub == BABYSIT:
        state.babysit_state = _bounded(result.stdout)
    elif sub == SHIP:
        state.ship_state = _bounded(result.stdout)
    if result.pr_number is not None:
        state.babysit_state = f"{state.babysit_state}\nPR={result.pr_number}"
    return event


def _reconcile_active_attempt(
    state: "rs.RalphState", project_root: Path
) -> Optional["rs.RalphState"]:
    """Never infer success from an open attempt after process restart."""
    active = state.active_attempt
    if not active:
        return None
    attempt_id = str(active.get("attempt_id", ""))
    events = [
        event for event in _read_events(state, project_root)
        if event.get("attempt_id") == attempt_id
    ]
    finished = next(
        (event for event in events if event.get("event_type") == "stage.attempt.finished"),
        None,
    )
    if finished is not None:
        state.active_attempt.clear()
        state.sub_stage = str(finished.get("sub_stage") or state.sub_stage)
        if finished.get("outcome") == "success":
            _record_completed_sub_stage(state, state.sub_stage)
            _checkpoint(state, project_root)
            return None
        return _land_recovery(
            state, project_root, f"reconciled failed {state.sub_stage} attempt {attempt_id}",
            failure_class=str(finished.get("failure_class") or "child_exit"),
            attempt_id=attempt_id,
        )
    accepted = any(event.get("event_type") == "dispatch.accepted" for event in events)
    _append_event(
        state, project_root, "stage.attempt.interrupted", attempt_id=attempt_id,
        sub_stage=str(active.get("sub_stage") or state.sub_stage),
        outcome="interrupted", failure_class="dispatch_unknown" if accepted else "interrupted",
    )
    state.active_attempt.clear()
    if accepted:
        return _land_recovery(
            state, project_root, f"{state.sub_stage} attempt {attempt_id} interrupted after dispatch",
            failure_class="dispatch_unknown", attempt_id=attempt_id, resume_allowed=False,
        )
    state.retry_count += 1
    state.recovery_reason = f"{state.sub_stage} attempt interrupted before dispatch"
    state.recovery_metadata.update({
        "failure_class": "interrupted", "attempt_id": attempt_id, "resume_allowed": True,
    })
    _append_event(
        state, project_root, "retry.scheduled", attempt_id=attempt_id,
        sub_stage=state.sub_stage, outcome="retry", failure_class="interrupted",
    )
    _checkpoint(state, project_root)
    return None


@contextmanager
def _session_lease(project_root: Path, session: str):
    """Hold one process-wide lease and reclaim only dead-owner leases."""
    safe = "".join(c for c in session if c.isalnum() or c in "-_") or "default"
    lease_root = resolve_trace_root(project_root)
    lease_path = lease_root / ".dev-kit" / "ralph" / f"{safe}.lease"
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "session": session}) + "\n"
    fd: Optional[int] = None
    for _attempt in range(2):
        try:
            fd = os.open(str(lease_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, payload.encode("utf-8"))
            os.close(fd)
            fd = None
            break
        except FileExistsError:
            try:
                raw = json.loads(lease_path.read_text(encoding="utf-8"))
                owner_pid = int(raw.get("pid", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                owner_pid = 0
            owner_alive = False
            if owner_pid > 0:
                try:
                    os.kill(owner_pid, 0)
                    owner_alive = True
                except ProcessLookupError:
                    owner_alive = False
                except PermissionError:
                    owner_alive = True
                except OSError:
                    owner_alive = False
            if owner_alive:
                raise ChainError(f"Ralph session lease is held by pid {owner_pid}")
            try:
                lease_path.unlink()
            except FileNotFoundError:
                continue
    if fd is not None:
        os.close(fd)
    if not lease_path.exists():
        raise ChainError("unable to acquire Ralph session lease")
    try:
        yield lease_path
    finally:
        try:
            raw = json.loads(lease_path.read_text(encoding="utf-8"))
            if int(raw.get("pid", -1)) == os.getpid():
                lease_path.unlink()
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            pass


def _run_attended_impl(
    state: "rs.RalphState", dispatch: ChainDispatch, *, project_root: Path
) -> "rs.RalphState":
    """Drive the locked chain from the last durable verified boundary."""
    if state.current_stage == rs.RECOVERY_REQUIRED:
        state.resume_attended()
        _append_event(
            state, project_root, "retry.scheduled", sub_stage=state.sub_stage,
            outcome="resume", failure_class=str(state.recovery_metadata.get("failure_class", "")),
        )
        _checkpoint(state, project_root)
    if state.current_stage != rs.ATTENDED_RUN or not state.attended_lock:
        raise ChainError(
            "run_attended requires ATTENDED_RUN with attended_lock=True; "
            f"current state: {state.current_stage}"
        )
    if state.can_ask_question():
        state.record_blocked_ask("run_attended precheck")
        raise rs.AttendedLockError(
            f"run_attended precheck: can_ask_question=True despite lock "
            f"(stage={state.current_stage}, lock={state.attended_lock})"
        )
    try:
        state.validate()
        if not any(
            e.get("event_type") == "run.started" and e.get("run_id") == state.run_id
            for e in _read_events(state, project_root)
        ):
            _append_event(state, project_root, "run.started", outcome="started")
            _checkpoint(state, project_root)
        reconciled = _reconcile_active_attempt(state, project_root)
        if reconciled is not None:
            return reconciled
    except ChainError:
        raise
    except Exception as exc:  # corrupt journal/checkpoint is never success
        return _land_recovery(
            state, project_root, f"checkpoint reconciliation failed: {type(exc).__name__}: {exc}",
            failure_class="checkpoint_corrupt", resume_allowed=False,
        )

    seen: Dict[str, int] = {}
    for sub in SUB_STAGE_ORDER:
        seen[sub] = seen.get(sub, 0) + 1
        if seen[sub] >= 2:
            state.sub_stage = sub
            _append_event(
                state, project_root, "stage.attempt.finished", sub_stage=sub,
                outcome="failure", failure_class="no_progress",
            )
            return _land_recovery(
                state, project_root, f"same-stage-repeat=2 tripped at {sub}; RECOVERY_REQUIRED",
                failure_class="no_progress",
            )
        if sub in state.completed_sub_stages:
            continue
        if _deadline_expired(state):
            return _land_recovery(
                state, project_root, f"deadline exceeded before {sub}",
                failure_class="deadline_exceeded",
            )
        if state.can_ask_question():
            state.record_blocked_ask(f"{sub} pre-dispatch")
            raise rs.AttendedLockError(f"can_ask_question=True before {sub} dispatch")
        attempt_id, started_at = _start_attempt(state, project_root, sub)
        try:
            if sub == BUILD:
                result = dispatch.build(state)
            elif sub == BABYSIT:
                result = dispatch.babysit(state)
            elif sub == SHIP:
                result = dispatch.ship(state)
            else:  # pragma: no cover
                raise ChainError(f"unknown sub_stage: {sub}")
        except rs.AttendedLockError:
            _record_finish(
                state, project_root, DispatchResult(exit_code=2, failure_class="attended_lock"),
                sub=sub, attempt_id=attempt_id, started_at=started_at,
                outcome="failure", failure_class="attended_lock",
            )
            _checkpoint(state, project_root)
            raise
        except RecoveryRequired as exc:
            result = DispatchResult(
                exit_code=1,
                stderr=f"RecoveryRequired: {exc}",
                failure_class="child_recovery",
            )
        except UserMergeRequired as exc:
            result = DispatchResult(
                exit_code=0,
                stderr=f"UserMergeRequired: {exc}",
                terminal="USER_MERGE_REQUIRED",
                failure_class="human_merge_boundary",
            )
        except Exception:  # noqa: BLE001 - traceback is bounded into evidence
            result = DispatchResult(
                exit_code=1, stderr=traceback.format_exc(), failure_class="child_exception"
            )

        terminal = _coerce_recovery_or_merge(result, sub)
        if result.terminal == "USER_MERGE_REQUIRED":
            if sub != SHIP:
                terminal = "RECOVERY_REQUIRED"
                result.failure_class = "terminal_contract"
                result.stderr = (
                    f"{sub} returned USER_MERGE_REQUIRED before SHIP: "
                    f"{result.stderr}"
                )
            else:
                terminal = "USER_MERGE_REQUIRED"
        outcome = "failure" if terminal == "RECOVERY_REQUIRED" else (
            "human_merge" if terminal == "USER_MERGE_REQUIRED" else "success"
        )
        failure_class = _failure_class(result) or ("terminal_contract" if outcome == "failure" else "")
        _record_finish(
            state, project_root, result, sub=sub, attempt_id=attempt_id,
            started_at=started_at, outcome=outcome, failure_class=failure_class,
            terminal=terminal or "",
        )
        if terminal == "RECOVERY_REQUIRED":
            _checkpoint(state, project_root)
            detail = _bounded(result.stderr, 200).strip()
            if result.failure_class == "child_exception":
                detail = f"{sub} crashed: {result.stderr[-160:].strip()}"
            return _land_recovery(
                state, project_root, f"{sub} exit_code={result.exit_code}: {detail}",
                failure_class=failure_class or "child_exit", attempt_id=attempt_id,
                resume_allowed=result.resume_allowed is not False,
            )
        if terminal == "USER_MERGE_REQUIRED":
            _record_completed_sub_stage(state, sub)
            return _land_user_merge(
                state, project_root,
                f"{sub} signalled USER_MERGE_REQUIRED after verified ship boundary"
                f" ({result.stderr.strip()})",
                attempt_id,
            )
        _record_completed_sub_stage(state, sub)
        state.last_action = f"{sub} exit_code=0; continuing"
        next_index = SUB_STAGE_ORDER.index(sub) + 1
        state.next_action = (
            f"enter {SUB_STAGE_ORDER[next_index]}"
            if next_index < len(SUB_STAGE_ORDER) else "emit terminal"
        )
        _checkpoint(state, project_root)
    return _land_done(state, project_root)


def run_attended(
    state: "rs.RalphState", dispatch: ChainDispatch, *, project_root: Path
) -> "rs.RalphState":
    """Run one session under a crash-reclaimable per-session lease."""
    with _session_lease(project_root, state.session):
        return _run_attended_impl(state, dispatch, project_root=project_root)


# CLI surface (used by scripts/ralph_drive.sh)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------


def _default_dispatch_for_session(
    state: "rs.RalphState", *, project_root: Path
) -> ChainDispatch:
    """Construct the real dispatch for a ralph session. Tests bypass
    this by passing their own ``ChainDispatch`` to ``run_attended``.

    ``project_root`` is threaded explicitly so subprocess cwd is pinned
    to the repo root, never to the host's cwd. A CLI invocation from
    a non-repo directory (e.g. a script wrapper) would otherwise land
    ``git push`` / ``gh pr merge`` against the wrong remote.
    """
    return RealDispatch(project_root=project_root)


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
    sub.add_parser("events")
    metrics_p = sub.add_parser("metrics")
    metrics_p.add_argument("--format", choices=("json", "text"), default="json")
    sub.add_parser("status-report")

    args = parser.parse_args(argv)

    if args.cmd == "show":
        print(json.dumps({"session": args.session}, indent=2))
        return 0

    if args.cmd in {"events", "metrics", "status-report"}:
        from lib.trace_log import read_events
        from skills.ralph.lib.ralph_metrics import reduce_metrics

        state = rs.RalphState.load(args.project_root, args.session)
        events = [
            event for event in read_events(args.project_root)
            if event.get("run_id") == state.run_id
        ]
        if args.cmd == "events":
            print(json.dumps(events, indent=2, sort_keys=True))
            return 0
        report = reduce_metrics(events)
        if args.cmd == "metrics" and args.format == "text":
            print(f"status: {report['status']}")
            print(f"score: {report['score']}")
            print(f"events: {report['event_count']}")
            for name, metric in report["metrics"].items():
                print(
                    f"{name}: {metric['value']} "
                    f"({metric['numerator']}/{metric['denominator']}, "
                    f"coverage={metric['coverage']}, status={metric['status']})"
                )
        elif args.cmd == "status-report":
            print(json.dumps({"state": state.to_dict(), "metrics": report}, indent=2, sort_keys=True))
        else:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] in {"OK", "FAILED"} else EXIT_ENVIRONMENT

    state = rs.RalphState.load(args.project_root, args.session)
    if state.current_stage not in {rs.ATTENDED_RUN, rs.RECOVERY_REQUIRED}:
        print(
            f"error: current_stage={state.current_stage!r}; "
            f"expected ATTENDED_RUN or RECOVERY_REQUIRED",
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
    except rs.RalphStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(final.to_dict(), indent=2, sort_keys=True))
    # Preserve the long-standing 0 exit for the expected human-merge
    # hand-off. Recovery is the actionable non-success terminal and gets a
    # distinct code so shell callers can route it to resume/forensics.
    return EXIT_RECOVERY_REQUIRED if final.current_stage == rs.RECOVERY_REQUIRED else EXIT_DONE


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
