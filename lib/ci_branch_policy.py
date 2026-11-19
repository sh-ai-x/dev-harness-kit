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

    def _run(jq_expr: str) -> tuple[set[str], str]:
        """Run one `gh api --jq` probe.

        Returns `(names, err_msg)`. `err_msg` is non-empty iff the probe
        hard-failed; in that case `names` is always empty and `err_msg`
        is the human-readable reason.

        (Previously the error string was smuggled through the `names`
        set as `set(f"gh api failed: {err}")` and popped back out with
        `.pop()`. `set(<str>)` splits the string into individual
        characters, so the caller received a single arbitrary character
        — e.g. `'a'` — instead of the actual failure reason.)
        """
        try:
            cp = subprocess.run(
                [gh, "api",
                 f"repos/{repo}/branches/main/protection/required_status_checks",
                 "--jq", jq_expr],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
            return set(), f"gh api error: {type(e).__name__}"
        if cp.returncode != 0:
            err = (cp.stderr or "").strip().splitlines()[-1] if cp.stderr else ""
            return set(), f"gh api failed: {err or cp.returncode}"
        names = {ln.strip() for ln in cp.stdout.splitlines() if ln.strip()}
        return names, ""

    names, err = _run(".contexts[]?")
    if err:
        return set(), err
    if names:
        return names, ""
    names2, err2 = _run(".checks[].context")
    if err2:
        return set(), err2
    if not names2:
        return set(), "no contexts found"
    return names2, ""
