#!/usr/bin/env python3
"""Record the executable RED/GREEN boundary consumed by tdd-guard."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE = ".dev-kit/.tdd-cycle.json"


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
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("phase", choices=("red", "green"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run(args.root.resolve(), args.phase, command)


if __name__ == "__main__":
    raise SystemExit(main())
