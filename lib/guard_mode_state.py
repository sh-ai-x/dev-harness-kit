#!/usr/bin/env python3
"""
guard_mode_state.py — session-scoped on/off state for the two hard-block
Iron Law hooks: `hooks/tdd-guard.sh` and `hooks/worktree-guard.sh`.

State lives at ``.dev-kit/guard-mode.session.json``, reset to all-"on" by
``hooks/session-start-guard-mode-reset.sh`` at the start of every session
(new window = enforced by default — mirrors `lib/harness_mode_state.py`'s
"new window = strict" design). Unlike harness-mode's optional local hooks,
`tdd_guard` and `worktree_guard` are hard PreToolUse blocks enforcing Iron
Law L1 (no prod code without a verification artifact) and the
`rules/git-workflow.md` worktree-isolation rule; this module exists so a session
can deliberately and visibly suspend them for itself, never silently and
never beyond the current session.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic import atomic_write_json  # noqa: E402

STATE_REL_PATH = Path(".dev-kit") / "guard-mode.session.json"

GUARDS = ("tdd_guard", "worktree_guard")

GUARD_DESCRIPTIONS = {
    "tdd_guard": "hooks/tdd-guard.sh — blocks prod code edits without RED evidence (Iron Law L1)",
    "worktree_guard": "hooks/worktree-guard.sh — blocks Edit/Write/MultiEdit in the main checkout (rules/git-workflow.md worktree isolation)",
}


def _state_path(root: Optional[Path] = None) -> Path:
    return (root or Path(".")) / STATE_REL_PATH


def _default_state() -> dict:
    return {g: "on" for g in GUARDS}


def read_state(root: Optional[Path] = None) -> dict:
    """Read the session state file. Missing/corrupt/invalid -> all-"on"."""
    path = _state_path(root)
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    state = _default_state()
    for g in GUARDS:
        if data.get(g) in ("on", "off"):
            state[g] = data[g]
    return state


def write_state(overrides: dict, root: Optional[Path] = None) -> dict:
    """Merge `overrides` (guard -> "on"/"off") onto the current state and
    write it atomically. Unknown keys and non "on"/"off" values are dropped
    silently — defense in depth against a bad caller writing garbage that
    would otherwise be interpreted as "off" via a loose truthiness check.
    Returns the resulting state.
    """
    state = read_state(root)
    for g, v in overrides.items():
        if g in GUARDS and v in ("on", "off"):
            state[g] = v
    atomic_write_json(_state_path(root), state)
    return state


def reset_state(root: Optional[Path] = None) -> dict:
    """Force every guard back to "on". Used by the SessionStart hook."""
    state = _default_state()
    atomic_write_json(_state_path(root), state)
    return state


def resolved_guard(name: str, root: Optional[Path] = None) -> str:
    """Return "on"/"off" for one guard. Unknown guard names resolve "on"
    (fail closed — an unrecognized name must never be read as permission
    to skip enforcement)."""
    if name not in GUARDS:
        return "on"
    return read_state(root).get(name, "on")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="guard-mode session state CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    get_p = sub.add_parser("get", help="print the resolved value for one guard")
    get_p.add_argument("guard", choices=GUARDS)

    set_p = sub.add_parser("set", help="set one guard for this session")
    set_p.add_argument("guard", choices=GUARDS)
    set_p.add_argument("value", choices=["on", "off"])

    sub.add_parser("reset", help="reset every guard to \"on\"")

    show_p = sub.add_parser("show", help="print the resolved state of every guard")
    show_p.add_argument("--json", action="store_true", help="compact single-line JSON")

    args = parser.parse_args(argv)
    if args.command == "get":
        print(resolved_guard(args.guard))
        return 0
    if args.command == "set":
        write_state({args.guard: args.value})
        return 0
    if args.command == "reset":
        reset_state()
        return 0
    if args.command == "show":
        state = read_state()
        out = {
            g: {"value": state[g], "description": GUARD_DESCRIPTIONS[g]}
            for g in GUARDS
        }
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
