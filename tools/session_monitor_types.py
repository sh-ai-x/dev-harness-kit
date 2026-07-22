"""session_monitor_types.py -- shared dataclasses for the session_monitor split.

The ``session_monitor`` decomposition splits rendering / picking / CLI into
focused sibling modules. Several of those siblings (``session_monitor_format``,
``session_monitor_picker``, ``session_monitor_render``) need the Status enum
and the WorktreeInfo dataclass at module load time, but importing them from
``session_monitor`` itself creates a back-edge cycle:

  - Run-as-script path: ``python3 tools/session_monitor.py --help`` loads
    the file as ``__main__``. No ``session_monitor`` module exists yet in
    ``sys.modules``. The sibling import ``from session_monitor import ...``
    inside ``session_monitor_format`` triggers a *second* load of
    ``session_monitor.py`` (now named ``session_monitor``), which itself
    reaches for ``from session_monitor_format import ...``. That second copy
    fails before argparse runs (it cannot resolve ``_GLYPH`` because the
    first copy is still mid-import).

The previous attempt to break the cycle ("hoist dataclasses above the
sibling imports") only works when the file is imported under the
``session_monitor`` name. Under the documented script entrypoint, the
import graph re-enters and explodes.

The fix: move the dataclasses into a dedicated module with **zero**
circular dependencies. ``session_monitor_types`` is consumed by both the
parent (``tools/session_monitor.py``) and every sibling, so the cycle
collapses to a one-way import tree:

  tools.session_monitor_types    (no project-internal imports)
         ^
         |-- tools.session_monitor          (re-exports + main)
         |-- tools.session_monitor_format   (uses Status, WorktreeInfo)
         |-- tools.session_monitor_picker   (uses Status, WorktreeInfo)
         |-- tools.session_monitor_render   (uses Status, WorktreeInfo)
         |-- tools.session_monitor_cli      (no domain types used)
         |-- tools.session_monitor_alias    (no domain types used)

Public surface (re-exported by ``tools/session_monitor.py`` for backward
compat with tests / existing callers):
- ``Status``
- ``Session``
- ``AgentNode``
- ``AgentGraph``
- ``WorktreeInfo``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class Status(Enum):
    LIVE = "live"
    IDLE = "idle"
    STALE = "stale"


@dataclass
class Session:
    agg: dict
    worktree_state: str
    status: Status
    pids: list[int] = field(default_factory=list)
    wt_path: Path | None = None

    @property
    def session_id(self) -> str:
        return self.agg.get("session_id", "")

    @property
    def source(self) -> str:
        return self.agg.get("source", "claude-code")

    @property
    def worktree(self) -> str:
        return self.agg.get("worktree") or "(unknown)"

    @property
    def branch(self) -> str:
        return self.agg.get("branch") or ""

    @property
    def model(self) -> str:
        return self.agg.get("model") or "?"

    @property
    def last_ts(self):
        return self.agg.get("last_ts")

    @property
    def subagent_count(self) -> int:
        tc = self.agg.get("tool_counts") or {}
        try:
            return int(tc.get("Agent", 0))
        except Exception:
            return 0

    @property
    def log_path(self) -> str:
        return self.agg.get("log_path", "")


@dataclass
class AgentNode:
    tool_use_id: str = ""
    subagent_type: str = ""
    description: str = ""
    prompt_excerpt: str = ""
    turn_count: int = 0
    last_ts: datetime | None = None


@dataclass
class AgentGraph:
    session_id: str
    root_user_prompt: str
    nodes: list[AgentNode]


@dataclass
class WorktreeInfo:
    dirname: str
    state: str
    path: Path | None
    sessions: list  # type: ignore[type-arg]  # list[Session] forward-ref
    last_commit_subject: str | None = None
