"""Canonical session event data and runtime record normalization."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Normalized event emitted during one runtime session."""

    session_id: str
    event_name: str
    timestamp: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def cwd(self) -> str | None:
        value = self.payload.get("cwd")
        return value if isinstance(value, str) else None

    @property
    def role(self) -> str | None:
        value = self.payload.get("role")
        return value if isinstance(value, str) else None

    @property
    def task(self) -> str | None:
        value = self.payload.get("task")
        return value if isinstance(value, str) else None


def normalize_session_event(
    raw: Mapping[str, Any] | object,
    session_id: str | None = None,
) -> SessionEvent | None:
    """Normalize one runtime event, returning ``None`` for malformed input."""
    if not isinstance(raw, Mapping):
        return None

    payload = raw.get("payload", {})
    if not isinstance(payload, Mapping):
        return None
    event_name = _event_name(raw, payload)
    event_session_id = raw.get("session_id") or raw.get("sessionId") or session_id
    timestamp = _parse_timestamp(raw.get("timestamp") or payload.get("timestamp"))
    if not isinstance(event_session_id, str) or not event_session_id:
        return None
    if event_name is None or timestamp is None:
        return None
    return SessionEvent(event_session_id, event_name, timestamp, payload)


def _event_name(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    for value in (
        raw.get("event_name"),
        raw.get("eventName"),
        raw.get("hook_event_name"),
        payload.get("type"),
        raw.get("type"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


__all__ = ["SessionEvent", "normalize_session_event"]
