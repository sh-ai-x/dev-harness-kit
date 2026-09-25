"""ci_branch_policy.py — branch-protection check for ci-doctor.

Issue #249 ish: compare GitHub branch-protection required status checks
against the `name:` values of jobs in review.yml. WARN on mismatch, SKIP
on absence of gh or repo context, INFO in source-repo mode.

Pulled out of `lib/ci_doctor.py` so the orchestrator stays focused.
Caller passes the `Check` factory.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from lib.ci_workflow_yaml import read_workflow

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


def check_branch_protection(target: Path, source_repo: bool,
                            gh_available_fn, detect_owner_repo_fn,
                            Check: CheckFactory) -> "object":
    """WARN on branch-policy vs review.yml job-name mismatch; SKIP/INFO otherwise."""
    if source_repo:
        return Check(label="branch policy", state="INFO",
                     detail="source repo: branch policy not audited")
    repo = detect_owner_repo_fn(target)
    if not repo:
        return Check(label="branch policy", state="SKIP",
                     detail="no GitHub remote on origin")
    required, degraded = fetch_required_status_checks(gh_available_fn, repo)
    if degraded:
        return Check(label="branch policy", state="SKIP", detail=degraded)
    review = target / ".github" / "workflows" / "review.yml"
    if not review.is_file():
        return Check(label="branch policy", state="INFO",
                     detail="review.yml not present; nothing to compare")
    raw, shape, err = read_workflow(review.parent, review.name)
    if raw is None:
        return Check(label="branch policy", state="INFO", detail=err)
    assert shape is not None
    if shape.parse_error or not shape.jobs:
        return Check(label="branch policy", state="INFO",
                     detail=f"could not extract review.yml job names: "
                            f"{shape.parse_error or 'no jobs'}")
    job_names = {j.name for j in shape.jobs if j.name}
    if not job_names:
        return Check(label="branch policy", state="INFO",
                     detail="review.yml jobs lack `name:` — bare-key matching required")
    missing = sorted(required - job_names)
    extra = sorted(job_names - required)
    if not missing and not extra:
        return Check(label="branch policy", state="PASS",
                     detail=f"required={sorted(required)}  workflow={sorted(job_names)}")
    return Check(label="branch policy", state="WARN",
                 detail=f"required-vs-workflow mismatch: "
                        f"required but not emitted by any review job={missing}; "
                        f"emitted by review but not required={extra}")
