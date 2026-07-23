"""Runtime-neutral hook event name normalization."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

_RUNTIME_HOOK_NAMES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "claude-code": MappingProxyType({}),
        "codex": MappingProxyType(
            {
                "PreToolUse": "before_tool_use",
                "PostToolUse": "after_tool_use",
                "SessionStart": "session_start",
                "SessionEnd": "session_end",
                "UserPromptSubmit": "user_prompt_submit",
                "PermissionRequest": "permission_request",
                "Notification": "notification",
            }
        ),
    }
)


def normalize_hook_name(runtime: str, neutral_name: str) -> str:
    """Map a neutral hook name to a supported runtime's native name."""
    try:
        names = _RUNTIME_HOOK_NAMES[runtime]
    except KeyError as exc:
        raise ValueError(f"unsupported runtime: {runtime}") from exc
    return names.get(neutral_name, neutral_name)


@dataclass(frozen=True, slots=True)
class HookNameNormalizer:
    """Bind :func:`normalize_hook_name` to one runtime."""

    runtime: str

    def normalize(self, neutral_name: str) -> str:
        return normalize_hook_name(self.runtime, neutral_name)


__all__ = ["HookNameNormalizer", "normalize_hook_name"]
