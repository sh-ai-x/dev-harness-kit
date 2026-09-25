"""ci_branch_policy.py — branch-protection check for ci-doctor.

Issue #249 ish: compare GitHub branch-protection required status checks
against the `name:` values of jobs in review.yml. WARN on mismatch, SKIP
on absence of gh or repo context, INFO in source-repo mode.

Pulled out of `lib/ci_doctor.py` so the orchestrator stays focused.
Caller passes the `Check` factory.
"""
from __future__ import annotations

import subprocess
from typing import Callable

CheckFactory = Callable[..., "object"]


def fetch_required_status_checks(gh_available_fn, repo: str) -> tuple[set[str], str]:
    """Required-check-context names via the GH API. Empty set + non-empty
    msg means degraded (gh absent / unauth / no contexts found)."""
    gh, degraded = gh_available_fn(timeout=10)
    if not gh:
        return set(), degraded or "gh not on PATH"

    def _run(jq_expr: str) -> tuple[set[str], bool]:
        try:
            cp = subprocess.run(
                [gh, "api",
                 f"repos/{repo}/branches/main/protection/required_status_checks",
                 "--jq", jq_expr],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError):
            return set(), True
        if cp.returncode != 0:
            err = (cp.stderr or "").strip().splitlines()[-1] if cp.stderr else ""
            return set(f"gh api failed: {err or cp.returncode}"), True
        names = {ln.strip() for ln in cp.stdout.splitlines() if ln.strip()}
        return names, False

    names, hard = _run(".contexts[]?")
    if hard:
        return set(), names.pop() if names else "gh api failed"
    if names:
        return names, ""
    names2, hard2 = _run(".checks[].context")
    if hard2:
        return set(), names2.pop() if names2 else "gh api failed"
    if not names2:
        return set(), "no contexts found"
    return names2, ""
