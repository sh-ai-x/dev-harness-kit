#!/usr/bin/env python3
"""test_worktree_auto_cut.py — regression tests for hooks/worktree-auto-cut.sh.

Verifies that the new UserPromptSubmit hook:
  - Stays silent in a worktree (the user is already in a worktree —
    nothing to cut).
  - Stays silent in the main checkout when the prompt is NOT a task
    (investigation, Q&A, false-positive guard).
  - Stays silent in the main checkout when the prompt lacks a code-edit
    verb (Q2 safer-trigger policy: pure investigation doesn't fire).
  - Stays silent in the main checkout when the prompt IS a task but
    main is dirty (precondition failure → fall back to manual nudge).
  - Stays silent outside any git repo.
  - Empty payload: no crash, exit 0.
  - In a clean main + task prompt with code-edit verb: creates a new
    worktree off main, emits additionalContext naming the new path and
    branch, and does NOT modify main.
  - The created branch name matches `<type>/<verb>-<noun>-<hash6>` and
    passes the git-workflow slug rules.
  - hooks.json wires worktree-auto-cut.sh into UserPromptSubmit with
    a timeout large enough for `git fetch` (>= 60s; the wired value is
    120s to cover slow origin or large HEAD).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS = REPO_ROOT / "hooks"


def _run_hook(
    script: str,
    payload: dict,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    full_env.setdefault("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT))
    return subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(cwd) if cwd else None,
        env=full_env,
    )


def _payload(prompt: str = "", cwd: str = "") -> dict:
    p = {"hook_event_name": "UserPromptSubmit", "session_id": "test"}
    if prompt:
        p["prompt"] = prompt
    if cwd:
        p["cwd"] = cwd
    return p


def _init_clean_main() -> tempfile.TemporaryDirectory:
    """Build a throwaway main repo with a single commit on `main` and
    no remote. Mirrors the fixture shape used in
    test_log_on_session_start.py — the auto-cut hook falls back to
    local `main` when no `origin` remote is configured.
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "README.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    return td


class TestWorktreeAutoCutSilentInWorktree(unittest.TestCase):
    """In an existing worktree: hook must stay silent."""

    def setUp(self):
        if not (HOOKS / "worktree-auto-cut.sh").exists():
            self.skipTest("worktree-auto-cut.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")

    def test_silent_in_worktree(self):
        # Build a main + linked worktree (reuses the test_log_on_session_start
        # helper, duplicated here to keep the test file self-contained).
        main_tmp = tempfile.TemporaryDirectory()
        main_root = Path(main_tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(main_root)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(main_root), "config", "user.email", "test@example.com"],
                       check=True)
        subprocess.run(["git", "-C", str(main_root), "config", "user.name", "Test"],
                       check=True)
        (main_root / "README.md").write_text("x")
        subprocess.run(["git", "-C", str(main_root), "add", "README.md"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(main_root), "commit", "-q", "-m", "init"],
                       check=True, capture_output=True)

        wt_parent = tempfile.TemporaryDirectory()
        wt_path = Path(wt_parent.name) / "wt"
        subprocess.run(
            ["git", "-C", str(main_root), "worktree", "add", "-b", "feat/scaffold", str(wt_path)],
            check=True, capture_output=True,
        )

        try:
            r = _run_hook(
                "worktree-auto-cut.sh",
                _payload(prompt="add file foo", cwd=str(wt_path)),
                cwd=wt_path,
            )
            self.assertEqual(r.returncode, 0,
                             f"rc={r.returncode}, stderr={r.stderr}, stdout={r.stdout!r}")
            self.assertEqual(r.stdout.strip(), "",
                             f"expected silent stdout; got: {r.stdout!r}")
        finally:
            wt_parent.cleanup()
            main_tmp.cleanup()


