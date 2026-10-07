"""Regression tests for explicit task-worktree cleanup at session end.

The cleanup hook is advisory because SessionEnd cannot collect an interactive
answer. It offers a choice at the last Stop boundary; the command below is
the only path that can remove a worktree, and only after an explicit
``--decision remove``.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLEANUP = ROOT / "bin" / "worktree-session-cleanup.sh"
HOOK = ROOT / "hooks" / "worktree-session-cleanup.sh"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _make_repo() -> tuple[tempfile.TemporaryDirectory, Path, Path]:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".worktrees/\nlogs/\n.dev-kit/\n")
    (root / "README.md").write_text("x\n")
    _git(root, "add", ".gitignore", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    wt = root / ".worktrees" / "task"
    _git(root, "worktree", "add", "-q", "-b", "fix/task", str(wt), "main")
    return td, root, wt


def _run_cleanup(wt: Path, decision: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("AGENT_LOG_ROOT", None)
    return subprocess.run(
        [str(CLEANUP), "--worktree", str(wt), "--decision", decision],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestWorktreeSessionCleanup(unittest.TestCase):
    def test_ask_is_non_destructive_and_explains_both_choices(self) -> None:
        td, root, wt = _make_repo()
        try:
            (wt / "logs").mkdir()
            (wt / "logs" / "session.json").write_text("{}\n")
            result = _run_cleanup(wt, "ask")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[choice-required]", result.stdout)
            self.assertIn("--decision keep", result.stdout)
            self.assertIn("--decision remove", result.stdout)
            self.assertTrue(wt.exists())
            self.assertTrue((wt / "logs" / "session.json").exists())
        finally:
            td.cleanup()

    def test_keep_is_explicitly_non_destructive(self) -> None:
        td, root, wt = _make_repo()
        try:
            result = _run_cleanup(wt, "keep")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[kept]", result.stdout)
            self.assertTrue(wt.exists())
            self.assertEqual(_git(root, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        finally:
            td.cleanup()

    def test_remove_rejects_dirty_worktree_without_archiving_or_deleting(self) -> None:
        td, root, wt = _make_repo()
        try:
            (wt / "README.md").write_text("dirty\n")
            result = _run_cleanup(wt, "remove")
            self.assertEqual(result.returncode, 2)
            self.assertIn("worktree is dirty", result.stderr)
            self.assertTrue(wt.exists())
            self.assertFalse((root / "logs" / ".archive").exists())
        finally:
            td.cleanup()

    def test_remove_archives_logs_then_removes_worktree_and_keeps_branch(self) -> None:
        td, root, wt = _make_repo()
        try:
            (wt / "logs").mkdir()
            (wt / "logs" / "session.json").write_text('{"ok":true}\n')
            result = _run_cleanup(wt, "remove")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[removed]", result.stdout)
            self.assertFalse(wt.exists())
            archive_files = list((root / "logs" / ".archive").glob("fix-task/*/session.json"))
            self.assertEqual(len(archive_files), 1)
            self.assertEqual(archive_files[0].read_text(), '{"ok":true}\n')
            self.assertEqual(
                _git(root, "show-ref", "--verify", "--quiet", "refs/heads/fix/task", check=False).returncode,
                0,
            )
        finally:
            td.cleanup()

    def test_remove_respects_babysit_retention(self) -> None:
        td, root, wt = _make_repo()
        try:
            marker = wt / ".dev-kit"
            marker.mkdir()
            (marker / "babysit-retention.json").write_text('{"owner":"babysit-pr"}\n')
            result = _run_cleanup(wt, "remove")
            self.assertEqual(result.returncode, 2)
            self.assertIn("retained by babysit-pr", result.stderr)
            self.assertTrue(wt.exists())
        finally:
            td.cleanup()


class TestWorktreeSessionCleanupHook(unittest.TestCase):
    def test_completion_stop_emits_choice_without_removing(self) -> None:
        td, root, wt = _make_repo()
        try:
            payload = {
                "hook_event_name": "Stop",
                "session_id": "cleanup-test",
                "cwd": str(wt),
                "last_assistant_message": "PR opened and tests passed.",
            }
            env = os.environ.copy()
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
            result = subprocess.run(
                ["bash", str(HOOK)], input=json.dumps(payload), cwd=str(ROOT),
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            context = document["hookSpecificOutput"]["additionalContext"]
            self.assertIn("KEEP or REMOVE", context)
            self.assertIn(str(wt), context)
            self.assertIn("--decision remove", context)
            self.assertTrue(wt.exists())
        finally:
            td.cleanup()

    def test_non_completion_stop_is_silent(self) -> None:
        td, root, wt = _make_repo()
        try:
            payload = {
                "hook_event_name": "Stop",
                "cwd": str(wt),
                "last_assistant_message": "I am still investigating the failure.",
            }
            env = os.environ.copy()
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
            result = subprocess.run(
                ["bash", str(HOOK)], input=json.dumps(payload), cwd=str(ROOT),
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
        finally:
            td.cleanup()

    def test_stop_prompt_surfaces_unpushed_commits_issue_959(self) -> None:
        """When the branch has commits the user has not pushed, the Stop-hook
        prompt must mention the unpushed count so the user makes an informed
        KEEP/REMOVE choice. Regression coverage for #959."""
        td, root, wt = _make_repo()
        try:
            # Two local commits ahead of `main` (the test fixture has no
            # remote, so the fallback comparison is against local main).
            (wt / "a.txt").write_text("a\n")
            _git(wt, "add", "a.txt")
            _git(wt, "commit", "-q", "-m", "first unpushed")
            (wt / "b.txt").write_text("b\n")
            _git(wt, "add", "b.txt")
            _git(wt, "commit", "-q", "-m", "second unpushed")
            self.assertEqual(
                _git(wt, "rev-list", "--count", "main..HEAD").stdout.strip(),
                "2",
            )

            payload = {
                "hook_event_name": "Stop",
                "cwd": str(wt),
                "last_assistant_message": "Tests passed.",
            }
            env = os.environ.copy()
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
            result = subprocess.run(
                ["bash", str(HOOK)], input=json.dumps(payload), cwd=str(ROOT),
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            context = document["hookSpecificOutput"]["additionalContext"]
            self.assertIn("KEEP or REMOVE", context)
            self.assertIn("Unpushed: 2 commit(s) ahead of main", context)
            self.assertIn("push first if you want them on a remote", context)
            # Worktree must still exist; the hook is advisory only.
            self.assertTrue(wt.exists())
        finally:
            td.cleanup()

    def test_stop_prompt_omits_unpushed_line_when_branch_is_synced_issue_959(self) -> None:
        """When the branch has no unpushed commits, the Stop-hook prompt must
        not mention unpushed state — keeps the prompt tight. Regression
        coverage for #959."""
        td, root, wt = _make_repo()
        try:
            # The fixture creates the worktree branched from main with no
            # commits ahead — exactly the "fully synced" case.
            self.assertEqual(
                _git(wt, "rev-list", "--count", "main..HEAD").stdout.strip(),
                "0",
            )
            payload = {
                "hook_event_name": "Stop",
                "cwd": str(wt),
                "last_assistant_message": "Done.",
            }
            env = os.environ.copy()
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
            result = subprocess.run(
                ["bash", str(HOOK)], input=json.dumps(payload), cwd=str(ROOT),
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            context = document["hookSpecificOutput"]["additionalContext"]
            self.assertIn("KEEP or REMOVE", context)
            self.assertNotIn("Unpushed:", context)
            self.assertTrue(wt.exists())
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
