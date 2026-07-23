"""Codex implementation of the runtime adapter contract."""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import SessionEvent, TokenLog

PromptCallback = Callable[[str], str]
SkillInstaller = Callable[[str, Path], None]

_CODEX_ENV_SIGNALS = (
    "CODEX_HOME",
    "CODEX_CLI",
    "CODEX_PROJECT_DIR",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
)
_HOOK_EVENT_NAMES = {
    "PreToolUse": "before_tool_use",
    "PostToolUse": "after_tool_use",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "PermissionRequest": "permission_request",
    "Notification": "notification",
}
_CODEX_RECORD_TYPES = {"session_meta", "turn_context", "event_msg", "response_item"}


class CodexAdapter:
    """Expose Codex runtime state through the neutral adapter contract."""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        prompt_callback: PromptCallback | None = None,
        skill_installer: SkillInstaller | None = None,
    ) -> None:
        self._project_root = project_root
        self._prompt_callback = prompt_callback
        self._skill_installer = skill_installer

    def name(self) -> str:
        return "codex"

    def is_current(self) -> bool:
        has_environment_signal = any(os.environ.get(name) for name in _CODEX_ENV_SIGNALS)
        return has_environment_signal and shutil.which("codex") is not None

    def read_token_log(self, window: str) -> TokenLog:
        result = TokenLog(window=window, input_tokens=0, output_tokens=0)
        for record in _records_for(self.workspace_root(), "sessions", window):
            snapshot = _token_snapshot(record)
            if snapshot is not None:
                result = TokenLog(
                    window=window,
                    input_tokens=snapshot[0],
                    output_tokens=snapshot[1],
                    cache_read_tokens=snapshot[2],
                    cache_creation_tokens=0,
                )
        return result

    def read_session_events(self, session_id: str) -> list[SessionEvent]:
        events: list[SessionEvent] = []
        for record in _records_for(self.workspace_root(), "session-events", session_id):
            event_name, payload = _event_parts(record)
            timestamp = _parse_timestamp(_nested_value(record, "timestamp"))
            if event_name is None or timestamp is None or payload is None:
                continue
            events.append(
                SessionEvent(
                    session_id=session_id,
                    event_name=event_name,
                    timestamp=timestamp,
                    payload=payload,
                )
            )
        return events

    def hook_event_name(self, neutral_name: str) -> str:
        return _HOOK_EVENT_NAMES.get(neutral_name, neutral_name)

    def prompt_user(self, question: str) -> str:
        if self._prompt_callback is None:
            raise RuntimeError("Codex prompt callback is not configured")
        return self._prompt_callback(question)

    def workspace_root(self) -> Path:
        if self._project_root is not None:
            return self._project_root.resolve()
        project_dir = os.environ.get("CODEX_PROJECT_DIR")
        if project_dir:
            return Path(project_dir).resolve()
        return Path.cwd().resolve()

    def install_skill(self, skill_name: str, skill_dir: Path) -> None:
        if self._skill_installer is None:
            raise RuntimeError("Codex skill installer is not configured")
        self._skill_installer(skill_name, skill_dir)


def _records_for(root: Path, directory: str, identifier: str) -> list[Mapping[str, Any]]:
    if not identifier or identifier in {".", ".."} or Path(identifier).name != identifier:
        return []

    candidates = [root / ".codex" / directory / f"{identifier}.jsonl"]
    candidates.extend((root / "logs" / "codex").glob(f"*/{identifier}.jsonl"))
    for path in candidates:
        records = _read_jsonl(path)
        if records:
            return records
    return []


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(record, Mapping):
                    records.append(record)
    except (OSError, UnicodeError, ValueError):
        return []
    return records


def _token_snapshot(record: Mapping[str, Any]) -> tuple[int, int, int] | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return None
    totals = info.get("total_token_usage")
    if not isinstance(totals, Mapping):
        return None
    input_tokens = _nonnegative_int(totals.get("input_tokens"))
    cached_tokens = _nonnegative_int(totals.get("cached_input_tokens"))
    output_tokens = _nonnegative_int(totals.get("output_tokens"))
    reasoning_tokens = _nonnegative_int(totals.get("reasoning_output_tokens"))
    return max(input_tokens - cached_tokens, 0), output_tokens + reasoning_tokens, cached_tokens


def _event_parts(record: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any] | None]:
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(record_type, str) or record_type not in _CODEX_RECORD_TYPES:
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    event_name = payload.get("type") or record_type
    if not isinstance(event_name, str) or not event_name:
        return None, None
    return event_name, payload


def _nested_value(record: Mapping[str, Any], field: str) -> Any:
    value = record.get(field)
    payload = record.get("payload")
    while value is None and isinstance(payload, Mapping):
        value = payload.get(field)
        payload = payload.get("payload")
    return value


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return max(parsed, 0)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
