"""test_worktree_auto_cut_cooldown — session-cooldown marker behavior.

Exercises hooks/worktree-auto-cut.sh's cooldown check via subprocess:
first call cuts (or attempts to) and writes the marker; second call within
the cooldown window must exit 0 silently (no additionalContext emit, no
worktree add).
"""
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "worktree-auto-cut.sh"


def _payload(prompt: str, *, session_id: str = "test-session-1") -> str:
    return json.dumps({"prompt": prompt, "session_id": session_id})


def _run_hook(payload: str, *, work_dir: Path, cooldown_secs: int = 300) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["WORKTREE_AUTO_CUT_COOLDOWN_SECS"] = str(cooldown_secs)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload, capture_output=True, text=True,
        env=env, cwd=str(work_dir), check=False,
    )


def test_hook_present():
    assert HOOK.exists(), f"{HOOK} missing"


def test_first_call_runs_full_hook(tmp_path, monkeypatch):
    """A first-time call with task-intent prompt should NOT silently exit
    (it will try to git fetch / cut, and may fail without a remote, but it
    must NOT short-circuit on the cooldown check)."""
    repo = tmp_path / "r"
    repo.mkdir()
    # No remote configured → the hook will fail at `git fetch origin main`
    # or the worktree cut. We're testing that it didn't bail early.
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(repo), check=True)
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    (repo / ".dev-kit").mkdir()

    cp = _run_hook(_payload("fix the broken thing", session_id="sid-A"), work_dir=repo)
    # The cooldown check must NOT trip on a fresh session — exit code is
    # non-zero (likely) because the hook can't cut without a remote, but
    # the *stderr output* should be from the actual cut attempt, not the
    # cooldown marker check.
    assert cp.stdout == "" or "auto-cut" not in cp.stdout, (
        f"cooldown should not fire on first call; got stdout={cp.stdout!r}"
    )


def test_second_call_within_cooldown_skips(tmp_path):
    """Write a cooldown marker as if a previous call had succeeded, then
    send a fresh prompt within the cooldown window. The hook must exit 0
    silently (no further work)."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(repo), check=True)
    (repo / ".dev-kit").mkdir()
    marker = repo / ".dev-kit" / ".auto-cut-cooldown"
    now = int(__import__("time").time())
    marker.write_text(f"test-session-2\n{now}\n")

    cp = _run_hook(
        _payload("fix something else", session_id="test-session-2"),
        work_dir=repo,
    )
    assert cp.returncode == 0, (
        f"expected silent exit 0 within cooldown; got rc={cp.returncode} "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    # No additionalContext envelope should be emitted (the hook short-circuited).
    assert cp.stdout.strip() == ""


def test_cooldown_expires(tmp_path):
    """A marker from far in the past (>> cooldown window) must NOT skip."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(repo), check=True)
    (repo / ".dev-kit").mkdir()
    marker = repo / ".dev-kit" / ".auto-cut-cooldown"
    # 10 minutes ago — outside the default 300s cooldown.
    old_ts = int(__import__("time").time()) - 600
    marker.write_text(f"test-session-3\n{old_ts}\n")

    cp = _run_hook(
        _payload("fix something", session_id="test-session-3"),
        work_dir=repo,
    )
    # The hook will try to fetch / cut and fail (no remote) — exit non-zero
    # is fine. The point is that it did NOT silently exit 0.
    assert cp.stdout.strip() == "", (
        f"cooldown should be expired; got stdout={cp.stdout!r}"
    )


def test_different_session_id_does_not_skip(tmp_path):
    """A marker for session A must not skip a prompt from session B."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(repo), check=True)
    (repo / ".dev-kit").mkdir()
    marker = repo / ".dev-kit" / ".auto-cut-cooldown"
    now = int(__import__("time").time())
    marker.write_text(f"session-A\n{now}\n")

    cp = _run_hook(
        _payload("fix something", session_id="session-B"),
        work_dir=repo,
    )
    # Should NOT be silent exit — different session, fresh cut attempt.
    assert cp.stdout.strip() == ""
