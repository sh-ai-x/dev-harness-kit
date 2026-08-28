"""_git.py — shared subprocess wrapper for behavior_scorers.

Both `process.py` (D2) and `safety.py` (D4) need to read git state from
the worktree with the same guard semantics (FileNotFoundError + timeout
both fall through as empty string so the scorer marks the check as
failed rather than crashing). This module owns that contract so a
timeout bump, OSError-vs-SubprocessError split, or invocation semantics
change only has to be made once.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def git_output(worktree: Path, *args: str, timeout: int = 30) -> str:
    """Run `git -C <worktree> <args>` and return stripped stdout.

    Returns `""` on `FileNotFoundError` or `subprocess.TimeoutExpired` so
    the caller marks the check as failed rather than crashing the dim.
    Other exceptions (e.g. OSError) propagate; the scorer wrapper
    translates them into a `crashed=True` DimensionScore.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()
