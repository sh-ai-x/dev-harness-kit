"""gh_cli.py — shared gh-CLI presence + auth probe.

Inlined `_gh_available` at 4 sites in the YAGNI sweep (PR #915) created
4 byte-identical 13-line copies of the same function (ci_doctor × 4
calls, ci_setup, gates_state, proposal_orch_issue_pr). The original
gh_cli.py docstring explicitly justified centralization by calling out
the duplication risk; the YAGNI math only flipped at 4 callers.

Re-centralized here so a timeout bump, an additional exception class,
or a different degraded message format is one edit instead of four.

Returns `(gh_path, degraded_msg)`:
  * `(path, "")`        — gh on PATH and authenticated
  * `(None, reason)`    — degraded; caller should SKIP rather than FAIL
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional, Tuple


def _gh_available(*, timeout: int = 10) -> Tuple[Optional[str], str]:
    gh = shutil.which("gh")
    if not gh:
        return None, "gh not on PATH"
    try:
        cp = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return None, f"gh auth error: {type(e).__name__}"
    if cp.returncode != 0:
        return None, "gh not authenticated"
    return gh, ""
