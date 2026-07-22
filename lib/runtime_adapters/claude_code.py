"""Claude Code implementation of the runtime adapter contract."""
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

_CLAUDE_ENV_SIGNALS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_SESSION_ID",
)


class ClaudeCodeAdapter:
    """Expose Claude Code runtime state through the neutral adapter contract."""

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
        return "claude-code"

    def is_current(self) -> bool:
        has_environment_signal = any(os.environ.get(name) for name in _CLAUDE_ENV_SIGNALS)
        return has_environment_signal and shutil.which("claude") is not None

    def read_token_log(self, window: str) -> TokenLog:
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        path = self._jsonl_path("sessions", window)
        if path is not None:
            for record in _read_jsonl(path):
                if record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, Mapping):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, Mapping):
                    continue
                for field in totals:
                    totals[field] += _nonnegative_int(usage.get(field))

        return TokenLog(
            window=window,
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cache_read_tokens=totals["cache_read_input_tokens"],
            cache_creation_tokens=totals["cache_creation_input_tokens"],
        )

    def read_session_events(self, session_id: str) -> list[SessionEvent]:
        path = self._jsonl_path("session-events", session_id)
        if path is None:
            return []

        events = []
        for record in _read_jsonl(path):
            event_name = record.get("event_name") or record.get("hook_event_name")
            timestamp = _parse_timestamp(record.get("timestamp"))
            payload = record.get("payload", {})
            if not isinstance(event_name, str) or not event_name or timestamp is None:
                continue
            if not isinstance(payload, Mapping):
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
        return neutral_name

    def prompt_user(self, question: str) -> str:
        if self._prompt_callback is None:
            raise RuntimeError("Claude Code prompt callback is not configured")
        return self._prompt_callback(question)

    def workspace_root(self) -> Path:
        if self._project_root is not None:
            return self._project_root.resolve()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
        if project_dir:
            return Path(project_dir).resolve()
        return Path.cwd().resolve()

    def install_skill(self, skill_name: str, skill_dir: Path) -> None:
        if self._skill_installer is None:
            raise RuntimeError("Claude Code skill installer is not configured")
        self._skill_installer(skill_name, skill_dir)

    def _jsonl_path(self, directory: str, identifier: str) -> Path | None:
        if not identifier or identifier in {".", ".."} or Path(identifier).name != identifier:
            return None
        return self.workspace_root() / ".claude" / directory / f"{identifier}.jsonl"


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


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if not isinstance(value, (int, str)):
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
