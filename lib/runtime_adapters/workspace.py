"""Runtime-neutral workspace and worktree resolution."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceResolver(Protocol):
    """Project, cwd, and worktree roots for the running runtime."""

    def project_root(self) -> Path: ...

    def cwd(self) -> Path: ...

    def worktree_root(self) -> Path: ...


@dataclass(frozen=True, slots=True)
class Workspace:
    """Resolved project, cwd, and worktree paths."""

    project_root: Path
    cwd: Path
    worktree_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", self.project_root.resolve())
        object.__setattr__(self, "cwd", self.cwd.resolve())
        object.__setattr__(self, "worktree_root", self.worktree_root.resolve())

    @classmethod
    def from_resolver(cls, resolver: WorkspaceResolver) -> "Workspace":
        return cls(
            project_root=resolver.project_root(),
            cwd=resolver.cwd(),
            worktree_root=resolver.worktree_root(),
        )


MainRootFinder = Callable[[str], str | None]
WorktreeFinder = Callable[[str, str], str | None]


class DefaultWorkspaceResolver:
    """Resolve project, cwd, and worktree roots deterministically."""

    def __init__(
        self,
        *,
        explicit: Path | None = None,
        cwd: Path | None = None,
        project_env: str = "PROJECT_DIR",
        main_root_finder: MainRootFinder | None = None,
        worktree_finder: WorktreeFinder | None = None,
    ) -> None:
        self._explicit = explicit
        self._cwd = cwd
        self._project_env = project_env
        self._main_root_finder = main_root_finder or _git_common_dir
        self._worktree_finder = worktree_finder or _find_worktree_for_cwd

    def project_root(self) -> Path:
        if self._explicit is not None:
            return self._explicit.resolve()
        env_value = os.environ.get(self._project_env)
        if env_value:
            return Path(env_value).resolve()
        return self.cwd_root().resolve()

    def cwd(self) -> Path:
        return self.cwd_root().resolve()

    def cwd_root(self) -> Path:
        return self._cwd or Path.cwd()

    def worktree_root(self) -> Path:
        cwd = self.cwd_root()
        main_root = self._main_root_finder(str(cwd))
        if main_root is None:
            return self.project_root()
        worktree = self._worktree_finder(str(cwd), main_root)
        if worktree is None:
            return Path(main_root).resolve()
        return Path(worktree).resolve()

    def resolve(self) -> Workspace:
        return Workspace.from_resolver(self)


def project_root(
    *,
    explicit: Path | None = None,
    project_env: str = "PROJECT_DIR",
) -> Path:
    """Return the resolved project root from the precedence chain."""
    if explicit is not None:
        return explicit.resolve()
    env_value = os.environ.get(project_env)
    if env_value:
        return Path(env_value).resolve()
    return Path.cwd().resolve()


def worktree_for(
    cwd: str | os.PathLike[str],
    main_root: str | os.PathLike[str],
) -> Path | None:
    """Return the worktree directory containing ``cwd`` if any."""
    try:
        start = Path(cwd).resolve()
    except OSError:
        return None
    main_root_path = Path(main_root).resolve()
    worktrees_dir = (main_root_path / ".git" / "worktrees").resolve()

    cursor = start
    while True:
        git_path = cursor / ".git"
        if git_path.is_file():
            try:
                content = git_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            if not content.startswith("gitdir: "):
                return None
            gitdir_raw = content[len("gitdir: "):].strip()
            if os.path.isabs(gitdir_raw):
                gitdir = Path(gitdir_raw).resolve()
            else:
                relative = Path(gitdir_raw)
                gitdir = (cursor / relative).resolve()
            try:
                gitdir.relative_to(worktrees_dir)
            except ValueError:
                return None
            return cursor
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent


def _git_common_dir(cwd: str) -> str | None:
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    common = out.stdout.strip()
    if not common:
        return None
    common_path = common if os.path.isabs(common) else os.path.normpath(os.path.join(cwd, common))
    parent = os.path.dirname(common_path)
    return parent if parent and os.path.isdir(parent) else None


def _find_worktree_for_cwd(cwd: str, main_root: str) -> str | None:
    worktree = worktree_for(cwd, main_root)
    return str(worktree) if worktree is not None else None


__all__ = [
    "DefaultWorkspaceResolver",
    "Workspace",
    "WorkspaceResolver",
    "project_root",
    "worktree_for",
]
