#!/usr/bin/env python3
"""
active_hooks_codec.py — .dev-kit/.active-hooks.json reader/writer.

Single source of truth for which hooks are active in each stage (MUST-13).
hooks.json only registers the matrix reader (NOT duplicates).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Dict

try:
    from .atomic import atomic_write_json, read_json_or_default  # type: ignore  # noqa: E402
except ImportError:
    from atomic import atomic_write_json, read_json_or_default  # type: ignore  # noqa: E402

DEFAULT_MATRIX: Dict[str, Dict[str, object]] = {
    "bootstrap": {
        "tdd-guard": False,
        "bash-guard": False,
        "secret-scan": "read-only",
        "slop-detector": False,
        "stop-verify": False,
    },
    "plan": {
        "tdd-guard": False,
        "bash-guard": False,
        "secret-scan": False,
        "slop-detector": False,
        "stop-verify": True,
    },
    "design": {
        "tdd-guard": False,
        "bash-guard": False,
        "secret-scan": False,
        "slop-detector": False,
        "stop-verify": True,
    },
    "build": {
        "tdd-guard": True,
        "bash-guard": True,
        "secret-scan": True,
        "slop-detector": True,
        "stop-verify": True,
    },
    "review": {
        "tdd-guard": False,
        "bash-guard": False,
        "secret-scan": True,
        "slop-detector": True,
        "stop-verify": True,
    },
    "security": {
        "tdd-guard": False,
        "bash-guard": False,
        "secret-scan": True,
        "slop-detector": True,
        "stop-verify": True,
    },
    "ship": {
        "tdd-guard": False,
        "bash-guard": False,
        "secret-scan": False,
        "slop-detector": False,
        "stop-verify": True,
    },
}


def ensure_matrix(project_root: Path) -> Dict:
    """Initialize .dev-kit/.active-hooks.json with default matrix if missing.

    Caller MUST intend to write — `load_matrix` is the read-only path.

    Preserves any slice owned by the regen tool (events, generated_at,
    schema_version) so the two writers can coexist on the same file
    (issue #676).
    """
    path = project_root / ".dev-kit" / ".active-hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_json_or_default(path, {})
    data: Dict = dict(existing) if isinstance(existing, dict) else {}
    data["schema_version"] = "1.0.0"
    data["matrix"] = copy.deepcopy(DEFAULT_MATRIX)
    data["override"] = {
        "disabled_hooks": [],
        "strict_mode": False,
        "env_override": {},
    }
    atomic_write_json(path, data)
    return data


def load_matrix(project_root: Path) -> Dict:
    """Read .dev-kit/.active-hooks.json. Returns in-memory DEFAULT_MATRIX on miss.

    Read-only — does NOT call `atomic_write_json`. Callers needing to
    mutate should use `ensure_matrix` first.

    inspect 2026-08-27 overeng-4: results are memoized per
    `(project_root, mtime)` so `stage-gate.sh` (which calls
    `is_hook_active` once per PreToolUse event) pays one disk read per
    `mtime` change instead of one read per invocation. Cache is
    invalidated when the file's `st_mtime` changes.
    """
    path = project_root / ".dev-kit" / ".active-hooks.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    cache_key = (str(path.resolve()), mtime)
    cached = _MATRIX_CACHE.get(cache_key)
    if cached is not None:
        return cached

    default: Dict = {
        "schema_version": "1.0.0",
        "matrix": copy.deepcopy(DEFAULT_MATRIX),
        "override": {
            "disabled_hooks": [],
            "strict_mode": False,
            "env_override": {},
        },
    }
    data = read_json_or_default(path, default)
    # Bound the cache so a long-running session doesn't accumulate stale
    # entries; eviction on size limit is fine because each entry is small.
    _MATRIX_CACHE[cache_key] = data
    if len(_MATRIX_CACHE) > 32:
        # Drop the oldest entry (first inserted). Python 3.7+ dicts
        # preserve insertion order, so popitem(last=False) removes the
        # oldest.
        _MATRIX_CACHE.popitem(last=False)
    return data


# Per-(path, mtime) cache for `load_matrix`. A long-running session can
# call `is_hook_active` thousands of times (PreToolUse); caching the
# parsed JSON keyed on mtime keeps it cheap while staying correct when
# the file is rewritten by the regen tool.
_MATRIX_CACHE: Dict[tuple, Dict] = {}


# Back-compat alias: read_matrix historically returned a fresh init on miss.
# Existing callers (set_stage / disable_override / __main__) want write
# semantics; load_matrix is the canonical read-only path.
read_matrix = load_matrix
init_matrix = ensure_matrix


def is_hook_active(project_root: Path, stage: str, hook_name: str) -> bool:
    """Return True if hook should fire in this stage.

    Fresh-checkout safety (issue #676): when the codec slice is missing
    from `.dev-kit/.active-hooks.json` (the default state on a clone —
    the regen tool only writes the event-keyed slice), fall back to
    `DEFAULT_MATRIX` so the documented `stage-gate.sh` fail-open
    contract is restored. Without this, the regen would create the
    file with no `matrix` key, `stage-gate.sh` would stop fail-opening,
    and `is_hook_active` would return False for every stage, silently
    disabling the five stage-gated hooks (`tdd-guard`, `bash-guard`,
    `secret-scan`, `slop-detector`, `stop-verify`).
    """
    data = load_matrix(project_root)
    if hook_name in data.get("override", {}).get("disabled_hooks", []):
        return False
    env_off = os.environ.get("DEV_KIT_HOOK_OFF", "")
    if env_off and hook_name in env_off.split(","):
        return False
    matrix = data.get("matrix") or copy.deepcopy(DEFAULT_MATRIX)
    if stage not in matrix:
        return False
    state = matrix[stage].get(hook_name, False)
    if state == "read-only":
        return True
    return bool(state)


def set_stage(project_root: Path, stage: str, hook: str, value: object) -> None:
    """Update a single cell in the matrix."""
    data = read_matrix(project_root)
    data.setdefault("matrix", {}).setdefault(stage, {})[hook] = value
    atomic_write_json(project_root / ".dev-kit" / ".active-hooks.json", data)


def disable_override(project_root: Path, hook_name: str) -> None:
    """Add hook to override.disabled_hooks."""
    data = read_matrix(project_root)
    data.setdefault("override", {}).setdefault("disabled_hooks", [])
    if hook_name not in data["override"]["disabled_hooks"]:
        data["override"]["disabled_hooks"].append(hook_name)
    atomic_write_json(project_root / ".dev-kit" / ".active-hooks.json", data)


if __name__ == "__main__":
    import sys
    root = Path(os.environ.get("PROJECT_ROOT", "."))
    if len(sys.argv) < 2:
        print("usage: active_hooks_codec.py {init|is-active <stage> <hook>|set <stage> <hook> <bool>|disable <hook>}", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "init":
        print(json.dumps(init_matrix(root), indent=2, ensure_ascii=False))
    elif cmd == "is-active" and len(sys.argv) >= 4:
        print(is_hook_active(root, sys.argv[2], sys.argv[3]))
    elif cmd == "set" and len(sys.argv) >= 5:
        v = sys.argv[4].lower() == "true"
        set_stage(root, sys.argv[2], sys.argv[3], v)
        print("ok")
    elif cmd == "disable" and len(sys.argv) >= 3:
        disable_override(root, sys.argv[2])
        print("ok")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
