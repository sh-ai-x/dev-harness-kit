"""gh_cli.py — shared gh-CLI presence + auth probe.

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


# inspect 2026-08-27 PR #755 reviewer feedback: YAGNI — `run_gh` was
# declared with no in-tree caller (verified via Grep on lib/ + tests/).
# Removed; the only present consumer was the abstract "each site can
# decide its own post-conditions" docstring promise, which is exactly
# the kind of speculative-API the OE-4 rubric flags. If a real caller
# materializes, reintroduce as a 5-line wrapper around subprocess.run
# with the (cp, degraded_msg) return shape.
