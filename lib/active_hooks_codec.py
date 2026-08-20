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
    """
    path = project_root / ".dev-kit" / ".active-hooks.json"
    default: Dict = {
        "schema_version": "1.0.0",
        "matrix": copy.deepcopy(DEFAULT_MATRIX),
        "override": {
            "disabled_hooks": [],
            "strict_mode": False,
            "env_override": {},
        },
    }
    return read_json_or_default(path, default)


# Back-compat alias: read_matrix historically returned a fresh init on miss.
# Existing callers (set_stage / disable_override / __main__) want write
# semantics; load_matrix is the canonical read-only path.
read_matrix = load_matrix
init_matrix = ensure_matrix


def is_hook_active(project_root: Path, stage: str, hook_name: str) -> bool:
    """Return True if hook should fire in this stage."""
    data = load_matrix(project_root)
    if hook_name in data.get("override", {}).get("disabled_hooks", []):
        return False
    env_off = os.environ.get("DEV_KIT_HOOK_OFF", "")
    if env_off and hook_name in env_off.split(","):
        return False
    matrix = data.get("matrix", {})
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
