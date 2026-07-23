"""Canonical token usage data and runtime record normalization."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenLog:
    """Normalized token usage for one requested time window."""

    window: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


def normalize_token_log(raw: Mapping[str, Any] | object, window: str) -> TokenLog:
    """Normalize Claude- or Codex-shaped usage data into :class:`TokenLog`."""
    if not isinstance(raw, Mapping):
        return TokenLog(window, 0, 0)

    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        return TokenLog(
            window,
            _nonnegative_int(usage.get("input_tokens")),
            _nonnegative_int(usage.get("output_tokens")),
            _first_int(usage, "cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens"),
            _first_int(usage, "cache_creation_tokens", "cache_creation_input_tokens", "cache_write_tokens"),
        )

    total = _codex_total_usage(raw)
    if total is not None:
        input_tokens = _nonnegative_int(total.get("input_tokens"))
        cached_tokens = _nonnegative_int(total.get("cached_input_tokens"))
        output_tokens = _nonnegative_int(total.get("output_tokens"))
        reasoning_tokens = _nonnegative_int(total.get("reasoning_output_tokens"))
        return TokenLog(window, max(input_tokens - cached_tokens, 0), output_tokens + reasoning_tokens, cached_tokens)

    return TokenLog(
        window,
        _nonnegative_int(raw.get("input_tokens")),
        _nonnegative_int(raw.get("output_tokens")),
        _first_int(raw, "cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens"),
        _first_int(raw, "cache_creation_tokens", "cache_creation_input_tokens", "cache_write_tokens"),
    )


def _codex_total_usage(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = raw.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return None
    total = info.get("total_token_usage")
    return total if isinstance(total, Mapping) else None


def _first_int(raw: Mapping[str, Any], *names: str) -> int:
    for name in names:
        if name in raw:
            return _nonnegative_int(raw.get(name))
    return 0


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return max(parsed, 0)
