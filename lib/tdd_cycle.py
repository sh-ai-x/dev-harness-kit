#!/usr/bin/env python3
"""Record the executable RED/GREEN boundary consumed by tdd-guard."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE = ".dev-kit/.tdd-cycle.json"


def _resolve_root(explicit: Path | None) -> Path:
    """Resolve the TDD state root the same way the shell hooks do.

    Mirrors ``${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}``
    used by hooks/tdd-guard.sh:26 and hooks/tdd-scope-judge.sh:14, so the
    CLI records RED/GREEN evidence where the guard reads it. Without this,
    ``python3 -m lib.tdd_cycle red`` (the command tdd-guard.sh itself
    suggests, with no ``--root``) wrote the state under cwd — when
    ``DEV_KIT_TDD_ROOT`` pointed elsewhere, the guard never saw the RED
    evidence and false-denied the next core-code edit.
    """
    if explicit is not None:
        return explicit.resolve()
    env_root = os.environ.get("DEV_KIT_TDD_ROOT")
    if env_root:
        return Path(env_root).resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except OSError:
        pass
    return Path.cwd().resolve()


def _state(root: Path) -> dict:
    try:
        return json.loads((root / STATE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def run(root: Path, phase: str, command: list[str]) -> int:
    if not command:
        print(f"{phase.upper()} requires a test command after --", file=sys.stderr)
        return 2
    if phase == "green" and _state(root).get("phase") != "red":
        print("GREEN blocked: run RED first", file=sys.stderr)
        return 2
    result = subprocess.run(command, cwd=root)
    if phase == "red":
        if result.returncode == 0:
            print("RED failed: test command passed", file=sys.stderr)
            return 1
    elif result.returncode:
        print(f"GREEN failed (exit {result.returncode})", file=sys.stderr)
        return result.returncode
    path = root / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phase": phase, "command": command, "exit_code": result.returncode,
                                "recorded_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    print(f"{phase.upper()} confirmed (exit {result.returncode})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=None,
                        help="TDD state root (default: DEV_KIT_TDD_ROOT, else git toplevel, else cwd)")
    parser.add_argument("phase", choices=("red", "green"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run(_resolve_root(args.root), args.phase, command)


if __name__ == "__main__":
    raise SystemExit(main())
