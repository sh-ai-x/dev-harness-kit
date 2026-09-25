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


def check_open_pr(target: Path, gh_available_fn, Check: CheckFactory) -> list["object"]:
    """CONFLICTING → FAIL; UNKNOWN → WARN; MERGEABLE → PASS; otherwise INFO."""
    data, degraded = fetch_open_pr_state(target, gh_available_fn, Check)
    if degraded:
        return [Check(label="open PR state", state="SKIP", detail=degraded)]
    rows: list["object"] = []
    mergeable = data.get("mergeable", "")
    if mergeable == "CONFLICTING":
        rows.append(Check(label="open PR mergeable", state="FAIL",
            detail=("open PR has merge conflicts with main — CI will not run. "
                    "Run: git fetch origin main && git merge origin/main")))
    elif mergeable == "UNKNOWN":
        rows.append(Check(label="open PR mergeable", state="WARN",
            detail="GitHub still computing merge state — re-run /dev-kit:ci-doctor in 30s"))
    elif mergeable == "MERGEABLE":
        rows.append(Check(label="open PR mergeable", state="PASS", detail="no conflicts"))
    else:
        rows.append(Check(label="open PR mergeable", state="INFO",
            detail=f"unrecognized mergeable value: {mergeable!r}"))
    if data.get("isDraft"):
        rows.append(Check(label="open PR draft", state="INFO",
            detail="PR is a draft — required checks gated until marked ready for review"))
    if data.get("title", "").startswith("chore(release): bump dev-kit to v"):
        rows.append(Check(label="open PR title", state="INFO",
            detail=("bump-PR — ci/review/security explicitly skip per "
                    "templates/ci/.github/workflows/ci.yml")))
    return rows
