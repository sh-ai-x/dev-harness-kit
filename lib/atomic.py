"""atomic.py — shared atomic-write + KST time helpers.

Used by state_codec, active_hooks_codec, write_project_md, eval_runner,
execute. POSIX-atomic via tempfile + os.replace.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write_json(path: Path, data: Any) -> None:
    """POSIX-atomic JSON write. Parent dirs created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, content: str) -> None:
    """POSIX-atomic text write. Parent dirs created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except (OSError, UnicodeEncodeError):
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json_or_default(path: Path, default: Any) -> Any:
    """Read JSON from `path` and return the parsed value.

    Returns `default` (verbatim, by reference) when the file is missing,
    unreadable (OSError), or malformed (json.JSONDecodeError). On hit,
    returns whatever `json.loads` produced (a fresh object — never the
    default).

    Single source of truth for the "read-or-default" pattern that the
    codecs (state_codec, active_hooks_codec, ci_setup) repeat. Kept in
    `atomic.py` because it is the symmetric counterpart to
    `atomic_write_json` — together they form the file I/O pair every
    codec needs.

    Args:
        path: file to read.
        default: returned verbatim when the file is missing or unreadable
            or malformed. May be None for loaders that distinguish
            "absent" from "present" (e.g. eval_runner's `load_transcript`
            uses None, not an empty dict).

    Returns:
        Parsed JSON value (any JSON-compatible type), or `default`.

    NOTE (issue #310 slice): `lib/eval_runner.py` has THREE read helpers
    (`load_transcript`, `_load_session_cache`, `_golden_index`) that
    match the same pattern. They are intentionally left untouched in
    this slice because the eval subsystem is being refactored in
    parallel under the `/dev-kit:evaluate` (review/security/plan) + the new
    `--session-log` / `--golden-diff` work; merging both refactors in
    one PR would conflict on `_load_session_cache`'s lru_cache key and
    on `_golden_index`'s "skip-on-error" contract. The follow-up is:
    migrate those three call sites to `read_json_or_default` once the
    eval slice lands and we have a single eval_runner PR to rebase.
    """
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default
