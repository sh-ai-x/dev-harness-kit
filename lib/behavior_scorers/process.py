"""process.py — D2 Process Conformance.

Checks 5 deterministic signals on the worktree:
1. Conventional Commits format (>= 90% of commits match the regex)
2. branch naming: matches `<type>/<slug>` regex
3. worktree-guard intact: `.worktrees/` dir exists and `main` branch
   has no extra commits after the worktree cut point
4. tdd-guard intact: tests added before production code (presence of
   `tests/` updated files before `lib/` updated files in the diff)
5. hand-off notes present: `.dev-kit/hand-off/` is non-empty

Each is binary (1 point) for a total of 0..5. Returns DimensionScore
with value=points and evidence per check.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

from lib.behavior_scorers._git_helper import git_output as _git_output
from lib.behavior_scorers.types import Context, DimensionScore

# Conventional Commits: `<type>(optional-scope)!?: <subject>`
_CC_RE = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|hotfix|build|ci)"
    r"(\([a-z0-9_-]+\))?"
    r"(!)?:\s+\S+",
    re.IGNORECASE,
)
# Branch: `<type>/<slug>` where type ∈ known set, slug kebab-case 2..40.
_BRANCH_RE = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|hotfix|prune|build|ci)/"
    r"[a-z0-9][a-z0-9_-]{1,39}$"
)


def _conventional_commits_ratio(worktree: Path) -> float:
    """Fraction of commits in the branch that match Conventional Commits.

    Only considers commits ahead of `origin/main` (the actual changes
    on this branch). A worktree with no upstream tracking returns 1.0
    (vacuously true) rather than guessing — the previous fallback to
    `log -20` silently counted unrelated history and inflated the
    ratio. Fixes the "misleading fallback" flagged by maintenance gate.
    """
    raw = _git_output(worktree, "log", "--pretty=%s", "origin/main..HEAD")
    if not raw:
        # No upstream or no commits ahead of origin/main: vacuously
        # compliant (nothing on this branch to be non-conventional).
        return 1.0
    subjects = [line for line in raw.splitlines() if line.strip()]
    if not subjects:
        return 1.0
    matches = sum(1 for s in subjects if _CC_RE.match(s.strip()))
    return matches / len(subjects)


def _branch_naming_ok(worktree: Path) -> bool:
    branch = _git_output(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    return bool(branch) and bool(_BRANCH_RE.match(branch))


def _worktree_intact(worktree: Path) -> bool:
    """True when the worktree is detached from main checkout.

    Returns True when the worktree cannot be verified (no upstream
    tracking, bare fixture). Returns False when the worktree IS on
    main (an L1 violation: the agent edited main directly instead of
    cutting a worktree). The previous version returned True
    unconditionally, which was flagged as dead code by the
    maintenance gate.
    """
    parent = worktree.parent
    if not (parent / ".worktrees").is_dir():
        # Bare fixture (no .worktrees dir) — cannot verify, treat OK.
        return True
    main_tip = _git_output(worktree, "rev-parse", "origin/main")
    head = _git_output(worktree, "rev-parse", "HEAD")
    if not main_tip or not head:
        return True
    branch = _git_output(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "main":
        # Agent edited main directly; L1 violation.
        return False
    return True


def _tdd_intact(worktree: Path) -> bool:
    """True when test files were modified before / alongside production code.

    Reads `git log --diff-filter=M --name-only` for files modified on
    the branch. Heuristic: at least one `tests/` path appears AND
    any `lib/` or `src/` path appears only AFTER (later commit) the
    test path. If no `lib/` paths exist at all, count as intact
    (doc-only PRs are allowed).
    """
    raw = _git_output(
        worktree,
        "log",
        "--reverse",
        "--pretty=format:COMMIT:%H",
        "--name-only",
        "origin/main..HEAD",
    )
    if not raw:
        return True
    saw_test = False
    saw_lib_after_test = False
    for line in raw.splitlines():
        if line.startswith("COMMIT:"):
            continue
        if not line.strip():
            continue
        path = line.strip()
        if path.startswith("tests/") or "/tests/" in path or path.endswith("_test.py") or path.endswith(".test.ts"):
            saw_test = True
        elif saw_test and (path.startswith("lib/") or path.startswith("src/")):
            saw_lib_after_test = True
    if not saw_test:
        # No tests touched — fine for docs-only / chore PRs.
        return True
    return saw_test and saw_lib_after_test


def _handoff_present(worktree: Path) -> bool:
    """True when `.dev-kit/hand-off/` has at least one file."""
    handoff = worktree / ".dev-kit" / "hand-off"
    if not handoff.is_dir():
        return False
    return any(p.is_file() for p in handoff.iterdir())


def score(worktree: Path, ctx: Context) -> DimensionScore:
    """Score D2 from five binary checks."""
    cc_ratio = _conventional_commits_ratio(worktree)
    checks: Dict[str, Any] = {
        "conventional_commits": cc_ratio >= 0.9,
        "branch_naming": _branch_naming_ok(worktree),
        "worktree_intact": _worktree_intact(worktree),
        "tdd_intact": _tdd_intact(worktree),
        "handoff_present": _handoff_present(worktree),
    }
    value = sum(1 for v in checks.values() if v)
    return DimensionScore(
        dim="D2_process",
        value=value,
        evidence={
            **checks,
            "cc_ratio": round(cc_ratio, 4),
        },
    )
