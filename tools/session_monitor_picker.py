"""session_monitor_picker.py -- inline arrow-key picker (termios + ANSI).

Splits the interactive picker out of ``tools/session_monitor.py``. The
picker is a single-pane inline UI built directly on ``termios`` and
ANSI escapes -- no ``curses``, no third-party deps. The intent mirrors
Claude Code's ``AskUserQuestion``: arrow keys to move, Enter to
resume, ``q`` / ``Esc`` / ``Ctrl-C`` to cancel. Rendering stays inside
the terminal's normal scrollback so the user never loses their last
command's output.

Public surface (re-exported by ``tools/session_monitor.py`` so callers
keep using ``sm.pick_session``, ``sm.build_rows``, etc.):
- ``_ANSI``, ``_STATUS_COLOR``
- ``build_rows``, ``_selectable_indices``, ``_move_selectable``
- ``_terminal_size``, ``_render_picker``, ``_read_key``
- ``pick_session``
"""
from __future__ import annotations

import os
import select
import sys
import termios
from datetime import datetime, timezone

# When imported as ``session_monitor.picker`` from the parent module,
# the parent has already inserted ``tools/`` on sys.path; when run
# standalone for tests, this is a no-op since the import is by name only.
from session_monitor import (  # noqa: E402  (parent module path set by host)
    _GLYPH,
    Session,
    Status,
    WorktreeInfo,
    _column_header,
    _commit_cell,
    _rel_time,
    _src_tag,
    group_by_state,
)

_ANSI = {
    "reset":      "\x1b[0m",
    "bold":       "\x1b[1m",
    "dim":        "\x1b[2m",
    "reverse":    "\x1b[7m",
    "hide_cur":   "\x1b[?25l",
    "show_cur":   "\x1b[?25h",
    "home":       "\x1b[H",
    "clear_eol":  "\x1b[K",
    "green":      "\x1b[32m",
    "yellow":     "\x1b[33m",
    "red":        "\x1b[31m",
    "cyan":       "\x1b[36m",
}

_STATUS_COLOR = {
    Status.LIVE: "green",
    Status.IDLE: "yellow",
    Status.STALE: "red",
}


def build_rows(model: list[WorktreeInfo], *,
               now: datetime | None = None) -> list[dict]:
    """Flatten a worktree model into header + session rows for the picker.

    Pure function -- testable without a TTY. Emits three row kinds:

    - ``"section"`` — top-level bucket label ("LIVE", "MERGED", ...) with
      no ``session`` key; not selectable.
    - ``"header"``  — per-worktree title with state + commit subject; not
      selectable.
    - ``"columns"`` — column-label row beneath each header; not selectable.
    - ``"session"`` — selectable row carrying a ``Session`` payload.

    The picker only lands its cursor on session rows (see
    ``_move_selectable``).
    """
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []
    sections = group_by_state(model)
    for label, wts in sections:
        section_total = sum(len(w.sessions) for w in wts)
        rows.append({
            "kind": "section",
            "text": (f"── {label.upper()}  ({len(wts)} worktrees, "
                     f"{section_total} sessions) " + "─" * 30),
        })
        for w in wts:
            tag = f"  last: \"{w.last_commit_subject}\"" if w.last_commit_subject else ""
            rows.append({
                "kind": "header",
                "text": (f"  ▸ {w.dirname}  [{w.state}]  "
                         f"({len(w.sessions)} sessions){tag}"),
            })
            rows.append({"kind": "columns", "text": _column_header("  ")})
            for s in w.sessions:
                sub = f" +{s.subagent_count}agt" if s.subagent_count else ""
                rows.append({
                    "kind": "session",
                    "text": (f"  {_GLYPH[s.status]} {s.status.value:5} "
                             f"{_src_tag(s.source):<3} "
                             f"{s.session_id[:8]} {s.model[:14]:14} "
                             f"{s.branch[:22]:22} "
                             f"{_rel_time(s.last_ts, now):>9}  "
                             f"{_commit_cell(w.last_commit_subject)}{sub}"),
                    "session": s,
                })
    return rows


def _selectable_indices(rows: list[dict]) -> list[int]:
    return [i for i, r in enumerate(rows) if r["kind"] == "session"]


def _move_selectable(rows: list[dict], cursor: int, delta: int) -> int:
    """Move the cursor by ``delta`` session rows, never landing on a header."""
    sel = _selectable_indices(rows)
    if not sel:
        return cursor
    if cursor in sel:
        pos = sel.index(cursor)
    else:
        # cursor was on a header; land on the nearest selectable row
        pos = len(sel)
        for j, idx in enumerate(sel):
            if idx >= cursor:
                pos = j
                break
    target = max(0, min(pos + delta, len(sel) - 1))
    return sel[target]


def _terminal_size(fallback: tuple[int, int] = (80, 24)) -> tuple[int, int]:
    try:
        return os.get_terminal_size(0)
    except OSError:
        return fallback


