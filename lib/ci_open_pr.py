"""ci_open_pr.py — open-PR state check for ci-doctor (issue #249).

A PR in `mergeable: CONFLICTING` causes GitHub Actions to silently
refuse ALL workflows on the PR. ci-doctor surfaces this rather than
reporting PASS. Surfaces UNKNOWN (still computing) as WARN, draft state
as INFO, and version-bump PRs as INFO so users don't ask why their CI
didn't run.

Pulled out of `lib/ci_doctor.py`. Caller passes the `Check` factory.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

CheckFactory = Callable[..., "object"]


def fetch_open_pr_state(target: Path, gh_available_fn,
                       Check: CheckFactory) -> tuple[dict, str]:
    """Empty dict + non-empty msg means degraded; caller emits SKIP."""
    gh, degraded = gh_available_fn(timeout=10)
    if not gh:
        return {}, degraded or "gh not on PATH"
    try:
        cp = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return {}, f"git branch detect error: {e}"
    if cp.returncode != 0:
        return {}, f"git branch detect failed: {(cp.stderr or '').strip() or cp.returncode}"
    branch = cp.stdout.strip()
    if not branch or branch == "HEAD":
        return {}, "detached HEAD — no branch PR can target"
    try:
        cp = subprocess.run(
            [gh, "pr", "view", branch, "--json",
             "mergeable,mergeStateStatus,isDraft,title"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return {}, f"gh pr view error: {e}"
    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        if "no pull requests found" in err.lower() or "not found" in err.lower():
            return {}, "no open PR for current branch"
        return {}, f"gh pr view failed: {err or cp.returncode}"
    try:
        return json.loads(cp.stdout), ""
    except json.JSONDecodeError as e:
        return {}, f"gh pr view JSON parse error: {e}"
