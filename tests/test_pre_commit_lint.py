from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
HOOK = ROOT / ".githooks" / "pre-commit"
RUFF_CONFIG = ROOT / ".ruff.toml"


def _init_tmp_git_repo() -> tempfile.TemporaryDirectory:
    """Create a throwaway Git repo with the repository Ruff configuration."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    shutil.copy2(RUFF_CONFIG, root / ".ruff.toml")
    (root / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(root), "add", ".ruff.toml", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, capture_output=True)
    return tmp


def _run_hook(root: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def _path_without_ruff(root: Path) -> dict[str, str]:
    bin_dir = root / "bin-without-ruff"
    bin_dir.mkdir()
    for command in ("bash", "git", "grep"):
        executable = shutil.which(command)
        if executable is None:
            raise RuntimeError(f"required test command not found: {command}")
        (bin_dir / command).symlink_to(executable)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    return env


class TestPreCommitLint(unittest.TestCase):
    def test_clean_staged_py_passes(self):
        if shutil.which("ruff") is None:
            self.skipTest("ruff is not installed")
        with _init_tmp_git_repo() as directory:
            root = Path(directory)
            (root / "clean.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "-C", str(root), "add", "clean.py"], check=True)

            result = _run_hook(root)

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_F401_blocks_with_actionable_stderr(self):
        if shutil.which("ruff") is None:
            self.skipTest("ruff is not installed")
        with _init_tmp_git_repo() as directory:
            root = Path(directory)
            (root / "dirty.py").write_text("import os\n")
            subprocess.run(["git", "-C", str(root), "add", "dirty.py"], check=True)

            result = _run_hook(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dirty.py", result.stderr)
            self.assertIn("F401", result.stderr)
            self.assertIn("commit blocked", result.stderr)
            self.assertIn("ruff check --fix", result.stderr)
            self.assertIn("git commit --no-verify", result.stderr)

    def test_lints_staged_blob_not_worktree(self):
        if shutil.which("ruff") is None:
            self.skipTest("ruff is not installed")
        with _init_tmp_git_repo() as directory:
            root = Path(directory)
            (root / "staged.py").write_text("import os\n")
            subprocess.run(["git", "-C", str(root), "add", "staged.py"], check=True)
            (root / "staged.py").write_text("VALUE = 1\n")

            result = _run_hook(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("staged.py", result.stderr)
            self.assertIn("F401", result.stderr)

    def test_blocks_conflict_markers_in_any_staged_blob(self):
        with _init_tmp_git_repo() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("<<<<<<< HEAD\nconflict\n=======\nother\n>>>>>>> branch\n")
            subprocess.run(["git", "-C", str(root), "add", "notes.txt"], check=True)
            (root / "notes.txt").write_text("clean\n")

            result = _run_hook(root, env=_path_without_ruff(root))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("conflict marker", result.stderr.lower())
            self.assertIn("notes.txt", result.stderr)

    def test_no_staged_py_is_noop_even_without_ruff(self):
        with _init_tmp_git_repo() as directory:
            root = Path(directory)

            result = _run_hook(root, env=_path_without_ruff(root))

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_ruff_emits_actionable_error(self):
        with _init_tmp_git_repo() as directory:
            root = Path(directory)
            (root / "clean.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "-C", str(root), "add", "clean.py"], check=True)

            result = _run_hook(root, env=_path_without_ruff(root))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("::error::", result.stderr)
            self.assertIn("ruff is required", result.stderr)
            self.assertIn("brew install ruff", result.stderr)
            self.assertIn("apt install ruff", result.stderr)


if __name__ == "__main__":
    unittest.main()
