"""Ralph evidence adapter.

The Ralph chain is allowed to continue when observability is unavailable.
This module therefore treats event collection as best effort, while making a
missing event explicit to the reducer instead of manufacturing a successful
outcome.

Events are persisted through :mod:`lib.trace_log`; the adapter only adds the
Ralph identity and context-budget boundary:

* ``run_id``, ``attempt_id`` and ``stage_id`` are present on every event.
* stdout/stderr/excerpts are redacted and bounded by the shared
  ``lib.context_budget`` contract.
* an idempotency key maps to one stable event id and one JSONL record.
* storage failures return a degraded emission result and never raise into the
  workflow's control path.
"""
from __future__ import annotations

import fcntl
import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ContextManager, Mapping, Optional

from lib.context_budget import (
    MAX_EVENT_BYTES,
    MAX_EXCERPT_CHARS,
    MAX_SUMMARY_CHARS,
    fingerprint,
    redact_excerpt,
    validate_handoff,
)
from lib.trace_log import append_event, now_utc, read_events, resolve_trace_root

EVENT_STATUS_RECORDED = "RECORDED"
EVENT_STATUS_DUPLICATE = "DUPLICATE"
EVENT_STATUS_DEGRADED = "DEGRADED"
OBSERVABILITY_OK = "OK"
OBSERVABILITY_DEGRADED = "DEGRADED"

_TRACE_EVENT_PATH = Path(".dev-kit") / "trace" / "events.jsonl"
_IDEMPOTENCY_LOCK_PATH = Path(".dev-kit") / "trace" / "events.idempotency.lock"
_FORBIDDEN_CONTEXT_KEYS = frozenset({"prompt", "prompts", "transcript", "transcripts", "messages"})


