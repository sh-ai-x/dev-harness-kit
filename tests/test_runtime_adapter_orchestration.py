#!/usr/bin/env python3
"""Focused tests for runtime workspace and skill-installation boundaries."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.runtime_adapters.skill_install import (  # noqa: E402
    SkillInstaller,
    install_skill,
)
from lib.runtime_adapters.workspace import (  # noqa: E402
    DefaultWorkspaceResolver,
    Workspace,
    project_root,
    worktree_for,
)


class TestWorkspaceResolution(unittest.TestCase):
    def test_project_root_precedence_explicit_environment_then_cwd(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            explicit = base / "explicit"
            from_env = base / "environment"
            cwd = base / "cwd"
            explicit.mkdir()
            from_env.mkdir()
            cwd.mkdir()

            with mock.patch.dict("os.environ", {"PROJECT_DIR": str(from_env)}, clear=True):
                with mock.patch("lib.runtime_adapters.workspace.Path.cwd", return_value=cwd):
                    self.assertEqual(project_root(explicit=explicit), explicit.resolve())
                    self.assertEqual(project_root(), from_env.resolve())

            with mock.patch.dict("os.environ", {}, clear=True):
                with mock.patch("lib.runtime_adapters.workspace.Path.cwd", return_value=cwd):
                    self.assertEqual(project_root(), cwd.resolve())

    def test_worktree_for_walks_up_to_git_file_marker(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            main_root = base / "main"
            worktree = main_root / "worktrees" / "feature"
            worktree.mkdir(parents=True)
            shared_git = main_root / ".git" / "worktrees" / "feature"
            shared_git.mkdir(parents=True)
            (worktree / ".git").write_text(
                f"gitdir: {shared_git}\n", encoding="utf-8"
            )
            nested = worktree / "deep" / "nested"
            nested.mkdir(parents=True)

            self.assertEqual(worktree_for(nested, main_root), worktree.resolve())

    def test_worktree_for_returns_none_outside_a_known_worktree(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            main_root = base / "main"
            main_root.mkdir()
            unrelated = base / "unrelated"
            unrelated.mkdir()

            self.assertIsNone(worktree_for(unrelated, main_root))

    def test_workspace_resolver_reports_explicit_inputs_deterministically(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            explicit = base / "explicit"
            explicit.mkdir()
            nested = explicit / "deep" / "nested"
            nested.mkdir(parents=True)

            resolver = DefaultWorkspaceResolver(explicit=explicit, cwd=nested)
            workspace = resolver.resolve()

            self.assertEqual(workspace.project_root, explicit.resolve())
            self.assertEqual(workspace.cwd, nested.resolve())
            self.assertEqual(workspace.worktree_root, explicit.resolve())

    def test_workspace_dataclass_exposes_resolved_paths(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            explicit = base / "explicit"
            explicit.mkdir()

            workspace = Workspace(
                project_root=explicit,
                cwd=explicit,
                worktree_root=explicit,
            )

            self.assertEqual(workspace.project_root, explicit.resolve())
            self.assertEqual(workspace.cwd, explicit.resolve())
            self.assertEqual(workspace.worktree_root, explicit.resolve())


class TestSkillInstallation(unittest.TestCase):
    def test_install_skill_uses_injected_installer(self):
        with tempfile_temporary_directory() as tmp:
            source = Path(tmp) / "review"
            source.mkdir()
            calls = []

            install_skill(
                "review",
                source,
                installer=lambda name, path: calls.append((name, path)),
            )

            self.assertEqual(calls, [("review", source.resolve())])

    def test_install_skill_rejects_traversal_unsafe_skill_dir(self):
        with tempfile_temporary_directory() as tmp:
            unsafe = Path(tmp).parent / "escape"
            unsafe.mkdir(parents=True, exist_ok=True)
            with self.assertRaisesRegex(ValueError, "must be inside"):
                install_skill("review", unsafe, installer=lambda *_args: None, allowed_root=Path(tmp))

    def test_install_skill_rejects_non_existent_skill_dir(self):
        with tempfile_temporary_directory() as tmp:
            missing = Path(tmp) / "missing"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                install_skill("review", missing, installer=lambda *_args: None)

    def test_install_skill_rejects_skill_dir_outside_allowed_root(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            allowed = base / "allowed"
            outside = base / "outside"
            allowed.mkdir()
            outside.mkdir()

            with self.assertRaisesRegex(ValueError, "must be inside"):
                install_skill(
                    "review",
                    outside,
                    installer=lambda *_args: None,
                    allowed_root=allowed,
                )

    def test_install_skill_keeps_skill_dir_inside_allowed_root(self):
        with tempfile_temporary_directory() as tmp:
            base = Path(tmp)
            allowed = base / "marketplace"
            skill_dir = allowed / "dev-kit" / "skills" / "review"
            skill_dir.mkdir(parents=True)
            calls = []

            install_skill(
                "review",
                skill_dir,
                installer=lambda name, path: calls.append((name, path)),
                allowed_root=allowed,
            )

            self.assertEqual(calls, [("review", skill_dir.resolve())])

    def test_install_skill_rejects_non_callable_installer(self):
        with tempfile_temporary_directory() as tmp:
            with self.assertRaisesRegex(TypeError, "callable"):
                install_skill("review", Path(tmp), installer=object())

    def test_skill_installer_accepts_callable(self):
        def installer(_name: str, _dir: Path) -> None:
            return None

        self.assertTrue(callable(installer))
        self.assertEqual(SkillInstaller.__args__[:2], (str, Path))  # type: ignore[attr-defined]


def tempfile_temporary_directory():
    import tempfile
    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