def _render_picker(out, rows: list[dict], cursor: int, scroll: int,
                   max_x: int, max_y: int) -> None:
    """Write the picker frame to ``out`` (one full redraw per call).

    Layout: 1 header line + body + 1 footer line. ``max_x`` and ``max_y``
    are the caller's terminal size in columns / rows; this function does
    not query the terminal itself so the same call can be unit-tested.
    """
    body_h = max(1, max_y - 2)
    sess_total = sum(1 for r in rows if r["kind"] == "session")
    wt_total = sum(1 for r in rows if r["kind"] == "header")
    head = (f" session-monitor  {sess_total} sessions / {wt_total} worktrees ")

    out.write(_ANSI["home"] + _ANSI["hide_cur"])
    out.write(_ANSI["bold"] + _ANSI["cyan"] + head.ljust(max_x) + _ANSI["reset"] + "\n")

    visible_end = min(scroll + body_h, len(rows))
    for i in range(scroll, visible_end):
        r = rows[i]
        text = r["text"][: max_x - 1]
        if r["kind"] in ("section", "header", "columns"):
            out.write(_ANSI["dim"] + text.ljust(max_x) + _ANSI["reset"] + "\n")
            continue
        color = _STATUS_COLOR.get(r["session"].status)
        prefix = _ANSI[color] if color else ""
        if i == cursor:
            out.write(_ANSI["reverse"] + prefix + text.ljust(max_x)
                      + _ANSI["reset"] + "\n")
        else:
            out.write(prefix + text.ljust(max_x) + _ANSI["reset"] + "\n")

    for _ in range(body_h - (visible_end - scroll)):
        out.write(_ANSI["clear_eol"] + "\n")

    footer = " ↑↓ / j k move   Enter resume   q / Esc / Ctrl-C quit "
    out.write(_ANSI["reverse"] + footer.ljust(max_x) + _ANSI["reset"])
    out.flush()


def _read_key(timeout: float = 0.5) -> bytes:
    """Read one logical keypress from stdin, with timeout.

    Resolves ``ESC [ A/B`` into single bytes ``b"\\x1b[A"`` /
    ``b"\\x1b[B"`` so the caller can match arrow keys directly. A lone
    ``ESC`` (no follow-up byte within 50 ms) is returned as-is.
    """
    rlist, _, _ = select.select([0], [], [], timeout)
    if not rlist:
        return b""
    b = os.read(0, 1)
    if b != b"\x1b":
        return b
    rlist, _, _ = select.select([0], [], [], 0.05)
    if not rlist:
        return b"\x1b"  # lone ESC
    nxt = os.read(0, 1)
    if nxt != b"[":
        return b"\x1b" + nxt
    rlist, _, _ = select.select([0], [], [], 0.05)
    if not rlist:
        return b"\x1b["
    return b"\x1b[" + os.read(0, 1)


def pick_session(model: list[WorktreeInfo]) -> Session | None:
    """Run the inline arrow-key picker. Returns the selected Session, or
    None if the user quit (``q`` / ``Esc`` / ``Ctrl-C``). Always restores
    the original ``termios`` state on exit, even on exception.
    """
    rows = build_rows(model)
    selectable = _selectable_indices(rows)
    if not selectable:
        return None

    cursor = selectable[0]
    scroll = 0

    try:
        saved = termios.tcgetattr(0)
    except termios.error:
        saved = None

    try:
        if saved is not None:
            attrs = termios.tcgetattr(0)
            # Disable canonical mode + echo, but keep ISIG so Ctrl-C
            # still raises KeyboardInterrupt (which the outer try/except
            # catches and turns into a clean None return).
            attrs[3] &= ~(termios.ICANON | termios.ECHO)
            termios.tcsetattr(0, termios.TCSAFLUSH, attrs)

        while True:
            max_x, max_y = _terminal_size()
            max_y = max(5, max_y)
            _render_picker(sys.stdout, rows, cursor, scroll, max_x, max_y)

            key = _read_key(0.5)
            if not key:
                continue

            if key in (b"\r", b"\n"):
                return rows[cursor]["session"]
            if key == b"\x1b":
                return None
            if key == b"\x1b[A" or key in (b"k", b"K"):
                cursor = _move_selectable(rows, cursor, -1)
            elif key == b"\x1b[B" or key in (b"j", b"J"):
                cursor = _move_selectable(rows, cursor, +1)
            elif key in (b"q", b"Q"):
                return None

            body_h = max(1, max_y - 2)
            if cursor < scroll:
                scroll = cursor
            elif cursor >= scroll + body_h:
                scroll = cursor - body_h + 1

    except KeyboardInterrupt:
        return None
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(0, termios.TCSAFLUSH, saved)
            except termios.error:
                pass
        sys.stdout.write(_ANSI["show_cur"] + "\n")
        sys.stdout.flush()
