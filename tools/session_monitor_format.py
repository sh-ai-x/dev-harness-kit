"""session_monitor_format.py -- shared format primitives + grouping.

Splits the rendering helpers out of ``tools/session_monitor.py``. These
primitives are shared between the inline picker
(``tools/session_monitor_picker.py``) and the plain listing / JSON
emitters, so they live in their own module to avoid a circular
import between the picker and the main module.

Imports ``Status`` / ``WorktreeInfo`` from ``session_monitor_types`` (NOT
``session_monitor``) so the cycle that fires under
``python3 tools/session_monitor.py --help`` (where the parent is loaded
as ``__main__`` and has no top-level ``session_monitor`` module yet)
cannot re-enter this module mid-load.

Public surface (re-exported by ``tools/session_monitor.py``):
- ``STATE_SECTIONS``, ``group_by_state``
- ``_GLYPH``, ``_rel_time``, ``_src_tag``
- ``_column_header``, ``_commit_cell``
- ``_per_worktree_top_skills``
"""
from __future__ import annotations

from datetime import datetime, timezone

from session_monitor_types import Status, WorktreeInfo  # noqa: E402

# Section labels for the structured listing. Order = display order,
# which also encodes priority (live work first, archived work last).
# Keep in sync with the bucket names emitted by ``group_by_state``.
STATE_SECTIONS = ("live", "merged", "gone", "unknown")


def group_by_state(model: list[WorktreeInfo]) -> list[tuple[str, list[WorktreeInfo]]]:
    """Group worktrees into state sections for the structured listing.

    Returns ``[(section_label, [WorktreeInfo...]), ...]`` in the fixed
    order ``live -> merged -> gone -> unknown``. Sections with no
    worktrees are omitted. Within a section the input ordering is
    preserved (callers like ``group_by_worktree`` already sort by
    recency, so this composes)."""
    buckets: dict[str, list[WorktreeInfo]] = {k: [] for k in STATE_SECTIONS}
    for w in model:
        buckets.setdefault(w.state, []).append(w)
    return [(k, buckets[k]) for k in STATE_SECTIONS if buckets[k]]


_GLYPH = {Status.LIVE: "●", Status.IDLE: "○", Status.STALE: "⌀"}


def _rel_time(ts: datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    try:
        secs = (now - ts).total_seconds()
    except Exception:
        return "?"
    if secs < 0:
        secs = 0
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _src_tag(source: str) -> str:
    return "cx" if source == "codex" else "cc"


def _column_header(indent: str) -> str:
    """Column-label line aligned to the STATUS/SRC/ID/MODEL/BRANCH/AGE/COMMIT
    fields shared by ``print_plain_listing`` and the inline picker.

    Field widths mirror the data rows exactly: STATUS covers glyph + status
    word (8), SRC (3), ID (9), MODEL (15), BRANCH (23), AGE right-justified
    (9), COMMIT (40, truncated). ``indent`` differs per view (4 spaces for
    ``--list``, 2 for the picker) but every column after it lines up."""
    return (f"{indent}{'STATUS':<8}{'SRC':<4}{'ID':<9}"
            f"{'MODEL':<15}{'BRANCH':<23}{'AGE':>9}  {'COMMIT':<40}")


def _commit_cell(subject: str | None) -> str:
    """Single-cell commit subject, 40-char truncated, '?' when absent."""
    if not subject:
        return "?"
    return subject[:40].ljust(40)


def _per_worktree_top_skills(agg: dict, top_n: int = 3) -> str:
    """Format the top-N skills whose ``cwd`` falls under this worktree.

    Empty string when the worktree has no path or no skill lines land
    under it. Output is the form ``skill:turns inv:invocations`` pairs
    for grep-friendly column alignment, e.g.
    ``dev-kit:inspect:3 inv:0 dev-kit:feat-fix:2 inv:2``.
    """
    if not agg:
        return ""
    rows = sorted(
        agg.items(),
        key=lambda kv: (-kv[1].get("turns", 0), -kv[1].get("invocations", 0), kv[0]),
    )[:top_n]
    parts = []
    for name, rec in rows:
        parts.append(f"{name}:{rec.get('turns', 0)} inv:{rec.get('invocations', 0)}")
    return "  ".join(parts)