class TestWorktreeAutoCutSilentForNonTaskPrompts(unittest.TestCase):
    """In main checkout: non-task prompts must NOT fire the cut."""

    def setUp(self):
        if not (HOOKS / "worktree-auto-cut.sh").exists():
            self.skipTest("worktree-auto-cut.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")
        self._main = _init_clean_main()

    def tearDown(self):
        self._main.cleanup()

    def test_silent_for_investigation_prompt(self):
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="what does this error mean?", cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "",
                         f"investigation prompt should not fire; got: {r.stdout!r}")

    def test_silent_for_qa_prompt(self):
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="explain how classify_worktree_dir works",
                     cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_uncertain_for_task_prompt_without_code_edit_verb(self):
        # "fix the bug" carries a task verb but no code-edit noun.
        # Under the intent-first contract (issue #845) this surfaces
        # a structured next-question — NOT a silent pass — so the
        # user knows the classifier saw the request but did not
        # promote it to implementation.
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="fix the bug", cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(r.returncode, 0)
        # stdout must carry the next-question envelope.
        doc = json.loads(r.stdout)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("uncertain", ctx.lower())
        self.assertIn("clarifying question", ctx.lower())


class TestWorktreeAutoCutDirtyMain(unittest.TestCase):
    """In main checkout + DIRTY main + ACCEPTED intent.md: hook must
    surface the dirty-main message and NOT cut. Without the
    accepted intent the hook must surface the capture-prompt
    instead — the dirty-main check is gated on accepted intent per
    the issue #845 brief.

    The "dirty" state we exercise is a tracked file that has been
    modified (e.g. README.md after the initial commit). Untracked
    files are intentionally ignored by the hook — the proto-spec
    ``.dev-kit/round-0/intent.md`` is untracked until the
    orchestrator commits it, and we don't want the cut blocked by
    its mere presence.
    """

    def setUp(self):
        if not (HOOKS / "worktree-auto-cut.sh").exists():
            self.skipTest("worktree-auto-cut.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")
        self._main = _init_clean_main()
        # Make main dirty by modifying the tracked README.md —
        # untracked files alone should not block the cut (see the
        # class docstring).
        (Path(self._main.name) / "README.md").write_text("modified")

    def tearDown(self):
        self._main.cleanup()

    def test_without_intent_prompts_capture(self):
        """Implementation prompt + dirty main + no accepted intent
        → hook surfaces the capture-prompt envelope (it must not
        reach the dirty-main check)."""
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="add file foo", cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(r.returncode, 0)
        doc = json.loads(r.stdout)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("no intent record", ctx)
        self.assertFalse((Path(self._main.name) / ".worktrees").exists())

    def test_with_accepted_intent_explains_dirty(self):
        """Implementation prompt + dirty main + accepted intent.md
        → hook surfaces the dirty-main message. The dirty check
        lives behind the intent gate."""
        root = Path(self._main.name)
        intent_dir = root / ".dev-kit" / "round-0"
        intent_dir.mkdir(parents=True, exist_ok=True)
        (intent_dir / "intent.md").write_text(
            "# Intent: add file foo\n"
            "\n"
            "request_id: req-deadbeefcafef00d\n"
            "client: claude-code\n"
            "decision_mode: accepted\n"
            "goal: add file foo\n"
            "\n"
            "## Acceptance criteria\n"
            "- foo file added\n",
            encoding="utf-8",
        )
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="add file foo", cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(r.returncode, 0)
        doc = json.loads(r.stdout)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("main checkout is dirty", ctx)
        self.assertIn("stash or commit", ctx)
        self.assertFalse((Path(self._main.name) / ".worktrees").exists())


