"""skill_usage_normalize.py -- per-record shape normalization.

Splits the two concerns that used to live in
``tools/skill_usage.py:aggregate_skill_usage``:

1. Wire-shape flattening -- yield every ``tool_use`` block from a
   record regardless of whether it came from Claude-Code
   (``message.content``) or Codex (``payload.*``). The block list
   normalizer is a generator of generators; one function per shape.
2. Record normalization -- resolve a single log record into a
   :class:`NormalizedUsage` carrying ``ts_str`` / ``ts`` / ``cwd`` /
   ``skill`` / ``skill_invocations``. The aggregator only iterates
   over this uniform shape, so the Codex-vs-Claude-Code branching
   stays in one place.

Re-imported by ``tools/skill_usage.py`` so the public test surface
(``skill_usage._iter_tool_uses``, ``skill_usage._normalize_usage_record``)
stays importable.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable


def _iter_tool_uses(record: dict):
    """Yield ``tool_use`` blocks from a Claude-Code or Codex record.

    Claude-Code nests blocks under ``record.message.content`` (list of
    dicts, each with ``type=="tool_use"``). Codex nests blocks under
    ``record.payload`` (either a list of blocks or a dict carrying a
    ``tool_uses`` list). Some intermediate builds wrap blocks one
    level deeper (``content`` inside a wrapper dict). This normalizer
    flattens those shapes so the aggregation loop can iterate over a
    uniform sequence of blocks without branching on record origin.

    Non-dict entries, ``text`` blocks, and records with neither
    ``message`` nor ``payload`` are silently skipped -- the
    aggregator treats tool_use counts as a partial signal and any
    malformed block is the same as a missing one.
    """
    msg = record.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    yield from _flatten_block_list(_unwrap_blocks(content))

    payload = record.get("payload")
    if isinstance(payload, list):
        yield from _flatten_block_list(payload)
    elif isinstance(payload, dict):
        yield from _flatten_block_list(payload.get("tool_uses"))
        yield from _flatten_block_list(_unwrap_blocks(payload.get("content")))


def _unwrap_blocks(value):
    """Return ``value`` if it is a list of blocks; otherwise, if it
    is a dict that itself carries a ``content`` or ``tool_uses``
    list, return that inner list. Anything else returns ``None``."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        inner = value.get("content")
        if isinstance(inner, list):
            return inner
        inner = value.get("tool_uses")
        if isinstance(inner, list):
            return inner
    return None


def _flatten_block_list(items) -> Iterable[dict]:
    """Yield each dict in ``items`` whose ``type`` is ``"tool_use"``.

    Non-list inputs, non-dict members, and non-tool_use blocks are
    silently skipped. ``items`` may itself contain nested lists (rare
    but seen in Codex payloads); the loop recurses one level.
    """
    if not isinstance(items, list):
        return
    for blk in items:
        if isinstance(blk, list):
            yield from _flatten_block_list(blk)
            continue
        if not isinstance(blk, dict):
            continue
        if blk.get("type") != "tool_use":
            continue
        yield blk


@dataclass
class NormalizedUsage:
    """One record's worth of skill-usage signals, resolved across the
    Claude-Code and Codex wire shapes so the aggregator can iterate over
    a uniform sequence.

    Codex wraps several fields under ``payload.*`` while leaving
    ``attributionSkill`` at the top level. Reading only the top level
    (pre-refactor behavior) dropped records whose ``timestamp`` lived
    under ``payload`` -- silently undercounting used skills as deletion
    candidates. The normalizer walks both layers so a record contributes
    regardless of where its timestamp sits.
    """

    ts_str: str = ""
    ts: _dt.datetime | None = None
    cwd: str = ""
    skill: str = ""
    skill_invocations: list[str] = field(default_factory=list)


def _parse_iso(ts: str) -> _dt.datetime | None:
    """Local copy so this module is self-contained for unit tests."""
    if not ts:
        return None
    s = ts.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _normalize_usage_record(record: dict) -> NormalizedUsage:
    """Resolve top-level vs ``payload.*`` fields into a uniform
    :class:`NormalizedUsage`. The aggregator consumes only this shape;
    nothing else in the file needs to know about Codex payload nesting."""
    payload = record.get("payload") if isinstance(record, dict) else None
    payload_dict = payload if isinstance(payload, dict) else None

    ts_str = record.get("timestamp") or ""
    if not ts_str and payload_dict is not None:
        ts_str = payload_dict.get("timestamp") or ""

    cwd = record.get("cwd") or ""
    if not cwd and payload_dict is not None:
        cwd = payload_dict.get("cwd") or ""

    skill = record.get("attributionSkill")
    skill = skill if isinstance(skill, str) and skill else ""

    invocations: list[str] = []
    for blk in _iter_tool_uses(record):
        if blk.get("name") != "Skill":
            continue
        inp = blk.get("input") or {}
        name = inp.get("skill")
        if isinstance(name, str) and name:
            invocations.append(name)

    return NormalizedUsage(
        ts_str=ts_str,
        ts=_parse_iso(ts_str) if ts_str else None,
        cwd=cwd,
        skill=skill,
        skill_invocations=invocations,
    )