def _required_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must not contain newlines")
    return value.strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _bounded_text(value: Any, *, limit: int) -> tuple[str, bool, bool]:
    """Return ``(text, truncated, redacted)`` using the shared helper."""
    raw = _text(value)
    bounded = redact_excerpt(raw, limit=limit)
    return bounded, bounded.endswith("...[truncated]"), bounded != raw


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Redact free-form evidence without accepting transcript-shaped output."""
    if isinstance(value, str):
        return redact_excerpt(value, limit=MAX_SUMMARY_CHARS)
    if isinstance(value, bytes):
        return redact_excerpt(value.decode("utf-8", errors="replace"), limit=MAX_SUMMARY_CHARS)
    if depth >= 4:
        return redact_excerpt(value, limit=MAX_SUMMARY_CHARS)
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in _FORBIDDEN_CONTEXT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_excerpt(value, limit=MAX_SUMMARY_CHARS)


def _json_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _event_key(
    *,
    run_id: str,
    attempt_id: str,
    stage_id: str,
    event_type: str,
    subject_id: str,
    outcome: str,
    parent_id: Optional[str],
    evidence_ref: Mapping[str, Any],
    idempotency_key: Optional[str],
) -> str:
    if idempotency_key is not None:
        return idempotency_key
    stable = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "stage_id": stage_id,
        "event_type": event_type,
        "subject_id": subject_id,
        "outcome": outcome,
        "parent_id": parent_id,
        "evidence_ref": dict(evidence_ref),
    }
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _idempotency_hash(key: str) -> str:
    return fingerprint(key)


def _lock(root: Path) -> ContextManager[Any]:
    """Serialize the read-before-append idempotency check when possible."""
    path = resolve_trace_root(root) / _IDEMPOTENCY_LOCK_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
    except OSError:
        return nullcontext()

    class _FileLock:
        def __enter__(self) -> "_FileLock":
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            return self

        def __exit__(self, *_args: Any) -> None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()

    return _FileLock()


def _existing_event(root: Path, *, event_id: str, key_hash: str) -> Optional[dict[str, Any]]:
    for event in read_events(root):
        if event.get("event_id") == event_id:
            return event
        evidence = event.get("evidence_ref") or {}
        if evidence.get("idempotency_key_hash") == key_hash:
            return event
    return None


@dataclass(frozen=True)
class EventEmission:
    """Result of an adapter call.

    ``degraded=True`` means the event was not durably appended. Callers may
    continue their real work, but must not use this result as success
    evidence. ``record`` is the sanitized candidate for diagnostics.
    """

    event_id: Optional[str]
    record: Mapping[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None
    persisted: bool = False
    duplicate: bool = False
    degraded: bool = False
    status: str = EVENT_STATUS_DEGRADED
    error: Optional[str] = None

    @property
    def evidence_available(self) -> bool:
        return self.persisted and not self.degraded and self.event_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "path": str(self.path) if self.path is not None else None,
            "persisted": self.persisted,
            "duplicate": self.duplicate,
            "degraded": self.degraded,
            "status": self.status,
            "error": self.error,
        }


class RalphEventAdapter:
    """Emit bounded Ralph lifecycle evidence into the trace event journal."""

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        attempt_id: str = "",
        workflow_id: str = "ralph",
        source: str = "ralph",
    ) -> None:
        self.root = Path(root)
        self.run_id = _required_id(run_id, "run_id")
        self.attempt_id = attempt_id.strip() if isinstance(attempt_id, str) else ""
        self.workflow_id = _required_id(workflow_id, "workflow_id")
        self.source = _required_id(source, "source")

    def _record(
        self,
        *,
        stage_id: str,
        attempt_id: str,
        event_type: str,
        subject_id: str,
        outcome: str,
        parent_id: Optional[str],
        ts: str,
        evidence_ref: Mapping[str, Any],
        key_hash: str,
        event_id: str,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event_id": event_id,
            "run_id": self.run_id,
            "attempt_id": attempt_id,
            "workflow_id": self.workflow_id,
            "stage_id": stage_id,
            "stage": stage_id,
            "event_type": event_type,
            "subject_id": subject_id,
            "parent_id": parent_id,
            "ts": ts,
            "outcome": outcome,
            "source": self.source,
            "evidence_ref": {
                **dict(evidence_ref),
                "attempt_id": attempt_id,
                "stage_id": stage_id,
                "idempotency_key_hash": key_hash,
                "observability_status": evidence_ref.get(
                    "observability_status", OBSERVABILITY_OK
                ),
            },
        }
        if _json_size(record) <= MAX_EVENT_BYTES:
            return record

        # Keep the identity and output evidence first. The shared event cap
        # is the only event-size policy; arbitrary extra metadata is dropped
        # only when it would make the event exceed that cap.
        original = record["evidence_ref"]
        compact = {
            key: original[key]
            for key in (
                "stdout",
                "stderr",
                "excerpt",
                "stdout_truncated",
                "stderr_truncated",
                "excerpt_truncated",
                "stdout_redacted",
                "stderr_redacted",
                "excerpt_redacted",
                "handoff",
                "attempt_id",
                "stage_id",
                "idempotency_key_hash",
                "observability_status",
            )
            if key in original
        }
        compact["evidence_truncated"] = True
        record["evidence_ref"] = compact
        if _json_size(record) > MAX_EVENT_BYTES:
            # Output itself remains bounded by MAX_EXCERPT_CHARS. A caller
            # with pathological identity fields receives degraded telemetry
            # rather than an oversized trace line.
            raise ValueError("Ralph evidence event exceeds MAX_EVENT_BYTES")
        return record

    def emit(
        self,
        stage_id: str,
        event_type: str,
        outcome: str,
        *,
        subject_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        ts: Optional[str] = None,
        stdout: Any = "",
        stderr: Any = "",
        excerpt: Any = None,
        handoff: Optional[Mapping[str, Any]] = None,
        evidence_ref: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> EventEmission:
        stage = _required_id(stage_id, "stage_id")
        event_name = _required_id(event_type, "event_type")
        result = _required_id(outcome, "outcome")
        subject = _required_id(subject_id or stage, "subject_id")
        attempt = _required_id(attempt_id or self.attempt_id, "attempt_id")
        parent = None if parent_id is None else _required_id(parent_id, "parent_id")
        timestamp = _required_id(ts or now_utc(), "ts")
        if idempotency_key is not None:
            idempotency_key = _required_id(idempotency_key, "idempotency_key")

        if handoff is not None:
            validate_handoff(dict(handoff))

        out, out_truncated, out_redacted = _bounded_text(
            stdout, limit=MAX_EXCERPT_CHARS
        )
        err, err_truncated, err_redacted = _bounded_text(
            stderr, limit=MAX_EXCERPT_CHARS
        )
        ref: dict[str, Any] = _safe_value(dict(evidence_ref or {}))
        ref.update(
            {
                "stdout": out,
                "stderr": err,
                "stdout_truncated": out_truncated,
                "stderr_truncated": err_truncated,
                "stdout_redacted": out_redacted,
                "stderr_redacted": err_redacted,
            }
        )
        if excerpt is not None:
            excerpt_value, excerpt_truncated, excerpt_redacted = _bounded_text(
                excerpt, limit=MAX_EXCERPT_CHARS
            )
            ref.update(
                {
                    "excerpt": excerpt_value,
                    "excerpt_truncated": excerpt_truncated,
                    "excerpt_redacted": excerpt_redacted,
                }
            )
        if handoff is not None:
            ref["handoff"] = _safe_value(dict(handoff))

        key = _event_key(
            run_id=self.run_id,
            attempt_id=attempt,
            stage_id=stage,
            event_type=event_name,
            subject_id=subject,
            outcome=result,
            parent_id=parent,
            evidence_ref=ref,
            idempotency_key=idempotency_key,
        )
        key_hash = _idempotency_hash(key)
        event_id = f"ralph-{key_hash}"
        record = self._record(
            stage_id=stage,
            attempt_id=attempt,
            event_type=event_name,
            subject_id=subject,
            outcome=result,
            parent_id=parent,
            ts=timestamp,
            evidence_ref=ref,
            key_hash=key_hash,
            event_id=event_id,
        )

        try:
            with _lock(self.root):
                existing = _existing_event(
                    self.root, event_id=event_id, key_hash=key_hash
                )
                if existing is not None:
                    return EventEmission(
                        event_id=existing.get("event_id", event_id),
                        record=existing,
                        path=resolve_trace_root(self.root) / _TRACE_EVENT_PATH,
                        persisted=True,
                        duplicate=True,
                        status=EVENT_STATUS_DUPLICATE,
                    )
                path, persisted_id = append_event(self.root, record)
        except Exception as exc:  # telemetry must not change the workflow result
            error = redact_excerpt(exc, limit=MAX_SUMMARY_CHARS)
            degraded_record = dict(record)
            degraded_ref = dict(degraded_record["evidence_ref"])
            degraded_ref["observability_status"] = OBSERVABILITY_DEGRADED
            degraded_record["evidence_ref"] = degraded_ref
            return EventEmission(
                event_id=event_id,
                record=degraded_record,
                degraded=True,
                status=EVENT_STATUS_DEGRADED,
                error=error,
            )
        record["event_id"] = persisted_id
        return EventEmission(
            event_id=persisted_id,
            record=record,
            path=path,
            persisted=True,
            status=EVENT_STATUS_RECORDED,
        )


EventAdapter = RalphEventAdapter


def emit_event(
    root: Path,
    *,
    run_id: str,
    attempt_id: str,
    stage_id: str,
    event_type: str,
    outcome: str,
    **kwargs: Any,
) -> EventEmission:
    """Functional convenience wrapper for one Ralph event."""
    return RalphEventAdapter(root, run_id=run_id, attempt_id=attempt_id).emit(
        stage_id,
        event_type,
        outcome,
        **kwargs,
    )


__all__ = [
    "EVENT_STATUS_DEGRADED",
    "EVENT_STATUS_DUPLICATE",
    "EVENT_STATUS_RECORDED",
    "EventAdapter",
    "EventEmission",
    "OBSERVABILITY_DEGRADED",
    "OBSERVABILITY_OK",
    "RalphEventAdapter",
    "emit_event",
]