class TestWorktreeAutoCutFires(unittest.TestCase):
    """In clean main + ACCEPTED intent.md + task prompt with
    code-edit verb: hook must auto-cut a worktree, bootstrap
    log-on, and return additionalContext.

    Per the issue #845 brief, the cut is gated on the accepted
    Intent record at .dev-kit/round-0/intent.md. Without that
    record the hook surfaces a capture-prompt envelope instead
    (see TestWorktreeAutoCutCapturePath below).
    """

    def setUp(self):
        if not (HOOKS / "worktree-auto-cut.sh").exists():
            self.skipTest("worktree-auto-cut.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")
        self._main = _init_clean_main()
        # Materialize an accepted intent.md so the cut is unblocked.
        root = Path(self._main.name)
        intent_dir = root / ".dev-kit" / "round-0"
        intent_dir.mkdir(parents=True, exist_ok=True)
        (intent_dir / "intent.md").write_text(
            "# Intent: add file foo\n"
            "\n"
            "request_id: req-deadbeefcafef00d\n"
            "client: claude-code\n"
            "decision_mode: accepted\n"
            "goal: add file foo\n"
            "\n"
            "## Acceptance criteria\n"
            "- foo file added\n",
            encoding="utf-8",
        )

    def tearDown(self):
        # Clean up any worktree the hook may have created.
        root = Path(self._main.name)
        wt_root = root / ".worktrees"
        if wt_root.exists():
            for child in wt_root.iterdir():
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "remove", "--force", str(child)],
                    capture_output=True,
                )
        # Remove any auto-cut branches.
        for ref in subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "--format=%(refname)",
             "refs/heads/fix/"],
            capture_output=True, text=True,
        ).stdout.splitlines():
            subprocess.run(["git", "-C", str(root), "branch", "-D", ref.removeprefix("refs/heads/")],
                           capture_output=True)
        self._main.cleanup()

    def test_cuts_worktree_and_returns_additional_context(self):
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="add file foo to the project", cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(
            r.returncode, 0,
            f"rc={r.returncode}, stderr={r.stderr}, stdout={r.stdout!r}",
        )
        # Output must be valid JSON with additionalContext.
        doc = json.loads(r.stdout)
        ctx = doc.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("worktree cut ready", ctx,
                      f"expected cut-ready marker; got: {ctx!r}")
        self.assertIn("Claude Code next: open a new session", ctx)
        self.assertIn("Codex next: spawn a subagent", ctx)
        # Slug must match the project branch-naming regex. The
        # new shape prefers the classifier's <type>/<slug>
        # suggestion over the old hash-suffix shape; both pass the
        # same regex.
        m = re.search(r"branch:\s+(\S+)", ctx)
        self.assertIsNotNone(m, f"no branch line in context: {ctx!r}")
        branch = m.group(1)
        self.assertRegex(
            branch,
            r"^fix/[a-z0-9-]{2,40}$",
            f"branch '{branch}' does not match slug policy",
        )
        # Worktree dir must exist on disk.
        wt_path = re.search(r"path:\s+(\S+)", ctx).group(1)
        self.assertTrue(
            Path(wt_path).exists(),
            f"worktree path does not exist: {wt_path}",
        )
        self.assertIn(
            f"{os.sep}.worktrees{os.sep}",
            wt_path,
            f"new worktree must use the client-neutral .worktrees root: {wt_path}",
        )
        # Worktree must be a real git worktree (HEAD on a branch).
        branch_ref = subprocess.run(
            ["git", "-C", wt_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(branch_ref, branch,
                         f"worktree on wrong branch: {branch_ref} vs {branch}")
        # Main must still be on `main` (the cut must not have moved it).
        main_ref = subprocess.run(
            ["git", "-C", self._main.name, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(main_ref, "main",
                         f"main moved unexpectedly: {main_ref}")

    def test_cuts_worktree_for_korean_task_prompt(self):
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="hook 오류를 수정해", cwd=self._main.name),
            cwd=Path(self._main.name),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("worktree cut ready", ctx)
        branch = re.search(r"branch:\s+(\S+)", ctx).group(1)
        self.assertRegex(branch, r"^fix/[a-z0-9-]{2,40}$")

    def test_cuts_worktree_at_repo_root_when_session_starts_in_subdirectory(self):
        """The hook's cwd is not necessarily the repository root."""
        subdir = Path(self._main.name) / "src"
        subdir.mkdir()
        r = _run_hook(
            "worktree-auto-cut.sh",
            _payload(prompt="add file foo to the project", cwd=str(subdir)),
            cwd=subdir,
        )
        self.assertEqual(
            r.returncode, 0,
            f"rc={r.returncode}, stderr={r.stderr}, stdout={r.stdout!r}",
        )
        doc = json.loads(r.stdout)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        wt_path = Path(re.search(r"path:\s+(\S+)", ctx).group(1))
        self.assertEqual(
            wt_path.parent.resolve(),
            (Path(self._main.name) / ".worktrees").resolve(),
        )
        self.assertTrue(wt_path.is_dir())


class TestWorktreeAutoCutOutsideGit(unittest.TestCase):
    """Outside any git repo: hook stays silent."""

    def setUp(self):
        if not (HOOKS / "worktree-auto-cut.sh").exists():
            self.skipTest("worktree-auto-cut.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")

    def test_silent_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook(
                "worktree-auto-cut.sh",
                _payload(prompt="add file foo", cwd=tmp),
                cwd=Path(tmp),
            )
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")


class TestWorktreeAutoCutEmptyPayload(unittest.TestCase):
    """No prompt or cwd in payload: hook must not crash, exit 0."""

    def setUp(self):
        if not (HOOKS / "worktree-auto-cut.sh").exists():
            self.skipTest("worktree-auto-cut.sh not found")

    def test_empty_payload(self):
        r = _run_hook("worktree-auto-cut.sh", _payload())
        self.assertEqual(r.returncode, 0)


class TestWorktreeAutoCutWiring(unittest.TestCase):
    """hooks.json must register worktree-auto-cut.sh under UserPromptSubmit."""

    def setUp(self):
        path = HOOKS / "hooks.json"
        if not path.exists():
            self.skipTest(f"hooks.json not found at {path}")
        self._cfg = json.loads(path.read_text(encoding="utf-8"))

    def _hooks_under(self, event: str) -> list:
        flat = []
        for entry in self._cfg["hooks"].get(event, []):
            for h in entry.get("hooks", []):
                flat.append(h)
        return flat

    def test_worktree_auto_cut_wired_into_userpromptsubmit(self):
        hooks = self._hooks_under("UserPromptSubmit")
        match = [h for h in hooks if "worktree-auto-cut.sh" in h.get("command", "")]
        self.assertTrue(
            match,
            f"worktree-auto-cut.sh not wired into UserPromptSubmit. Got: {hooks}",
        )
        for h in match:
            # The hook runs `git fetch origin main` + `git worktree add`,
            # both of which regularly exceed the 30s default on slow
            # networks or large HEADs. An explicit timeout >= 60 is
            # required so the UserPromptSubmit hook doesn't lose its
            # advisory output to "timeout after 30s — output discarded".
            timeout = h.get("timeout", 30)
            self.assertGreaterEqual(
                timeout, 60,
                f"worktree-auto-cut.sh timeout must be >= 60s (got {timeout}); "
                f"30s default loses the advisory on slow networks",
            )

    def test_worktree_auto_cut_wired(self):
        """Regression: the auto-cut hook must remain in UserPromptSubmit."""
        hooks = self._hooks_under("UserPromptSubmit")
        self.assertTrue(
            any("worktree-auto-cut.sh" in h.get("command", "") for h in hooks),
            f"worktree-auto-cut.sh missing from UserPromptSubmit: {hooks}",
        )


class TestWorktreeAutoCutHasNoGenerationCap(unittest.TestCase):
    """Auto-cut must keep creating fresh worktrees; cleanup is separate."""

    def test_no_active_limit_or_suffix_cap(self):
        source = (HOOKS / "worktree-auto-cut.sh").read_text(encoding="utf-8")
        self.assertNotIn("DEV_KIT_AUTO_CUT_MAX", source)
        self.assertNotIn("count_active_auto_cut_worktrees", source)
        self.assertNotIn('"$n" -gt 99', source)


if __name__ == "__main__":
    unittest.main()
