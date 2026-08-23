"""Regression tests for lib.worktree_prune.

Covers the deterministic age-sort + main-checkout-exclusion contract:
  * collect() returns a list[Row] sorted oldest-first.
  * The main checkout is excluded from the candidate list.
  * Detached-HEAD worktrees are excluded (no branch → no stale-branch
    triage; janitor agent handles those).
  * The branch→epoch map is built from a single for-each-ref call
    (regression for the N-spawns performance issue, not a behavior
    assertion — the test just checks that the function returns a
    non-empty dict for a populated repo).
  * render_table() emits a header + one row per input, oldest first,
    columns aligned, no ANSI codes.
  * render_table() returns "" for an empty input.
  * Row.age_days() is (now - epoch) / 86400, with epoch=0 → 0.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "lib" / "worktree_prune.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("worktree_prune", SCRIPT)
    assert spec and spec.loader, f"could not load {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["worktree_prune"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wp():
    return _load_module()


# --- Row unit tests (no git) --------------------------------------------


class TestRowAgeDays:
    def test_known_epoch_returns_day_delta(self, wp):
        now = 1_700_000_000
        one_day_ago = now - 86_400
        assert wp.Row(path="/p", branch="b", epoch=one_day_ago, sha="abc").age_days(now) == 1

    def test_zero_epoch_returns_zero(self, wp):
        # Unknown epoch — surface as 0 rather than a huge number; the
        # script's audit table would otherwise show 50+ years for any
        # worktree whose branch has been deleted from refs/heads.
        assert wp.Row(path="/p", branch="b", epoch=0, sha="abc").age_days(1_700_000_000) == 0

    def test_future_epoch_clamped_to_zero(self, wp):
        # Clock skew between commits and "now" should not show as a
        # negative age. Document the behavior — we floor at 0 rather
        # than raise.
        now = 1_700_000_000
        future = now + 10_000
        assert wp.Row(path="/p", branch="b", epoch=future, sha="abc").age_days(now) == 0


# --- render_table unit tests (no git) -----------------------------------


class TestRenderTable:
    def test_empty_returns_empty_string(self, wp):
        assert wp.render_table([], 1_700_000_000) == ""

    def test_header_present_and_rows_ordered(self, wp):
        rows = [
            wp.Row(path="/wt/old", branch="feat/old", epoch=1_000_000, sha="aaa"),
            wp.Row(path="/wt/new", branch="feat/new", epoch=1_699_000_000, sha="bbb"),
        ]
        out = wp.render_table(rows, 1_700_000_000)
        lines = out.splitlines()
        assert lines[0].startswith("   #")  # header
        assert "AGE(d)" in lines[0]
        assert "feat/old" in lines[2]  # row 1
        assert "feat/new" in lines[3]  # row 2
        # Oldest first
        assert "feat/old" in lines[2]
        assert "feat/new" in lines[3]

    def test_no_ansi_codes(self, wp):
        rows = [wp.Row(path="/p", branch="b", epoch=0, sha="x")]
        out = wp.render_table(rows, 1_700_000_000)
        assert "\x1b[" not in out  # no ANSI escape sequences


# --- end-to-end against a real git repo ---------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    (repo / "README").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


def _add_worktree(repo: Path, branch: str, age_seconds: int = 0) -> Path:
    """Cut a worktree, then backdate the branch tip's committer date so
    tests can assert specific age ordering without sleeping."""
    wt = repo.parent / f"wt-{branch.replace('/', '-')}"
    if wt.exists():
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
            check=False, capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt), "-b", branch],
        check=True, capture_output=True,
    )
    if age_seconds:
        # Backdate by amending the initial commit. Two commits so the
        # worktree's branch tip (the new one) has a controllable date.
        (wt / "x").write_text(branch + "\n")
        subprocess.run(["git", "-C", str(wt), "add", "x"], check=True, capture_output=True)
        env = {
            "GIT_AUTHOR_DATE": f"@{1700000000 - age_seconds}",
            "GIT_COMMITTER_DATE": f"@{1700000000 - age_seconds}",
        }
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-q", "-m", branch],
            check=True, capture_output=True, env={**subprocess.os.environ, **env},
        )
    return wt


class TestCollectEndToEnd:
    def test_main_checkout_is_excluded(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        rows = wp.collect(str(repo))
        # Only the main checkout is registered → empty list.
        assert rows == []

    def test_worktrees_sorted_oldest_first(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        _add_worktree(repo, "feat/old", age_seconds=86_400 * 30)  # 30d old
        _add_worktree(repo, "feat/mid", age_seconds=86_400 * 7)   # 7d old
        _add_worktree(repo, "feat/new", age_seconds=86_400)        # 1d old

        rows = wp.collect(str(repo))
        branches = [r.branch for r in rows]
        assert branches == ["feat/old", "feat/mid", "feat/new"]

    def test_branch_epoch_map_built_from_for_each_ref(self, wp, tmp_path):
        # Sanity check: branch_map is non-empty for a repo with at least
        # one branch beyond main. This guards against a regression where
        # someone replaces for-each-ref with N git-log calls.
        repo = _make_git_repo(tmp_path)
        _add_worktree(repo, "feat/x")
        epoch_map = wp._branch_epoch_map(str(repo))
        assert "feat/x" in epoch_map
        assert epoch_map["feat/x"] > 0

    def test_detached_worktree_excluded(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        # Detached worktree: `git worktree add --detach`
        wt = repo.parent / "wt-detached"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt)],
            check=True, capture_output=True,
        )
        rows = wp.collect(str(repo))
        assert all(r.branch != "" for r in rows)
        assert not any(r.path == str(wt) for r in rows)


class TestCliModes:
    """End-to-end against `python3 -m lib.worktree_prune` as a subprocess.

    The bin script depends on three CLI modes — JSON (default), --table
    with a summary header the shell can grep, and --count for the
    short-circuit path. Each is exercised here so the shell contract
    can't drift from the Python implementation.
    """

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "lib.worktree_prune", "--repo", str(repo), *args],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )

    def test_table_mode_emits_summary_header(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        _add_worktree(repo, "feat/x")
        result = self._run(repo, "--table")
        assert result.returncode == 0, result.stderr
        first = result.stdout.splitlines()[0]
        assert first.startswith("Worktrees registered: 1")
        assert "(excluding main checkout)" in first

    def test_table_mode_empty_repo_says_zero(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        result = self._run(repo, "--table")
        assert result.returncode == 0
        assert "Worktrees registered: 0" in result.stdout

    def test_count_mode_prints_integer(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        _add_worktree(repo, "feat/a")
        _add_worktree(repo, "feat/b")
        result = self._run(repo, "--count")
        assert result.returncode == 0
        assert result.stdout.strip() == "2"

    def test_default_mode_emits_json(self, wp, tmp_path):
        repo = _make_git_repo(tmp_path)
        _add_worktree(repo, "feat/json")
        result = self._run(repo)
        assert result.returncode == 0
        rows = json.loads(result.stdout)
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert rows[0]["branch"] == "feat/json"
        assert set(rows[0].keys()) == {"path", "branch", "epoch", "sha"}

    def test_table_head_mode_renders_first_n_rows(self, wp, tmp_path):
        # Regression for review finding #1 (PR #721): the shell script
        # uses --table --head N to render the "Will remove" preview so
        # it shares the audit-table format instead of duplicating the
        # Python heredoc. Without --head the table prints all rows;
        # with --head N, only the first N.
        repo = _make_git_repo(tmp_path)
        _add_worktree(repo, "feat/a", age_seconds=86_400 * 10)  # oldest
        _add_worktree(repo, "feat/b", age_seconds=86_400 * 5)
        _add_worktree(repo, "feat/c", age_seconds=86_400)        # newest

        result = self._run(repo, "--table", "--head", "1")
        assert result.returncode == 0
        body = result.stdout
        # Header is preserved (still the same audit-table format).
        assert "Worktrees registered: 3" in body
        assert "AGE(d)" in body
        assert "BRANCH" in body
        # Only the oldest row is in the body.
        assert "feat/a" in body
        assert "feat/b" not in body
        assert "feat/c" not in body

    def test_render_head_table_helper_matches_full_table_prefix(self, wp):
        # Pure-Python check that render_head_table(n=...) is exactly
        # the first n rows of render_table(all).
        rows = [
            wp.Row(path="/wt/a", branch="feat/a", epoch=1_000_000, sha="aaa"),
            wp.Row(path="/wt/b", branch="feat/b", epoch=1_500_000, sha="bbb"),
            wp.Row(path="/wt/c", branch="feat/c", epoch=1_699_000_000, sha="ccc"),
        ]
        now = 1_700_000_000
        full = wp.render_table(rows, now).split("\n")
        head = wp.render_head_table(rows, now, 2).split("\n")
        # head_table includes header + separator + N data rows = first N+2 lines.
        assert head == full[:4]
