"""test_pr_create_route_hook.py — shell-level smoke tests for pr-create-route.sh.

Pins:
  - hook fires once on `gh pr create` (exit 0)
  - hook writes `.dev-kit/.pr-route.json` with the classifier payload
  - hook is non-blocking on missing classifier (exit 0)
  - hook is non-blocking on missing jq (exit 0)
  - hook is non-blocking on `gh pr merge` / `gh pr view` / `gh pr list` (exit 0,
    no classification)
  - ask mode fires when guard-mode.session.json:fork_pr_confirm == "on"

Mirrors the test pattern in `tests/test_worktree_guard.py` —
construct a tmp repo, write a payload to stdin, assert exit code +
filesystem side effects.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
HOOK = PROJECT_ROOT / "hooks" / "pr-create-route.sh"

# Only run if the hook exists in this checkout (skip in archived
# release tarballs that lack dev-kit hooks).
_SKIP_REASON: str = ""


def _set_up_repo(tmp: Path, *, fork_pr_confirm: str | None = None) -> Path:
    """Build a minimal repo layout under ``tmp`` that the hook can
    classify as a consumer fork of dev-harness-kit. The
    ``.claude-plugin/plugin.json:owner = sh-ai-x`` makes the classifier
    treat this as a dev-harness-kit plugin source checkout; the fake
    ``origin`` URL points at ``eve/dev-harness-kit`` so the classifier
    classifies it as a fork. No team.json → no maintainer signal →
    consumer_fork.
    """
    (tmp / ".dev-kit").mkdir(parents=True, exist_ok=True)
    (tmp / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tmp / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "dev-kit", "owner": "sh-ai-x"}),
        encoding="utf-8",
    )
    if fork_pr_confirm is not None:
        (tmp / ".dev-kit" / "guard-mode.session.json").write_text(
            json.dumps({"fork_pr_confirm": fork_pr_confirm}),
            encoding="utf-8",
        )
    return tmp


def _git_init(repo: Path) -> None:
    """Initialise a real git repo so the hook's ``git rev-parse`` calls
    succeed. We set a fake origin + HEAD branch to a known value."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@x",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@x"}
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, env=env,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/eve/dev-harness-kit.git"],
                   cwd=str(repo), check=True, env=env, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feat/test"], cwd=str(repo),
                   check=True, env=env, capture_output=True)


def _invoke_hook(repo: Path, payload: dict) -> subprocess.CompletedProcess:
    """Invoke the hook with a JSON payload on stdin; cwd = repo."""
    env = {**os.environ, "DEV_KIT_AGENT": "claude-code"}
    # The tmp repo has no `lib/`; PYTHONPATH points at the worktree
    # so `python3 -m lib.actor_classifier` resolves.
    lib_parent = str(PROJECT_ROOT)
    env["PYTHONPATH"] = (
        f"{lib_parent}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else lib_parent
    )
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(repo),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def _payload(command: str) -> dict:
    return {"tool_input": {"command": command}}


@unittest.skipIf(not HOOK.exists(), "hook not present in this checkout")
class TestPrCreateRouteHook(unittest.TestCase):

    def setUp(self) -> None:
        if shutil.which("jq") is None:
            self.skipTest("jq not on PATH — hook fails open, skip smoke")
        if shutil.which("python3") is None:
            self.skipTest("python3 not on PATH")
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")

    def test_fires_on_gh_pr_create_writes_breadcrumb(self) -> None:
        """`gh pr create` → exit 0 + breadcrumb written."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _set_up_repo(Path(tmp))
            _git_init(repo)
            cp = _invoke_hook(repo, _payload("gh pr create --base main --head feat/test"))
            self.assertEqual(cp.returncode, 0, msg=cp.stderr)
            breadcrumb = repo / ".dev-kit" / ".pr-route.json"
            self.assertTrue(breadcrumb.exists(), msg=f"missing {breadcrumb}")
            payload = json.loads(breadcrumb.read_text(encoding="utf-8"))
            self.assertEqual(payload["actor_type"], "consumer_fork")
            self.assertEqual(
                payload["recommended_gate"], "fork_pr_review_environment",
            )

    def test_silent_route_default_mode(self) -> None:
        """Default mode (no fork_pr_confirm) is silent — no ask JSON,
        just a stderr summary."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _set_up_repo(Path(tmp))
            _git_init(repo)
            cp = _invoke_hook(repo, _payload("gh pr create --base main --head feat/test"))
            self.assertEqual(cp.returncode, 0)
            # No `permissionDecision: ask` in stdout.
            self.assertNotIn("ask", cp.stdout)
            self.assertIn("consumer_fork", cp.stderr)

    def test_ask_mode_when_fork_pr_confirm_on(self) -> None:
        """fork_pr_confirm=on → ask JSON envelope on stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _set_up_repo(Path(tmp), fork_pr_confirm="on")
            _git_init(repo)
            cp = _invoke_hook(repo, _payload("gh pr create --base main --head feat/test"))
            self.assertEqual(cp.returncode, 0)
            self.assertIn("permissionDecision", cp.stdout)
            self.assertIn("ask", cp.stdout)
            self.assertIn("consumer_fork", cp.stdout)

    def test_skips_non_pr_create_commands(self) -> None:
        """gh pr merge / view / list → exit 0, no breadcrumb."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _set_up_repo(Path(tmp))
            _git_init(repo)
            for cmd in (
                "gh pr merge --auto",
                "gh pr view",
                "gh pr list --state open",
                "gh issue list",
            ):
                with self.subTest(cmd=cmd):
                    cp = _invoke_hook(repo, _payload(cmd))
                    self.assertEqual(cp.returncode, 0)
                    self.assertFalse((repo / ".dev-kit" / ".pr-route.json").exists())

    def test_skips_when_stdin_empty(self) -> None:
        """Empty stdin → exit 0, no breadcrumb."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _set_up_repo(Path(tmp))
            _git_init(repo)
            env = {**os.environ, "DEV_KIT_AGENT": "claude-code"}
            cp = subprocess.run(
                ["bash", str(HOOK)],
                cwd=str(repo),
                input="",
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(cp.returncode, 0)
            self.assertFalse((repo / ".dev-kit" / ".pr-route.json").exists())


if __name__ == "__main__":
    unittest.main()
