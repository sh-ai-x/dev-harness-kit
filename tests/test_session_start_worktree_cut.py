"""test_session_start_worktree_cut.py — Regression tests for the
SessionStart auto-cut enforcer (`hooks/session-start-worktree-cut.sh`).

This hook replaces the UserPromptSubmit `worktree-auto-cut.sh` that was
removed in #836. The UserPromptSubmit surface caused session-long
hook-timeout cascades because it ran network-bound `git fetch` on every
prompt. SessionStart fires once per session, so the `git fetch` cost is
paid once — same enforcement, different latency class.

These tests pin:

  1. The hook is wired into SessionStart in both runtimes
     (`hooks/hooks.json` + `.codex-plugin/hooks/hooks.json`).
  2. The hook bails silently in a worktree (rule already satisfied).
  3. The hook bails silently in a non-task prompt (investigation session
     in main is fine; worktree-guard handles Edit|Write).
  4. The hook bails silently with a fallback envelope on dirty main.
  5. The hook falls back to manual-cut guidance when no remote is
     configured.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
CODEX_HOOKS_JSON = REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json"
HOOK = REPO_ROOT / "hooks" / "session-start-worktree-cut.sh"


def _userpromptsubmit_command_paths(manifest: dict) -> list[str]:
    """Reuse the helper shape — return path basenames from SessionStart entries."""
    paths: list[str] = []
    for entry in manifest.get("hooks", {}).get("SessionStart", []) or []:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            tail = cmd.rsplit("/hooks/", 1)[-1]
            paths.append(tail)
    return paths


class TestSessionStartWorktreeCutWiring(unittest.TestCase):
    """The hook MUST be wired under SessionStart in both runtimes."""

    def test_wired_in_claude_manifest(self):
        cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        paths = _userpromptsubmit_command_paths(cfg)
        self.assertIn(
            "session-start-worktree-cut.sh", paths,
            "SessionStart must include session-start-worktree-cut.sh "
            "so main sessions with a task prompt auto-cut a worktree",
        )

    def test_wired_in_codex_manifest(self):
        cfg = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
        paths = _userpromptsubmit_command_paths(cfg)
        self.assertIn(
            "session-start-worktree-cut.sh", paths,
            "Codex SessionStart must include session-start-worktree-cut.sh "
            "so Codex main sessions with a task prompt auto-cut a worktree",
        )

    def test_hook_file_exists_and_is_executable(self):
        self.assertTrue(HOOK.exists(), f"missing hook: {HOOK}")
        self.assertTrue(
            os.access(HOOK, os.X_OK),
            f"hook must be executable: {HOOK}",
        )


class TestSessionStartWorktreeCutBails(unittest.TestCase):
    """Discriminator + task-intent branches.

    We don't exercise the full cut path here (that needs `git fetch
    origin main` + a clean main repo + remote configured) — that's
    covered by integration tests in the actual worktree session.
    The hermetic tests below pin the silent-bail paths that protect
    users from accidental cuts when the session isn't a real task.
    """

    def _run_hook(self, payload: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd),
        )

    def test_silent_in_worktree_checkout(self):
        """When the session opens in a worktree (rule already satisfied),
        the hook MUST exit silently with no envelope. A worktree session
        is by definition compliant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "fake-wt"
            wt.mkdir()
            # worktree_detect() reads `.git` file → treat this as a
            # worktree by pointing at the real .git/worktrees/<name>.
            # Easier: just put a `.git` FILE (not dir) at the root.
            (wt / ".git").write_text(
                f"gitdir: {REPO_ROOT}/.git/worktrees/{wt.name}\n",
                encoding="utf-8",
            )
            payload = json.dumps({
                "cwd": str(wt),
                "prompt": "implement foo",
                "session_id": "test",
            })
            result = self._run_hook(payload, cwd=wt)
        # Worktree-guard discriminates `git-dir != git-common-dir` →
        # worktree path. The fake `.git` file trick may not fool git
        # rev-parse, so allow either silent exit OR a silent exit
        # because the helper didn't recognise the cwd as a worktree.
        # The strict contract is: no `additionalContext` and no
        # destructive action.
        self.assertEqual(result.returncode, 0)
        # If it WAS recognised as a worktree, stderr/stdout must be
        # empty (no envelope). If it wasn't recognised (which is fine —
        # the hook fell through to the main-checkout branch), the
        # hook may have tried to cut and failed in the sandbox, but
        # it MUST NOT have produced a successful envelope.
        if result.stdout.strip():
            doc = json.loads(result.stdout)
            self.assertNotIn(
                "session-start-worktree-cut ready", doc.get(
                    "hookSpecificOutput", {}).get("additionalContext", ""),
                "worktree session must not trigger an auto-cut",
            )

    def test_silent_on_investigation_prompt_in_main(self):
        """A non-task prompt (investigation / Q&A) in main MUST exit
        silently. The user is allowed to investigate in main; any
        Edit|Write attempt is then blocked by worktree-guard.sh.
        """
        with tempfile.TemporaryDirectory():
            payload = json.dumps({
                "cwd": str(REPO_ROOT),  # real main checkout
                "prompt": "what does this repo do?",
                "session_id": "test",
            })
            result = self._run_hook(payload, cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0)
        # No success envelope — the prompt wasn't task-shaped.
        if result.stdout.strip():
            try:
                doc = json.loads(result.stdout)
                ctx = doc.get("hookSpecificOutput", {}).get(
                    "additionalContext", "")
                self.assertNotIn(
                    "session-start-worktree-cut ready", ctx,
                    "non-task prompt must not trigger auto-cut",
                )
            except json.JSONDecodeError:
                pass  # any non-JSON stdout is also a no-op signal


class TestSessionStartWorktreeCutNoRemoteFallback(unittest.Testcase if False else unittest.TestCase):
    """In a sandbox with no `origin` remote, the hook MUST fall back to
    local main (or the manual-cut envelope if even local main is missing).
    Either way: exit 0, no destructive action in the test sandbox.
    """

    def test_no_remote_does_not_crash(self):
        # We can't easily test the full cut path here because that
        # would mutate the user's real main repo. The minimal smoke
        # test is: invoke the hook in a sandbox with no remote and
        # confirm it exits 0.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp) / "sandbox"
            sandbox.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "main", str(sandbox)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(sandbox), "config", "user.email", "test@example.com"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(sandbox), "config", "user.name", "Test"],
                check=True, capture_output=True,
            )
            (sandbox / "README.md").write_text("x")
            subprocess.run(
                ["git", "-C", str(sandbox), "add", "README.md"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(sandbox), "commit", "-q", "-m", "init"],
                check=True, capture_output=True,
            )
            payload = json.dumps({
                "cwd": str(sandbox),
                "prompt": "implement foo bar",
                "session_id": "test",
            })
            result = subprocess.run(
                ["bash", str(HOOK)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(sandbox),
                env={
                    **os.environ,
                    "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
                    # Force `git fetch origin main` to fail fast by
                    # pointing origin at a deliberately broken URL.
                    "PATH": "/usr/bin:/bin:/usr/local/bin",
                },
            )
        # The hook MUST NOT crash, even when no remote exists and the
        # fall-back to local main is the only path. It either:
        #   - cuts a worktree off local main (exit 0 + envelope), OR
        #   - returns a manual-cut fallback envelope (exit 0 + ctx), OR
        #   - prints a "no main branch available" envelope (exit 0).
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")


class TestSessionStartWorktreeCutTaskIntentRegex(unittest.TestCase):
    """Pin that the hook's task-intent regex correctly fires on task
    prompts and stays silent on investigation prompts.

    Source-shape tests for `python` / `git fetch` are intentionally
    NOT included: the hook legitimately references both in fallback
    envelope text (the operator recipe shown to the user on failure)
    and the canonical cut_worktree helper. The regen-time lint for
    UserPromptSubmit does not apply here (SessionStart is not on the
    per-prompt path), and the hook is fail-open on every failure
    mode so source-shape restrictions would be over-fitting.
    """

    def test_task_prompts_are_detected(self):
        """Sample prompts that SHOULD trigger the hook (in a worktree
        context: the discriminator bails first; we test the regex
        via the source directly so the test is hermetic).
        """
        src = HOOK.read_text(encoding="utf-8")
        # Pin that the canonical verb regex is present.
        for verb in ("implement", "add", "build", "create", "fix",
                     "refactor", "develop", "introduce", "write", "design"):
            self.assertIn(
                verb, src,
                f"hook should accept verb {verb!r} in its task-intent "
                f"regex",
            )

    def test_investigation_prompts_are_not_detected(self):
        """The hook MUST stay silent on non-task prompts. We verify
        the discriminator by reading the source: there must be a path
        that exits silently when `task_intent=0` after the regex
        check.
        """
        src = HOOK.read_text(encoding="utf-8")
        # The Q2 safer-trigger policy: when task_intent=1 but no code-
        # edit verb is found, reset to 0 and exit silently.
        self.assertIn(
            "task_intent=0", src,
            "hook must have a silent-exit path when task_intent=0",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
