"""Tests for `bin/install-pre-push.sh`.

Covers the AC contract from issue #833:

  * Idempotent — re-running with the same source produces no diff
    (or a "no-op" status line); the existing symlink is left in place.
  * Worktree-aware — resolves `core.hooksPath` (when set) or
    `--git-common-dir/hooks/` (when not), so a single install covers
    every worktree.
  * `--uninstall` removes the symlink.

Three fixtures:

  1. **core.hooksPath set** — installs into that path.
  2. **No core.hooksPath** — installs into `<git-common-dir>/hooks/`.
  3. **`--uninstall`** — removes the install cleanly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SRC = REPO_ROOT / "hooks" / "pre-push.sh"
INSTALL = REPO_ROOT / "bin" / "install-pre-push.sh"


# Shared identity for both `_make_repo` (its own `git commit`) and
# `_run_install` (the install script's own `git config`-side calls).
# CI runners leave `user.name` / `user.email` unset, which makes a bare
# `git commit` exit 128 with `fatal: empty ident name`. Setting the env
# vars + the global gitconfig pointers here guarantees the fixture works
# whether the parent process inherited those values from the host
# shell or not. The fixture is the source of truth; callers must
# inherit its env for any nested `subprocess.run` that touches git.
GIT_IDENTITY_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _run_install(repo: Path, extra_args: list[str] | None = None,
                 env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_IDENTITY_ENV}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(INSTALL), *(extra_args or [])],
        capture_output=True, text=True, env=env, cwd=str(repo),
        timeout=20, check=False,
    )


def _make_repo(tmp: Path) -> tuple[Path, Path]:
    """Create a tiny git repo with the source hook mirrored under ``hooks/``.

    Returns ``(repo, source_hook_path)`` so callers can assert the
    symlink resolves to the right target.
    """
    repo = tmp / "repo"
    repo.mkdir()
    env = {**os.environ, **GIT_IDENTITY_ENV}
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)],
                   check=True, env=env)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init", "--quiet"],
                   check=True, env=env)
    # Mirror the source hook so `install-pre-push.sh` can find it.
    hooks = repo / "hooks"
    hooks.mkdir()
    source_hook = hooks / "pre-push.sh"
    shutil.copy(HOOK_SRC, source_hook)
    return repo, source_hook


class CoreHooksPathTests(unittest.TestCase):
    """When `core.hooksPath` is set, install there."""

    def test_install_into_configured_hooks_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            repo, source_hook = _make_repo(tmpd)
            custom = tmpd / "custom-hooks"
            custom.mkdir()
            subprocess.run(
                ["git", "-C", str(repo), "config", "core.hooksPath", str(custom)],
                check=True,
            )
            cp = _run_install(repo)
            self.assertEqual(cp.returncode, 0,
                             msg=f"stderr={cp.stderr!r}")
            target = custom / "pre-push"
            self.assertTrue(target.is_symlink(),
                            msg=f"target={target} cp.stdout={cp.stdout!r}")
            # Symlink should resolve to our source hook. Use resolve()
            # to normalize macOS's /tmp → /private/tmp symlink.
            self.assertEqual(
                Path(os.readlink(str(target))).resolve(),
                source_hook.resolve(),
            )

    def test_install_idempotent(self):
        """Re-running with the same source → no-op, same symlink target."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            repo, source_hook = _make_repo(tmpd)
            custom = tmpd / "custom-hooks"
            custom.mkdir()
            subprocess.run(
                ["git", "-C", str(repo), "config", "core.hooksPath", str(custom)],
                check=True,
            )
            cp1 = _run_install(repo)
            self.assertEqual(cp1.returncode, 0)
            target = custom / "pre-push"
            self.assertTrue(target.is_symlink())
            # Re-run; expect "already installed" status and same target.
            cp2 = _run_install(repo)
            self.assertEqual(cp2.returncode, 0)
            self.assertIn("already installed", cp2.stdout)
            self.assertTrue(target.is_symlink())
            self.assertEqual(
                Path(os.readlink(str(target))).resolve(),
                source_hook.resolve(),
            )


class NoHooksPathTests(unittest.TestCase):
    """When `core.hooksPath` is NOT set, install into `--git-common-dir/hooks/`."""

    def test_install_into_common_dir_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            repo, _ = _make_repo(tmpd)
            # Do NOT set core.hooksPath — the script must fall back
            # to `<git-common-dir>/hooks/`.
            cp = _run_install(repo)
            self.assertEqual(cp.returncode, 0, msg=f"stderr={cp.stderr!r}")
            # Find the common hooks dir from the script's stdout.
            self.assertIn("/hooks/pre-push", cp.stdout)


class UninstallTests(unittest.TestCase):
    def test_uninstall_removes_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            repo, _ = _make_repo(tmpd)
            custom = tmpd / "custom-hooks"
            custom.mkdir()
            subprocess.run(
                ["git", "-C", str(repo), "config", "core.hooksPath", str(custom)],
                check=True,
            )
            cp = _run_install(repo)
            self.assertEqual(cp.returncode, 0)
            target = custom / "pre-push"
            self.assertTrue(target.exists())

            cp_un = _run_install(repo, ["--uninstall"])
            self.assertEqual(cp_un.returncode, 0)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
