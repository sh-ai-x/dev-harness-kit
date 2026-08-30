"""git_worktree.py — canonical `git worktree add` helper (issue #310).

Centralizes every `git worktree add` invocation used by the harness into a
single helper so the "safe vs reset-branch vs overwrite" semantics live
in one place. Before this helper existed:

  * lib/acp_dispatch.py shipped its own `_cut_worktree` (safe mode: `-b`,
    fails closed when branch or worktree dir exists). This is the right
    contract for ACP dispatch — losing an existing branch would destroy
    another T's work.
  * lib/execute.py shipped its own inline `git worktree add -B ...`
    invocation in `_step_pre_spawn`. `-B` resets the branch ref to
    `origin/main`, which is the right contract for per-step harness
    runs — a previous failed run may have left a stale branch at the
    same name, and we want the new step to start from the tip of main.

Two callers, two intentional behaviors. Both routed through this helper
so future code can't quietly drift the contract. The contract is
parameterized rather than auto-detected because "what should happen when
the branch / dir already exists" is a policy decision, not a probe.

Public API:
  * `cut_worktree(...)` — cut one worktree.
  * `git_common_dir(...)` — resolve the git common dir for a path.
  * `list_worktree_dirs(...)` — enumerate worktree dirs under the
    canonical `.worktrees/` root.
  * `CutWorktreeResult` — frozen dataclass returned by `cut_worktree`.

Reused by (do not duplicate):
  * lib/acp_dispatch.py — `ACPDispatcher._cut_worktree`
  * lib/execute.py — `_step_pre_spawn` inline `git worktree add -B ...`
  * tools/token_efficiency_analyzer.py — `git_common_dir` (planned)
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Optional

#: Canonical worktree root under the repo (client-neutral, see
#: rules/git-workflow.md). Legacy ``.claude/worktrees`` and
#: ``.codex/worktrees`` are NOT enumerated by `list_worktree_dirs` —
#: dashboard side already handles them, and adding them here would
#: double-count.
WORKTREE_ROOT_NAME = ".worktrees"


@dataclasses.dataclass(frozen=True)
class CutWorktreeResult:
    """Outcome of a successful `cut_worktree` call.

    Attributes
    ----------
    branch : str
        The branch ref that the new worktree is on. Echoes the input so
        callers can log / dispatch without re-resolving.
    worktree_path : Path
        Absolute path of the new worktree. Same as the input.
    was_pre_existing : bool
        True when ``branch`` already existed at the start of the call
        (so ``reset_branch=True` had something to reset). False for a
        first-time cut. Used by `acp_dispatch` regression tests to
        confirm the safe-mode failure path did not destroy the
        pre-existing ref.
    """

    branch: str
    worktree_path: Path
    was_pre_existing: bool


def git_common_dir(repo_root: Path) -> Optional[Path]:
    """Resolve the git common dir for `repo_root` (handles linked worktrees).

    Returns None when `repo_root` is not inside a git working tree or
    when the underlying `git rev-parse --git-common-dir` call fails. The
    common dir is `<repo>/.git` for a normal checkout and the shared
    `<main>/.git` for any linked worktree — distinguishing the two is
    what worktree-guard uses to block edits in the main checkout.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    raw = completed.stdout.strip()
    if not raw:
        return None
    return Path(raw).resolve()


def list_worktree_dirs(repo_root: Path) -> list[Path]:
    """Enumerate worktree dirs under `<repo_root>/.worktrees/`.

    Sorted ascending by basename. Skips any non-directory children so
    stray files (a misplaced .DS_Store, an editor swap file) don't
    crash the caller. Returns [] when the root does not exist.
    """
    wt_root = Path(repo_root) / WORKTREE_ROOT_NAME
    if not wt_root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(wt_root.iterdir()):
        if child.is_dir():
            out.append(child)
    return out


def _branch_exists(repo_root: Path, branch: str,
                   git_runner=subprocess.run) -> bool:
    """Return True when `branch` already exists as a local ref.

    Used by `cut_worktree` to decide whether a failed
    `git worktree add -b` had a pre-existing branch that must NOT be
    cleaned up by the caller. Inspect 2026-08-27 overarch-2: the
    public alias `branch_exists` is the entry point external callers
    (e.g. `lib.acp_dispatch`) should use; this underscore form is kept
    as a back-compat alias for the in-package call site at line 223.
    """
    completed = git_runner(
        ["git", "-C", str(repo_root), "rev-parse", "--verify",
         f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    return completed.returncode == 0


# inspect 2026-08-27 overarch-2: public alias so callers outside
# `lib/` do not reach into the underscore-prefixed private symbol.
branch_exists = _branch_exists


def _remove_worktree_dir(repo_root: Path, worktree_path: Path,
                         git_runner=subprocess.run) -> None:
    """Best-effort removal of a worktree dir after a failed cut.

    Uses `git worktree remove --force` first so git's metadata stays
    consistent, then falls back to `shutil.rmtree` for the rare case
    where the worktree was never registered (e.g. `git worktree add`
    failed before git could open the dir).
    """
    completed = git_runner(
        ["git", "-C", str(repo_root), "worktree", "remove", "--force",
         str(worktree_path)],
        capture_output=True, text=True,
    )
    if completed.returncode != 0 and worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


def cut_worktree(
    repo_root: Path,
    branch: str,
    worktree_path: Path,
    *,
    base: str = "origin/main",
    reset_branch: bool = False,
    overwrite_worktree: bool = False,
    git_runner=subprocess.run,
) -> CutWorktreeResult:
    """Cut `git worktree add` in a canonical way.

    Parameters
    ----------
    repo_root : Path
        Absolute path of the orch / main checkout. All `git` commands
        run with `-C <repo_root>`.
    branch : str
        Target feature branch in `<type>/<slug>` form. Empty / None is
        rejected (a worktree without a branch is not a worktree).
    worktree_path : Path
        Absolute path for the new worktree.
    base : str, default ``"origin/main"``
        Branch ref / SHA the new worktree starts from. Override this
        when callers need to cut from a specific commit (e.g. an
        integration branch tip in a test fixture).
    reset_branch : bool, default ``False``
        False (safe, ``acp_dispatch``): use ``-b``. Fail if `branch`
        already exists. Pre-existing branches survive a failed cut.
        True (``execute`` per-step): use ``-B``. Reset `branch` to
        `base`. Allows reusing a branch name from a previous failed
        run so the next step starts at the tip of `base`.
    overwrite_worktree : bool, default ``False``
        False (safe): raise `FileExistsError` when `worktree_path`
        already exists on disk. True: best-effort remove the existing
        dir + ``git worktree remove --force`` before cutting.
    git_runner : callable, default ``subprocess.run``
        Subprocess runner for tests (real subprocess by default).

    Returns
    -------
    CutWorktreeResult
        ``CutWorktreeResult(branch, worktree_path, was_pre_existing)``.

    Raises
    ------
    FileExistsError
        `worktree_path` already exists and `overwrite_worktree=False`.
    subprocess.CalledProcessError
        `git worktree add` failed (raised by the ``check=True`` flag
        on the runner). Callers can inspect ``.stderr`` for git's
        diagnostic. Wrapping in RuntimeError would lose information
        when the caller re-raises — bare ``CalledProcessError`` keeps
        the call stack traceable to the failing git invocation.
        acp_dispatch and execute both re-raise verbatim after their
        own cleanup hooks run.
    ValueError
        `branch` or `worktree_path` is empty.
    """
    if not branch:
        raise ValueError("branch is required (got empty)")
    if not str(worktree_path):
        raise ValueError("worktree_path is required (got empty)")

    worktree_path = Path(worktree_path)
    repo_root = Path(repo_root)

    if worktree_path.exists():
        if not overwrite_worktree:
            raise FileExistsError(
                f"worktree path already exists: {worktree_path}. "
                f"Pick a unique slug or remove the stale dir."
            )
        # Overwrite mode: best-effort remove, then proceed.
        _remove_worktree_dir(repo_root, worktree_path, git_runner=git_runner)

    pre_existing_branch = _branch_exists(repo_root, branch, git_runner=git_runner)

    flag = "-B" if reset_branch else "-b"
    # ``check=True`` so a non-zero return propagates as
    # ``CalledProcessError`` — callers (acp_dispatch, execute) wrap
    # this in their own RuntimeError-with-context for the human
    # message, and tests that mock ``subprocess.run`` get the same
    # behavior as before: the mock returns a fake CompletedProcess
    # regardless of ``check=True``, so the runner keeps going.
    git_runner(
        ["git", "-C", str(repo_root), "worktree", "add", flag,
         branch, str(worktree_path), base],
        capture_output=True, text=True, check=True,
    )

    return CutWorktreeResult(
        branch=branch,
        worktree_path=worktree_path,
        was_pre_existing=pre_existing_branch,
    )


__all__ = [
    "CutWorktreeResult",
    "WORKTREE_ROOT_NAME",
    "branch_exists",
    "cut_worktree",
    "git_common_dir",
    "list_worktree_dirs",
]
