"""tools/_repo_name.py — Resolve the canonical repository name from any worktree.

Used by both ``tools/linear_sync.py`` (Edit|Write auto-sync) and
``tools/linear_pr_sync.py`` (GH-Actions PR sync) so the Linear project
name always tracks the consumer repo's directory basename, never a
hardcoded fallback like ``"dev-harness-kit"``.

Returns the **main-checkout** basename even when called from a linked
worktree (``git rev-parse --git-common-dir`` resolves the main checkout
path). Hides a leading dot so ``.worktrees/foo`` returns ``foo`` and
the bare worktree directory returns the original repo name.

Failure modes (all silent fallthrough, never raises):

  - git binary absent / not a git repo / shallow clone edge case →
    returns ``repo`` itself, then ``repo.name``.
  - empty directory name → returns the literal string ``"repository"``,
    matching the legacy fallback in ``tools/linear_sync.py``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def main_repo_root(repo: Path) -> Path:
    """Return the main checkout's path, even from inside a linked worktree.

    Discriminator + resolution:
        - ``git rev-parse --git-common-dir`` returns the main ``.git/``
          from any worktree of the same repo.
        - ``dirname`` of that path is the main checkout.
        - In a non-worktree (or single-worktree) repo, the current
          ``repo`` is the main checkout; return it unchanged.
    """
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "ignore").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return repo
    if not common:
        return repo
    # ``git rev-parse --git-common-dir`` returns a path that may be
    # relative to the worktree root. Resolve it against the worktree.
    p = Path(common)
    if not p.is_absolute():
        p = (repo / p).resolve()
    return p.parent


def repo_name(repo: Path) -> str:
    """Canonical repository name = main-checkout basename.

    A worktree at ``.worktrees/fix-xxx/`` returns the main checkout's
    directory name (e.g. ``dev-harness-kit``), NOT the worktree's own
    basename. This is the name Linear projects follow per #539
    ("A repository whose Linear project name differs from its
    canonical repository name gets a project named exactly after the
    repository").
    """
    name = main_repo_root(repo).name
    if name.startswith(".") and len(name) > 1:
        name = name[1:]
    return name or "repository"
