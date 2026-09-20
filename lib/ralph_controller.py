"""Thin, file-backed RALPH worker/session boundary adapter.

This module is deliberately smaller than a workflow engine.  The canonical
workflow state remains ``skills.ralph.lib.ralph_state``; this adapter records
the evidence needed to resume a disposable worker turn without turning Stop
or SessionEnd into workflow completion.

All writes are best-effort from hook callers.  A telemetry failure is a
visible error to the CLI but never a reason for a normal RALPH Stop to block.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from lib.trace_log import append_event, new_event_id
from skills.ralph.lib.ralph_state import RalphState

SCHEMA_VERSION = 1
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RalphControllerError(ValueError):
    """Raised when a checkpoint cannot be safely persisted."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_session(session: str) -> str:
    if not isinstance(session, str) or SESSION_RE.fullmatch(session) is None:
        raise RalphControllerError(
            "session must be one safe path segment using letters, digits, '.', '_' or '-'")
    return session


def _checkpoint_path(project_root: Path, session: str) -> Path:
    session = _safe_session(session)
    root = project_root.resolve()
    directory = root / ".dev-kit" / "ralph"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.checkpoints.jsonl"
    if path.is_symlink():
        raise RalphControllerError(f"refusing symlink checkpoint path: {path}")
    return path


def _append_record(project_root: Path, session: str, record: Dict[str, Any]) -> Path:
    path = _checkpoint_path(project_root, session)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return path


def _state_snapshot(project_root: Path, session: str) -> Dict[str, Any]:
    state = RalphState.load(project_root, session)
    return {
        "current_stage": state.current_stage,
        "sub_stage": state.sub_stage,
        "attended_lock": state.attended_lock,
        "iteration": state.iteration,
    }


def _trace_boundary(
    project_root: Path,
    *,
    session: str,
    event_type: str,
    outcome: str,
    evidence: Dict[str, Any],
) -> None:
    snapshot = _state_snapshot(project_root, session)
    append_event(
        project_root,
        {
            "event_id": new_event_id(),
            "run_id": f"ralph:{session}",
            "workflow_id": "ralph",
            "stage": snapshot["current_stage"],
            "event_type": event_type,
            "subject_id": f"worker:{session}",
            "parent_id": None,
            "ts": _now(),
            "outcome": outcome,
            "source": "ralph-controller",
            "evidence_ref": {**evidence, "state": snapshot},
        },
    )


def record_checkpoint(
    project_root: Path,
    *,
    session: str,
    reason: str = "worker_stop",
    next_action: str = "resume next cycle",
    payload_digest: str = "",
    hook_event: str = "Stop",
    candidate_id: str = "",
) -> Dict[str, Any]:
    """Record a resumable worker checkpoint without closing the workflow."""
    snapshot = _state_snapshot(project_root, session)
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "ralph.worker.checkpointed",
        "event_id": new_event_id(),
        "ts": _now(),
        "session": _safe_session(session),
        "reason": reason[:200],
        "next_action": next_action[:500],
        "hook_event": hook_event[:80],
        "payload_digest": payload_digest[:128],
        "candidate_id": candidate_id[:128],
        "state": snapshot,
    }
    path = _append_record(project_root, session, record)
    _trace_boundary(
        project_root,
        session=session,
        event_type="ralph.worker.checkpointed",
        outcome="checkpointed",
        evidence={
            "checkpoint_path": str(path.relative_to(project_root.resolve())),
            "checkpoint_id": record["event_id"],
            "reason": record["reason"],
            "next_action": record["next_action"],
            "hook_event": record["hook_event"],
            "payload_digest": record["payload_digest"],
        },
    )
    return record


def record_session_closed(
    project_root: Path,
    *,
    session: str,
    reason: str = "session_end",
    hook_event: str = "SessionEnd",
    outcome: str = "cancelled",
) -> Dict[str, Any]:
    """Record worker closure; deliberately never marks the workflow complete."""
    if outcome not in {"cancelled", "failed", "exception"}:
        raise RalphControllerError("worker session outcome must be cancelled, failed, or exception")
    snapshot = _state_snapshot(project_root, session)
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "ralph.worker.session_closed",
        "event_id": new_event_id(),
        "ts": _now(),
        "session": _safe_session(session),
        "reason": reason[:200],
        "hook_event": hook_event[:80],
        "outcome": outcome,
        "state": snapshot,
    }
    path = _append_record(project_root, session, record)
    _trace_boundary(
        project_root,
        session=session,
        event_type="ralph.worker.session_closed",
        outcome=outcome,
        evidence={
            "checkpoint_path": str(path.relative_to(project_root.resolve())),
            "session_event_id": record["event_id"],
            "reason": record["reason"],
            "hook_event": record["hook_event"],
            "workflow_completed": False,
        },
    )
    return record


def latest_checkpoint(project_root: Path, session: str) -> Optional[Dict[str, Any]]:
    """Return the latest valid checkpoint, or ``None`` when no checkpoint exists."""
    path = _checkpoint_path(project_root, session)
    if not path.is_file():
        return None
    latest: Optional[Dict[str, Any]] = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION:
            latest = value
    return latest


def _cli(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser(prog="ralph_controller")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session", default="default")
    sub = parser.add_subparsers(dest="command", required=True)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--reason", default="worker_stop")
    checkpoint.add_argument("--next-action", default="resume next cycle")
    checkpoint.add_argument("--payload-digest", default="")
    checkpoint.add_argument("--hook-event", default="Stop")
    checkpoint.add_argument("--candidate-id", default="")

    closed = sub.add_parser("session-closed")
    closed.add_argument("--reason", default="session_end")
    closed.add_argument("--hook-event", default="SessionEnd")
    closed.add_argument("--outcome", default="cancelled", choices=["cancelled", "failed", "exception"])
    sub.add_parser("resume")

    args = parser.parse_args(list(argv))
    try:
        if args.command == "checkpoint":
            value = record_checkpoint(
                args.project_root,
                session=args.session,
                reason=args.reason,
                next_action=args.next_action,
                payload_digest=args.payload_digest,
                hook_event=args.hook_event,
                candidate_id=args.candidate_id,
            )
        elif args.command == "session-closed":
            value = record_session_closed(
                args.project_root,
                session=args.session,
                reason=args.reason,
                hook_event=args.hook_event,
                outcome=args.outcome,
            )
        else:
            value = latest_checkpoint(args.project_root, args.session) or {}
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except (OSError, RalphControllerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
