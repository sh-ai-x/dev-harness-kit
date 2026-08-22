"""Regression tests for tools.worktree_cleanup (issue #689 Phase 2).

Covers the cleanup-safe archival contract:
  * Idempotent — second invocation is a no-op once source is gone
  * Fail-safe — never raises; returns a status dict
  * AGENT_LOG_ROOT override works
  * Default archive target is main checkout's logs/.archive/<branch>/<ts>/
  * No logs/ in worktree → status="skipped"
  * Non-worktree input → status="skipped" (or "error" under --strict)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "tools" / "worktree_cleanup.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wc", SCRIPT)
    assert spec and spec.loader, f"could not load {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wc"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wc():
    return _load_module()


# --- helpers -------------------------------------------------------------


def _make_worktree(main: Path, branch: str = "feat-test") -> Path:
    """Cut a real git worktree of ``main`` on ``branch`` and return the path.

    Uses ``git worktree add`` so the .git file/symlink setup is realistic.
    The caller is responsible for ``git worktree remove`` after the test.
    """
    wt_path = main.parent / "wt-fixture"
    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(main), "worktree", "remove", "--force", str(wt_path)],
            check=False, capture_output=True, text=True,
        )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", branch],
        check=True, capture_output=True, text=True,
    )
    return wt_path


def _seed_logs(worktree: Path, files: int = 3) -> list[Path]:
    """Populate ``<worktree>/logs/claude-code/main/`` with ``files`` JSONL
    files and return the list of paths created.
    """
    log_dir = worktree / "logs" / "claude-code" / "main"
    log_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(files):
        p = log_dir / f"session-{i}.jsonl"
        p.write_text(f'{{"session_id": "{i}", "content": "fixture-{i}"}}\n')
        paths.append(p)
    return paths


# --- unit tests ----------------------------------------------------------


class TestResolveArchiveRoot:
    def test_uses_agent_log_root_when_set(self, wc, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_LOG_ROOT", str(tmp_path / "external"))
        root, external = wc._resolve_archive_root(str(tmp_path / "repo"))
        assert external is True
        assert root == tmp_path / "external" / "repo" / ".archive"

    def test_defaults_to_main_checkout(self, wc, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_LOG_ROOT", raising=False)
        root, external = wc._resolve_archive_root(str(tmp_path / "repo"))
        assert external is False
        assert root == tmp_path / "repo" / "logs" / ".archive"


class TestArchiveWorktreeLogs:
    def test_skips_when_no_logs_dir(self, wc, tmp_path):
        result = wc.archive_worktree_logs(str(tmp_path / "no-logs"), main_root=str(tmp_path))
        assert result["status"] == "skipped"
        assert result["files_copied"] == 0
        assert result["worktree_logs"] is None

    def test_copies_logs_to_main_archive(self, wc, tmp_path):
        main = tmp_path / "repo"
        main.mkdir()
        wt = main / "wt"
        wt.mkdir()
        (wt / "logs" / "codex" / "fix-x").mkdir(parents=True)
        (wt / "logs" / "codex" / "fix-x" / "s1.jsonl").write_text("x\n")
        (wt / "logs" / "codex" / "fix-x" / "s2.jsonl").write_text("y\n")

        result = wc.archive_worktree_logs(str(wt), main_root=str(main))

        assert result["status"] == "ok"
        assert result["files_copied"] == 2
        # Default archive target = <main>/logs/.archive/<branch>/<ts>/
        target = Path(result["archive_target"])
        assert target.parent.parent == main / "logs" / ".archive"
        copied = sorted(p.name for p in target.rglob("*.jsonl"))
        assert copied == ["s1.jsonl", "s2.jsonl"]

    def test_external_root_overrides_default(self, wc, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_LOG_ROOT", str(tmp_path / "external"))
        main = tmp_path / "repo"
        main.mkdir()
        wt = main / "wt"
        wt.mkdir()
        (wt / "logs" / "claude-code" / "main").mkdir(parents=True)
        (wt / "logs" / "claude-code" / "main" / "s.jsonl").write_text("x\n")

        result = wc.archive_worktree_logs(str(wt), main_root=str(main))

        assert result["external_root"] is True
        target = Path(result["archive_target"])
        assert target == tmp_path / "external" / "repo" / ".archive" / "no-git" / target.name
        assert (target / "claude-code" / "main" / "s.jsonl").exists()

    def test_strict_flag_returns_error_status_for_non_worktree(self, wc, tmp_path):
        # A plain directory with logs/ but no .git file. Non-strict skips
        # silently (no main detected); strict treats this as an error.
        wt = tmp_path / "standalone"
        wt.mkdir()
        (wt / "logs" / "claude-code" / "main").mkdir(parents=True)
        (wt / "logs" / "claude-code" / "main" / "s.jsonl").write_text("x\n")

        loose = wc.archive_worktree_logs(str(wt), strict=False)
        strict = wc.archive_worktree_logs(str(wt), strict=True)

        assert loose["status"] == "skipped"
        assert strict["status"] == "error"
        assert "not a registered git worktree" in strict["error"]

    def test_idempotent_after_source_removed(self, wc, tmp_path):
        # Simulate the post-removal replay: source gone, archive already
        # in place. Function returns a clean skip; no exception, no error.
        main = tmp_path / "repo"
        main.mkdir()
        wt = main / "wt"
        # Note: wt is intentionally not created — caller already ran
        # `git worktree remove`.
        result = wc.archive_worktree_logs(str(wt), main_root=str(main))
        assert result["status"] == "skipped"
        assert result["files_copied"] == 0


class TestRealGitWorktree:
    """End-to-end against a real git worktree — proves the .git file
    parsing and branch detection work in a real checkout."""

    def test_real_worktree_archive(self, wc, tmp_path):
        main = tmp_path / "repo"
        main.mkdir()
        subprocess.run(["git", "init", "-q", "--initial-branch=main", str(main)], check=True)
        subprocess.run(
            ["git", "-C", str(main), "config", "user.email", "test@test"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(main), "config", "user.name", "test"],
            check=True, capture_output=True,
        )
        # Seed an initial commit so worktree add can branch off it.
        (main / "README").write_text("hello\n")
        subprocess.run(["git", "-C", str(main), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(main), "commit", "-q", "-m", "init"], check=True)

        wt = _make_worktree(main, branch="feat-cleanup-test")
        try:
            _seed_logs(wt, files=2)
            result = wc.archive_worktree_logs(str(wt), main_root=str(main))

            assert result["status"] == "ok"
            assert result["branch"] == "feat-cleanup-test"
            assert result["files_copied"] == 2
            # Archive landed under <main>/logs/.archive/feat-cleanup-test/<ts>/
            target = Path(result["archive_target"])
            assert target.parent.name == "feat-cleanup-test"
            assert target.parent.parent == main / "logs" / ".archive"
            # Files preserved their relative path under logs/.
            assert (target / "claude-code" / "main" / "session-0.jsonl").exists()
            assert (target / "claude-code" / "main" / "session-1.jsonl").exists()
        finally:
            subprocess.run(
                ["git", "-C", str(main), "worktree", "remove", "--force", str(wt)],
                check=False, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(main), "branch", "-D", "feat-cleanup-test"],
                check=False, capture_output=True,
            )
