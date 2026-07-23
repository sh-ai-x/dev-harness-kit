"""Runtime-neutral skill installation boundary."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

SkillInstaller = Callable[[str, Path], None]


def install_skill(
    skill_name: str,
    skill_dir: Path,
    *,
    installer: SkillInstaller | None = None,
    allowed_root: Path | None = None,
) -> Path:
    """Validate ``skill_dir`` and dispatch to an injected installer.

    The function never invokes any runtime marketplace or Codex registration
    tool directly. It only validates inputs and hands the resolved path to the
    injected ``installer`` callable.
    """
    if not isinstance(skill_name, str) or not skill_name or skill_name in {".", ".."}:
        raise ValueError("invalid skill_name")
    if Path(skill_name).name != skill_name or ".." in Path(skill_name).parts:
        raise ValueError("invalid skill_name")
    if not isinstance(skill_dir, Path):
        raise ValueError("skill_dir must be a Path")
    if not skill_dir.is_absolute():
        raise ValueError("skill_dir must be absolute")
    if not skill_dir.exists():
        raise ValueError(f"skill_dir does not exist: {skill_dir}")
    resolved = skill_dir.resolve()

    if allowed_root is not None:
        root = allowed_root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("skill_dir must be inside the allowed root") from exc

    if installer is None:
        raise RuntimeError("skill installer is not configured")
    if not callable(installer):
        raise TypeError("skill installer must be callable")

    installer(skill_name, resolved)
    return resolved


__all__ = ["SkillInstaller", "install_skill"]
