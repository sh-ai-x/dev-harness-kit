"""_gh_cli.py — shared gh-CLI presence + auth probe.

Five sites in ci_doctor.py + ci_setup.py used to repeat the same
`shutil.which('gh') + subprocess.run([gh, 'auth', 'status'], timeout=...)`
+ try/except dance. Centralize the *presence + auth* check here so a
timeout bump, an additional exception class, or a different degraded
message format is one edit instead of five.

Returns a `(gh_path, degraded_msg)` tuple:
  * `(path, "")`        — gh is on PATH and authenticated
  * `(None, reason)`    — degraded; caller should SKIP rather than FAIL
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional, Tuple


def gh_available(*, timeout: int = 10) -> Tuple[Optional[str], str]:
    """Return `(gh_path, degraded_msg)`.

    `gh_path` is the absolute path to the `gh` binary when present and
    authenticated, else `None`. `degraded_msg` is the empty string on
    success, otherwise a one-line reason suitable for surfacing as a
    SKIP row. Never raises — `SubprocessError`, `TimeoutExpired`, and
    `OSError` all collapse to a degraded return.
    """
    gh = shutil.which("gh")
    if not gh:
        return None, "gh not on PATH"
    try:
        cp = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return None, f"gh auth error: {type(e).__name__}"
    if cp.returncode != 0:
        return None, "gh not authenticated"
    return gh, ""


def run_gh(
    gh: str,
    *args: str,
    timeout: int = 10,
    cwd: Optional[str] = None,
) -> Tuple[Optional["subprocess.CompletedProcess[str]"], str]:
    """Run `gh <args>` with the standard guard.

    Returns `(cp_or_None, degraded_msg)`. On success, `cp` is the
    `CompletedProcess` and `degraded_msg` is empty. On any subprocess
    failure, `cp` is `None` and `degraded_msg` describes the failure.
    Callers that need parsed JSON should inspect `cp.stdout` themselves;
    this helper intentionally returns the raw process so each site can
    decide its own post-conditions.
    """
    try:
        cp = subprocess.run(
            [gh, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return None, f"gh {args[0] if args else '?'} error: {type(e).__name__}: {e}"
    return cp, ""
