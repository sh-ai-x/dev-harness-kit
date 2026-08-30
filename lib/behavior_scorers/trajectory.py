"""trajectory.py — D7 Trajectory Quality (hybrid: heuristic + LLM).

Phase 0 ships the heuristic half. The LLM-judge half lands in Phase 1
(proposal §03). The final score is `heuristic_value * 0.7 + llm_value * 0.3`
when both are present; with no LLM, the score is just the heuristic value.

Heuristic checks (proposal §01 D7):
- `same_tool_3x`: same `skill` field appearing 3+ times → penalty
- `read_before_edit_missing`: no `Read` tool calls before `Edit` tool
  calls → penalty (agent may be writing without context)
- `backtrack > 2`: 3+ "backtrack" patterns (judged by repeated
  identical `phase` strings in close succession) → penalty

Score mapping:
- 5: 0 penalties
- 4: 1 penalty
- 3: 2 penalties
- 1-2: 3 penalties (catastrophic loop)

The Phase 1 LLM-judge wiring will add a 30% weight on top.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from lib.behavior_scorers.types import Context, DimensionScore


def _load_latest_trace(worktree: Path) -> Dict[str, Any] | None:
    """Load the most recent trace JSON under `eval/transcripts/`.

    Layout contract: `<worktree>/eval/transcripts/<case_id>/*.json`.
    Returns the JSON-decoded contents of the latest-mtime trace, or
    `None` if the layout is empty / unreadable. Centralized here so
    D7 (trajectory) and D3 (efficiency) cannot drift on the layout.
    """
    latest_path = _latest_trace_path(worktree)
    if latest_path is None:
        return None
    try:
        return json.loads(latest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _latest_trace_path(worktree: Path) -> Path | None:
    """Resolve the latest-mtime `eval/transcripts/<dim>/*.json` path.

    Returns `None` when no transcripts directory, no case_dirs, or no
    `*.json` files exist. The pair (`_latest_trace_path` +
    `_load_latest_trace`) lets `efficiency.py` compute metrics directly
    on the path without re-decoding the JSON when only the path matters.
    """
    transcripts = worktree / "eval" / "transcripts"
    if not transcripts.is_dir():
        return None
    case_dirs = [p for p in transcripts.iterdir() if p.is_dir()]
    if not case_dirs:
        return None
    latest_dir = max(case_dirs, key=lambda d: d.stat().st_mtime)
    traces = sorted(latest_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not traces:
        return None
    return traces[-1]


def _heuristic(trace: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the three heuristic checks to a trace dict."""
    steps: List[Dict[str, Any]] = trace.get("steps", [])

    skill_counts = Counter(s.get("skill", "") for s in steps)
    same_tool_3x = sum(1 for c in skill_counts.values() if c >= 3)

    # Read-before-edit: simple proxy on the 'extra' field which carries
    # tool-specific metadata. Scorers that populate `extra.tool` set
    # the tool name; absent entries count as no-evidence.
    def _tools(s: Dict[str, Any]) -> List[str]:
        extra = s.get("extra") or {}
        tool = extra.get("tool")
        if isinstance(tool, str):
            return [tool]
        if isinstance(tool, list):
            return [str(t) for t in tool]
        return []

    tools_in_order: List[str] = []
    for s in steps:
        tools_in_order.extend(_tools(s))
    saw_read = any("Read" in t or "read" in t for t in tools_in_order)
    saw_edit = any("Edit" in t or "edit" in t or "Write" in t for t in tools_in_order)
    read_before_edit_missing = saw_edit and not saw_read

    # Backtrack detection: 3+ consecutive identical phase values.
    backtrack = 0
    prev = None
    run = 0
    for s in steps:
        phase = s.get("phase", "")
        if phase == prev:
            run += 1
        else:
            run = 1
            prev = phase
        if run >= 3:
            backtrack = max(backtrack, run)

    backtrack_too_many = backtrack >= 3

    penalties = sum(
        1 for v in (bool(same_tool_3x), read_before_edit_missing, backtrack_too_many) if v
    )
    return {
        "skill_counts": dict(skill_counts),
        "same_tool_3x_count": same_tool_3x,
        "read_before_edit_missing": read_before_edit_missing,
        "backtrack_max_run": backtrack,
        "backtrack_too_many": backtrack_too_many,
        "penalties": penalties,
    }


def score(worktree: Path, ctx: Context) -> DimensionScore:
    """Score D7 from heuristic (Phase 0) + LLM judge (Phase 1)."""
    trace = _load_latest_trace(worktree)
    if trace is None:
        return DimensionScore(
            dim="D7_trajectory",
            value=3,
            evidence={"reason": "no trace available", "phase": 0},
        )

    h = _heuristic(trace)
    penalties = h["penalties"]

    if penalties == 0:
        heuristic_value = 5
    elif penalties == 1:
        heuristic_value = 4
    elif penalties == 2:
        heuristic_value = 3
    else:
        heuristic_value = 1

    # Phase 1 will multiply this with LLM value; for now, return the
    # heuristic directly.
    return DimensionScore(
        dim="D7_trajectory",
        value=heuristic_value,
        evidence={**h, "phase": 0},
    )
