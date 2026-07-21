#!/usr/bin/env python3
"""session_monitor.py -- inline arrow-key picker over all Claude Code +
Codex sessions in this repo.

Shows every session captured by ``/dev-kit:log`` (running or stopped) across
this repo's worktrees, grouped by git worktree with LIVE / IDLE / STALE
status. Pressing Enter on a session exits the picker and execs
``claude --resume <sid>`` (or ``codex resume <sid>``) with the working
directory set to that session's worktree, so the user lands back inside it.

The UI is a single-pane inline picker (arrow keys to move, Enter to
resume, ``q`` / ``Esc`` / ``Ctrl-C`` to cancel) built directly on
``termios`` + ANSI escapes -- no ``curses``, no third-party deps. The
intent is the same "pick one of N" pattern Claude Code's own
``AskUserQuestion`` uses; rendering stays inside the terminal's normal
scrollback so the user never loses their last command's output.

Stdlib only. All log parsing and worktree classification is reused from
``tools/token_efficiency_analyzer.py`` -- this module adds status
derivation, running-process detection, and the resume hand-off.

Usage::

    python3 tools/session_monitor.py            # interactive picker (real terminal)
    python3 tools/session_monitor.py --list     # non-interactive listing (previewable)
    python3 tools/session_monitor.py --days 90  # widen the capture window

The picker and the ``os.execvp`` resume hand-off both need a real TTY;
they cannot run through a non-interactive harness Bash tool. Use ``--list``
to preview inside a conversation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

# When run as ``python3 tools/session_monitor.py`` the script's own dir is
# already ``sys.path[0]``; the explicit insert also covers ``import
# session_monitor`` from the test suite (which inserts ``tools/`` on the path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_usage  # noqa: E402  (path set up above)
import token_efficiency_analyzer as tea  # noqa: E402  (path set up above)


# Forward declarations: the dataclasses (Status / Session / AgentNode /
# AgentGraph / WorktreeInfo) are referenced by the sibling-module
# imports below, so they are hoisted above the imports. They stay
# here even after the rest of the module is loaded -- the load order
# in Python is "execute imports, then run remaining top-level code",
# and the sibling modules only need to resolve names that are
# already defined by the time they look them up.
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


# Three concerns used to live inline: the interactive arrow-key picker
# (termios + ANSI), the --cli-setup alias installer, the JSON +
# eval-handshake emitter, and the shared format primitives. Re-import
# them here so the public surface (sm.pick_session, sm.install_cli_alias,
# sm.print_json, sm._column_header, ...) stays importable from a single
# namespace.
from session_monitor_alias import (  # noqa: E402
    _CLI_BEGIN,
    _CLI_END,
    CLI_ALIAS_NAME,
    _alias_block,
    _render_rc,
    _shell_rc,
    _strip_managed_block,
    install_cli_alias,
)
from session_monitor_cli import (  # noqa: E402
    _validate_modes,
    main,
    parse_args,
)
from session_monitor_format import (  # noqa: E402
    _GLYPH,
    STATE_SECTIONS,
    _column_header,
    _commit_cell,
    _per_worktree_top_skills,
    _rel_time,
    _src_tag,
    group_by_state,
)
from session_monitor_picker import (  # noqa: E402
    _ANSI,
    _STATUS_COLOR,
    _move_selectable,
    _read_key,
    _render_picker,
    _selectable_indices,
    _terminal_size,
    build_rows,
    pick_session,
)
from session_monitor_render import (  # noqa: E402
    EVAL_AXES,
    build_eval_handshake,
    print_json,
)

# Public re-exports. Tests import these via ``sm.X``; ruff treats
# re-exports listed in __all__ as intentional and skips the F401
# "unused import" check on them.
__all__ = [
    # dataclasses
    "Status", "Session", "AgentNode", "AgentGraph", "WorktreeInfo",
    # constants
    "RECENCY_WINDOW_SECONDS",
    # alias install
    "CLI_ALIAS_NAME", "_CLI_BEGIN", "_CLI_END",
    "_alias_block", "_render_rc", "_shell_rc", "_strip_managed_block",
    "install_cli_alias",
    # format primitives (shared with picker + render)
    "STATE_SECTIONS", "group_by_state",
    "_GLYPH", "_rel_time", "_src_tag",
    "_column_header", "_commit_cell", "_per_worktree_top_skills",
    # picker
    "_ANSI", "_STATUS_COLOR",
    "build_rows", "_selectable_indices", "_move_selectable",
    "_terminal_size", "_render_picker", "_read_key",
    "pick_session",
    # render
    "EVAL_AXES", "build_eval_handshake", "print_json",
    # CLI
    "parse_args", "_validate_modes", "main",
]

# A session with no running process is still "LIVE" if its most recent turn
# landed within this window -- the Stop/SessionEnd hooks fire per turn, so a
# fresh last_ts means the CLI is very likely still open.
RECENCY_WINDOW_SECONDS = 180




# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------
def discover_repo_root(start: Path | None = None) -> Path:
    """Resolve the MAIN repo checkout (owner of the shared .git), even when
    invoked from inside a worktree."""
    start = (start or Path.cwd()).resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).parent
    except Exception:
        pass
    return start


def worktree_paths(repo_root: Path, *, runner=subprocess.run) -> dict[str, Path]:
    """Map worktree dirname (matching ``tea.worktree_from_path``) -> abs path.

    Always includes the ``(main)`` sentinel. Degrades to just ``(main)`` if
    ``git worktree list`` is unavailable."""
    paths: dict[str, Path] = {"(main)": repo_root}
    try:
        out = runner(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return paths
    if out.returncode != 0:
        return paths
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line[len("worktree "):].strip())
            name = tea.worktree_from_path(p)
            paths.setdefault(name, p)
    return paths


def _is_cli_process(cmd: str) -> bool:
    """True when the ps command line is a claude/codex CLI (not the desktop
    app, a Helper process, or an unrelated shell that merely mentions them)."""
    low = cmd.lower()
    if "claude.app" in low or "helper" in low or ".vscode" in low:
        return False
    toks = cmd.split()
    if not toks:
        return False
    # Only trust the executable (argv[0]) or an interpreter's script arg.
    return any(Path(t).name in ("claude", "codex") for t in toks[:2])


def _is_resume_process(cmd: str) -> bool:
    padded = " " + cmd + " "
    return " -r " in padded or "--resume" in cmd or " resume " in padded


def list_cli_processes(*, runner=subprocess.run) -> list[dict]:
    """Enumerate running claude/codex CLI processes (read-only)."""
    try:
        out = runner(["ps", "-axo", "pid=,command="],
                     capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    procs: list[dict] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, cmd = parts
        if not pid_s.isdigit() or not _is_cli_process(cmd):
            continue
        procs.append({"pid": int(pid_s), "command": cmd,
                      "is_resume": _is_resume_process(cmd)})
    return procs


def pid_cwd(pid: int, *, runner=subprocess.run) -> Path | None:
    """Resolve a process's cwd via ``lsof``. None on any failure."""
    try:
        out = runner(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                     capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def _worktree_for_cwd(cwd: Path, wt_paths: dict[str, Path]) -> str | None:
    """Longest-prefix match of a cwd against known worktree paths."""
    try:
        cwd = cwd.resolve()
    except Exception:
        pass
    best: str | None = None
    best_len = -1
    for name, p in wt_paths.items():
        try:
            pr = p.resolve()
        except Exception:
            pr = p
        if cwd == pr or pr in cwd.parents:
            length = len(str(pr))
            if length > best_len:
                best, best_len = name, length
    return best


def map_processes_to_worktrees(procs: list[dict], wt_paths: dict[str, Path],
                               *, runner=subprocess.run) -> dict[str, list[int]]:
    """Map each running CLI process to the worktree it is cwd'd into.

    Processes whose cwd is outside every known worktree (a different repo)
    are dropped."""
    result: dict[str, list[int]] = {}
    for proc in procs:
        cwd = pid_cwd(proc["pid"], runner=runner)
        if cwd is None:
            continue
        name = _worktree_for_cwd(cwd, wt_paths)
        if name is None:
            continue
        result.setdefault(name, []).append(proc["pid"])
    return result


def derive_status(agg: dict, worktree_state: str, now: datetime) -> Status:
    """Per-session base status: STALE (worktree merged/gone) > LIVE (recent
    turn within the recency window) > IDLE.

    Running-process attribution is applied separately by
    ``attach_live_processes`` because a process is cwd'd into a *worktree*,
    which may hold many sessions -- only the newest one is plausibly the live
    CLI, so a running PID must not blanket-mark every session in the worktree."""
    if worktree_state in tea.STALE_WORKTREE_STATES:
        return Status.STALE
    last = agg.get("last_ts")
    if last is not None:
        try:
            if (now - last).total_seconds() <= RECENCY_WINDOW_SECONDS:
                return Status.LIVE
        except Exception:
            pass
    return Status.IDLE


def attach_live_processes(sessions: list[Session],
                          pid_map: dict[str, list[int]]) -> None:
    """Attribute each worktree's running CLI PIDs to its newest non-stale
    session and mark that one LIVE. Mutates the sessions in place."""
    by_wt: dict[str, list[Session]] = {}
    for s in sessions:
        by_wt.setdefault(s.worktree, []).append(s)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for wt, pids in pid_map.items():
        if not pids:
            continue
        cands = [s for s in by_wt.get(wt, []) if s.status is not Status.STALE]
        if not cands:
            continue
        newest = max(cands, key=lambda s: s.last_ts or epoch)
        newest.pids = pids
        newest.status = Status.LIVE


def _enrich_branches_from_worktrees(sessions: list[Session],
                                    *, runner=subprocess.run) -> None:
    """Override each session's branch with the worktree's current HEAD
    branch. The log captures ``branch`` at save-time, which can lag the
    actual checkout (e.g. a session that started on ``main`` before the
    user moved the worktree to a feature branch). Mutates ``agg['branch']``
    in place so the picker, ``--list``, and ``--json`` all show the same
    value. Skips stale (merged/gone) worktrees and detached HEADs. Falls
    back to the log-captured branch on any ``git`` failure.
    """
    by_wt: dict[str, list[Session]] = {}
    for s in sessions:
        by_wt.setdefault(s.worktree, []).append(s)
    for sess_list in by_wt.values():
        first = sess_list[0]
        if first.worktree_state in tea.STALE_WORKTREE_STATES:
            continue
        wt_path = first.wt_path
        if wt_path is None or not Path(wt_path).is_dir():
            continue
        try:
            out = runner(
                ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
        except Exception:
            continue
        if out.returncode != 0:
            continue
        branch = out.stdout.strip()
        if not branch or branch == "HEAD":  # detached
            continue
        for s in sess_list:
            s.agg["branch"] = branch


def build_agent_graph(path: Path) -> AgentGraph:
    """Stream one session's jsonl into a parent -> sub-agent graph.

    Orchestrator only; the four pure steps are tested in isolation:

    1. :func:`_decode_transcript` -- reads + parses jsonl records,
       skipping blank / malformed lines with a narrow exception set.
    2. :func:`_extract_root_prompt` -- first ``user`` text, truncated.
    3. :func:`_collect_spawn_nodes` -- walks assistant ``Agent``
       ``tool_use`` blocks in encounter order.
    4. :func:`_collect_sidechain_chains` + :func:`_correlate_nodes_to_chains`
       -- group ``isSidechain`` lines and pair each to the spawn whose
       ``description`` matches the chain head (falls back to encounter
       index when no description is present).

    Codex logs have no sidechains and yield an empty node list.
    """
    if not Path(path).is_file():
        return AgentGraph(session_id=Path(path).stem,
                          root_user_prompt="", nodes=[])
    records = list(_decode_transcript(path))
    nodes = _collect_spawn_nodes(records)
    _correlate_nodes_to_chains(nodes, _collect_sidechain_chains(records))
    return AgentGraph(session_id=Path(path).stem,
                      root_user_prompt=_extract_root_prompt(records),
                      nodes=nodes)


def _decode_transcript(path: Path) -> Iterable[dict]:
    """Yield parsed jsonl records from a session log.

    Skips blank lines and lines that fail to parse. Exceptions are
    narrowed to the realistic failure set: ``OSError`` (file open /
    read) and ``json.JSONDecodeError`` (parse). Anything else (e.g.
    ``AttributeError`` from a bug upstream) propagates so the failure
    is visible during development rather than silently dropped."""
    p = Path(path)
    if not p.is_file():
        return
    try:
        fh = open(p, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield obj
    finally:
        fh.close()


def _extract_root_prompt(records: list[dict]) -> str:
    """Return the first ``user`` message's text, truncated to 200 chars.

    Empty string when no user message is present (e.g. Codex-only
    transcripts whose first line is ``session_meta``)."""
    for obj in records:
        if obj.get("type") == "user":
            return _first_user_text(obj.get("message") or {})[:200]
    return ""


def _collect_spawn_nodes(records: list[dict]) -> list[AgentNode]:
    """Extract ``Agent`` ``tool_use`` spawn edges from the main
    transcript (non-sidechain ``assistant`` turns), in encounter order."""
    nodes: list[AgentNode] = []
    for obj in records:
        if obj.get("isSidechain"):
            continue
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not (isinstance(blk, dict)
                    and blk.get("type") == "tool_use"
                    and blk.get("name") == "Agent"):
                continue
            inp = blk.get("input") or {}
            nodes.append(AgentNode(
                tool_use_id=blk.get("id", "") or "",
                subagent_type=(inp.get("subagent_type")
                               or inp.get("agentType") or ""),
                description=inp.get("description", "") or "",
                prompt_excerpt=(inp.get("prompt", "") or "")[:200],
            ))
    return nodes


def _collect_sidechain_chains(records: list[dict]) -> list[tuple[str, dict]]:
    """Group ``isSidechain`` records into chains by walking ``parentUuid``
    links. Returns ``[(chain_id, {"turns": int, "last_ts": dt,
    "first_user_text": str}), ...]`` in encounter order. Orphan records
    (``parentUuid`` pointing at nothing) seed a new chain.

    ``first_user_text`` captures the chain head's user text so the
    correlation step can match spawns to chains by ``description`` -- the
    stable identity that survives concurrent spawn reordering in the
    wire log (Plan-sidechain's first record can land before
    Explore-sidechain's first record, which scrambles the encounter
    index but not the chain-head text).
    """
    chains: dict[str, dict] = {}
    order: list[str] = []
    uuid_to_root: dict[str, str] = {}
    for obj in records:
        if not obj.get("isSidechain"):
            continue
        uuid = obj.get("uuid") or ""
        parent = obj.get("parentUuid")
        root = uuid_to_root.get(parent) if parent else None
        if root is None:
            root = uuid or f"chain-{len(order)}"
            order.append(root)
            chains[root] = {"turns": 0, "last_ts": None,
                            "first_user_text": ""}
        if uuid:
            uuid_to_root[uuid] = root
        chains[root]["turns"] += 1
        ts = tea.parse_iso(obj.get("timestamp", "") or "")
        cur = chains[root]["last_ts"]
        if ts and (cur is None or ts > cur):
            chains[root]["last_ts"] = ts
        if not chains[root]["first_user_text"] and obj.get("type") == "user":
            text = _first_user_text(obj.get("message") or {})
            if text:
                chains[root]["first_user_text"] = text
    return [(cid, chains[cid]) for cid in order]


def _correlate_nodes_to_chains(nodes: list[AgentNode],
                               chains: list[tuple[str, dict]]) -> None:
    """Attach each chain's ``turns`` + ``last_ts`` to the spawn node it
    belongs to.

    Strategy: when at least one spawn carries a ``description``, match
    each node to the chain whose head text contains that description.
    The chain head is the user prompt the parent sent to the subagent,
    so the description (which the parent ``Agent`` ``tool_use`` echoes
    back) is a stable identity even when concurrent sidechains
    interleave and the encounter index would swap turn counts between
    agents. Falls back to position-by-index when no description match
    is found -- preserves legacy behaviour for transcripts without
    ``description`` fields. Mutates ``nodes`` in place."""
    if not chains:
        return
    chain_user_texts = [meta.get("first_user_text", "") for _, meta in chains]
    used: set[int] = set()
    matched_any = False
    for node in nodes:
        if not node.description:
            continue
        for j, ct in enumerate(chain_user_texts):
            if j in used or not ct:
                continue
            if node.description in ct:
                _, meta = chains[j]
                node.turn_count = meta.get("turns", 0) or 0
                node.last_ts = meta.get("last_ts")
                used.add(j)
                matched_any = True
                break
    if matched_any:
        return
    # Legacy fallback: pair by encounter index for transcripts that
    # carry no ``description`` field.
    for i, node in enumerate(nodes):
        if i < len(chains):
            _, meta = chains[i]
            node.turn_count = meta.get("turns", 0) or 0
            node.last_ts = meta.get("last_ts")


def _first_user_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                return blk.get("text", "") or ""
            if isinstance(blk, str):
                return blk
    return ""


def build_resume(agg: dict, repo_root: Path,
                 wt_path: Path | None) -> tuple[Path, list[str], str | None]:
    """Return (cwd, argv, warning) for the resume hand-off."""
    sid = agg.get("session_id", "")
    source = agg.get("source", "claude-code")
    if source == "codex":
        argv = ["codex", "resume", sid]
    else:
        argv = ["claude", "--resume", sid]
    if wt_path and Path(wt_path).is_dir():
        return Path(wt_path), argv, None
    warning = (f"worktree '{agg.get('worktree', '?')}' is gone/merged; "
               f"resuming in main checkout {repo_root}")
    return repo_root, argv, warning


def collect_sessions(repo_root: Path, logs_dir: Path,
                     repo: str, days: int) -> list[dict]:
    """discover -> dedupe -> aggregate (skip None) -> date/repo filter."""
    files = tea._dedupe_by_session(tea.discover_logs(logs_dir, repo_root=repo_root))
    aggs = [a for p in files if (a := tea.aggregate_session(p)) is not None]
    return tea.filter_sessions(aggs, repo, days)


def group_by_worktree(sessions: list[Session], wt_meta: dict,
                      wt_paths: dict[str, Path]) -> list[WorktreeInfo]:
    buckets: dict[str, list[Session]] = {}
    for s in sessions:
        buckets.setdefault(s.worktree, []).append(s)

    infos: list[WorktreeInfo] = []
    for name, sess in buckets.items():
        sess.sort(key=lambda s: (s.last_ts or datetime.min.replace(
            tzinfo=timezone.utc)), reverse=True)
        state = wt_meta.get(name, {}).get("state", "unknown")
        path = wt_paths.get(name)
        for s in sess:
            s.wt_path = path
        infos.append(WorktreeInfo(dirname=name, state=state,
                                  path=path, sessions=sess))

    def _rank(info: WorktreeInfo):
        has_live = any(s.status is Status.LIVE for s in info.sessions)
        newest = max((s.last_ts for s in info.sessions if s.last_ts),
                     default=datetime.min.replace(tzinfo=timezone.utc))
        # live worktrees first, then most-recently-active
        return (0 if has_live else 1, _neg_time(newest))

    infos.sort(key=_rank)
    return infos


def _neg_time(dt: datetime) -> float:
    try:
        return -dt.timestamp()
    except Exception:
        return 0.0


def build_model(repo_root: Path, logs_dir: Path, repo: str, days: int,
                *, now: datetime | None = None,
                runner=subprocess.run) -> list[WorktreeInfo]:
    now = now or datetime.now(timezone.utc)
    aggs = collect_sessions(repo_root, logs_dir, repo, days)
    wt_meta = tea.classify_all_worktrees(repo_root, git_runner=runner)
    wt_paths = worktree_paths(repo_root, runner=runner)
    pid_map = map_processes_to_worktrees(
        list_cli_processes(runner=runner), wt_paths, runner=runner)

    sessions: list[Session] = []
    for agg in aggs:
        wt = agg.get("worktree") or "(unknown)"
        state = wt_meta.get(wt, {}).get("state", "unknown")
        sessions.append(Session(agg=agg, worktree_state=state,
                                status=derive_status(agg, state, now)))
    attach_live_processes(sessions, pid_map)
    result = group_by_worktree(sessions, wt_meta, wt_paths)
    _enrich_branches_from_worktrees(sessions, runner=runner)
    attach_last_commit_subjects(result, runner=runner)
    return result


def get_last_commit_subject(wt_path: Path, *,
                            runner=subprocess.run) -> str | None:
    """Resolve the last commit's subject line from a worktree dir.

    Returns ``None`` on any failure (no git, no commits, missing dir,
    subprocess error) so the listing never crashes. The subject line
    is read with ``%s`` so multi-line commit messages are truncated at
    the first newline — only the headline fits in a column.
    """
    if wt_path is None or not Path(wt_path).is_dir():
        return None
    try:
        out = runner(
            ["git", "-C", str(wt_path), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    subj = out.stdout.strip()
    return subj or None


def attach_last_commit_subjects(model: list[WorktreeInfo],
                                *, runner=subprocess.run) -> None:
    """Populate each WorktreeInfo's ``last_commit_subject`` field.

    Resolves the subject once per worktree dir (sessions in the same
    worktree share HEAD) and is a no-op when the path is missing or
    not a git repo. Mutates in place."""
    for w in model:
        w.last_commit_subject = get_last_commit_subject(w.path, runner=runner)


def filter_model(model: list[WorktreeInfo], pattern: str) -> list[WorktreeInfo]:
    """Substring filter (case-insensitive) against every visible session
    field: session_id, branch, model, source, log_path, worktree
    dirname, status. Empty pattern is identity. WorktreeInfo buckets
    whose sessions all fail the filter are dropped entirely."""
    pat = (pattern or "").strip().lower()
    if not pat:
        return list(model)
    out: list[WorktreeInfo] = []
    for w in model:
        kept = [s for s in w.sessions if _session_matches(s, w, pat)]
        if kept:
            out.append(WorktreeInfo(
                dirname=w.dirname, state=w.state, path=w.path,
                sessions=kept, last_commit_subject=w.last_commit_subject,
            ))
    return out


def _session_matches(s: Session, w: WorktreeInfo, pat: str) -> bool:
    haystacks = (
        s.session_id, s.branch, s.model, s.source, s.log_path,
        w.dirname, s.status.value,
    )
    return any(pat in (h or "").lower() for h in haystacks)


# Section labels for the structured listing. Order = display order, which
# also encodes priority (live work first, archived work last). Keep in sync
# with the bucket names emitted by ``group_by_state``.
# (Re-exported from session_monitor_format.)


def print_plain_listing(model: list[WorktreeInfo], logs_dir: Path,
                         *, skill_usage_agg: dict | None = None,
                         skill_top_n: int = 3) -> None:
    """Non-interactive listing for previewing inside a conversation (--list).

    Sessions are bucketed by worktree STATE first (live -> merged -> gone
    -> unknown) so the structural picture reads top-down: active work on
    top, archived work at the bottom. Each worktree header shows the
    resolved ``last_commit_subject`` so you can see what each worktree's
    tip is without dropping into git yourself.
    """
    now = datetime.now(timezone.utc)
    total = sum(len(w.sessions) for w in model)
    if total == 0:
        print(f"[session-monitor] no sessions found under {logs_dir}")
        return
    live = sum(1 for w in model for s in w.sessions if s.status is Status.LIVE)
    sections = group_by_state(model)
    print(f"[session-monitor] {total} sessions across {len(model)} worktrees "
          f"({live} live)  logs={logs_dir}")
    for label, wts in sections:
        section_total = sum(len(w.sessions) for w in wts)
        print(f"\n── {label.upper()}  ({len(wts)} worktrees, "
              f"{section_total} sessions) " + "─" * max(0, 56 - len(label)))
        for w in wts:
            tag = f"last: \"{w.last_commit_subject}\"" if w.last_commit_subject else "last: ?"
            print(f"  ▸ {w.dirname}  [{w.state}]  ({len(w.sessions)} sessions)  {tag}")
            if skill_usage_agg and w.path is not None:
                wt_skills = skill_usage.filter_by_cwd_prefix(
                    skill_usage_agg, str(w.path))
                top = _per_worktree_top_skills(wt_skills, top_n=skill_top_n)
                if top:
                    print(f"    TOP SKILLS: {top}")
            print(_column_header("    "))
            for s in w.sessions:
                sub = f" +{s.subagent_count}agt" if s.subagent_count else ""
                print(f"    {_GLYPH[s.status]} {s.status.value:5} "
                      f"{_src_tag(s.source):<3} {s.session_id[:8]} "
                      f"{s.model:14.14} {s.branch:22.22} "
                      f"{_rel_time(s.last_ts, now):>9}  "
                      f"{_commit_cell(w.last_commit_subject)}{sub}")

    if skill_usage_agg:
        print("\n── SKILL USAGE  (top across all logs) " + "─" * 30)
        print(skill_usage.format_table(skill_usage_agg, top=10))


# EVAL_AXES + build_eval_handshake live in session_monitor_render; the
# re-import at the top of this module keeps ``sm.EVAL_AXES`` /
# ``sm.build_eval_handshake`` importable for callers that do not want to
# add the render module to their import path.
# (Re-exported from session_monitor_render.)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
# parse_args / _validate_modes / main live in session_monitor_cli; the
# re-import at the top of this module keeps the legacy ``sm.parse_args``
# / ``sm.main`` / ``sm._validate_modes`` call sites working without
# changing the script's ``__main__`` guard.
# (Re-exported from session_monitor_cli.)


if __name__ == "__main__":
    raise SystemExit(main())
