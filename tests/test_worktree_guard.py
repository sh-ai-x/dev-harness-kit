#!/usr/bin/env python3
"""test_worktree_guard.py — regression tests for the 2 worktree-rule hooks.

Verifies the bash-level behavior of:
  - hooks/worktree-guard.sh       (PreToolUse Edit|Write|MultiEdit — confirmation prompt on main, hard deny on jq-missing)
  - hooks/session-start-check.sh  (SessionStart — advisory additionalContext)

The hard rule under test (.claude/rules/git-workflow.md):
  "Every task = new worktree + client handoff + new branch."

We test the scripts as black boxes by feeding them JSON via stdin and
asserting on exit code + stdout/stderr. No mocks. We synthesize real
git repos (main + linked worktree) via `git worktree add` to exercise
the --git-dir / --git-common-dir discriminator.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS = REPO_ROOT / "hooks"


def _run_hook(script: str, payload: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
    )


def _edit_payload(file_path: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path}}


def _prompt_payload(prompt: str, cwd: str = "") -> dict:
    p = {"tool_name": "UserPromptSubmit", "prompt": prompt}
    if cwd:
        p["cwd"] = cwd
    return p


def _session_payload(cwd: str = "") -> dict:
    p = {"hook_event_name": "SessionStart", "session_id": "test"}
    if cwd:
        p["cwd"] = cwd
    return p


def _init_main_with_worktree() -> tuple:
    """Build a throwaway repo with a linked worktree. Returns (main_tmp, wt_tmp).

    main_tmp: tempdir that IS the main checkout (git_dir == git_common_dir).
    wt_tmp:   tempdir that IS a worktree (git_dir != git_common_dir).
    """
    main_tmp = tempfile.TemporaryDirectory()
    main_root = Path(main_tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(main_root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main_root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(main_root), "config", "user.name", "Test"], check=True)
    (main_root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(main_root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main_root), "commit", "-q", "-m", "init"], check=True, capture_output=True)

    wt_parent = tempfile.TemporaryDirectory()
    wt_path = Path(wt_parent.name) / "wt"
    subprocess.run(
        ["git", "-C", str(main_root), "worktree", "add", "-b", "fix/test", str(wt_path)],
        check=True, capture_output=True,
    )
    return main_tmp, wt_parent, wt_path


def _init_orch_worktree() -> tuple:
    """Build a throwaway repo with one .worktrees/orch-test worktree on
    branch orch/test. Returns (main_tmp, orch_path). The orch worktree
    is a real git worktree on an orchestration branch so the hook's
    file_path-extracted branch detection (B) actually fires.
    """
    main_tmp = tempfile.TemporaryDirectory()
    main_root = Path(main_tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(main_root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main_root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(main_root), "config", "user.name", "Test"], check=True)
    (main_root / "lib").mkdir()
    (main_root / "lib" / "placeholder.py").write_text("# placeholder")
    (main_root / "README.md").write_text("init")
    subprocess.run(["git", "-C", str(main_root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main_root), "commit", "-q", "-m", "init"], check=True, capture_output=True)
    orch_path = main_root / ".worktrees" / "orch-test"
    subprocess.run(
        ["git", "-C", str(main_root), "worktree", "add", "-b", "orch/test", str(orch_path)],
        check=True, capture_output=True,
    )
    return main_tmp, orch_path


class TestWorktreeGuardAsks(unittest.TestCase):
    """worktree-guard.sh must ASK (exit 0 + permissionDecision:'ask' JSON)
    on Edit/Write/MultiEdit in the main checkout for any non-safelist path.
    Hard deny was relaxed to a confirmation prompt in chore/wg-ask-mode; the
    user can override with an explicit reason captured in the transcript.
    """

    def setUp(self):
        if not (HOOKS / "worktree-guard.sh").exists():
            self.skipTest("worktree-guard.sh not found")

    def test_asks_edit_in_main_checkout(self):
        main_tmp, _, _ = _init_main_with_worktree()
        try:
            r = _run_hook("worktree-guard.sh", _edit_payload("/some/file.py"), cwd=Path(main_tmp.name))
            self.assertEqual(r.returncode, 0, f"expected ask (exit 0), got rc={r.returncode}, stderr={r.stderr}")
            combined = r.stdout + r.stderr
            self.assertIn("WORKTREE GUARD", combined)
            self.assertIn("permissionDecision", combined)
            self.assertIn('"ask"', combined)
            self.assertIn("main checkout", combined)
        finally:
            main_tmp.cleanup()

    def test_ask_output_is_valid_pretooluse_json(self):
        """Minor 4 (ask mode): ask output must match the PreToolUse JSON
        schema that Claude Code parses (hookSpecificOutput.permissionDecision).
        The permissionDecision must be 'ask' (not 'deny') so the user gets a
        confirmation prompt instead of a hard refusal."""
        main_tmp, _, _ = _init_main_with_worktree()
        try:
            r = _run_hook("worktree-guard.sh", _edit_payload("/some/file.py"), cwd=Path(main_tmp.name))
            self.assertEqual(r.returncode, 0)
            # The ask JSON is printed to stderr; find it.
            ask_lines = [ln for ln in (r.stdout + r.stderr).splitlines()
                          if ln.strip().startswith("{")]
            self.assertTrue(ask_lines, f"no JSON line in output: stdout={r.stdout!r} stderr={r.stderr!r}")
            for line in ask_lines:
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as e:
                    self.fail(f"ask output is not valid JSON: {line!r} ({e})")
                self.assertIn("hookSpecificOutput", doc)
                hso = doc["hookSpecificOutput"]
                self.assertEqual(hso.get("hookEventName"), "PreToolUse")
                self.assertEqual(hso.get("permissionDecision"), "ask")
                self.assertIn("permissionDecisionReason", hso)
                self.assertTrue(len(hso["permissionDecisionReason"]) > 0)
        finally:
            main_tmp.cleanup()

    def test_asks_write_in_subdir_of_main_checkout(self):
        """Subdirectory of the main checkout is still main checkout → ask."""
        main_tmp, _, _ = _init_main_with_worktree()
        try:
            sub = Path(main_tmp.name) / "src" / "deep"
            sub.mkdir(parents=True, exist_ok=True)
            r = _run_hook("worktree-guard.sh", _edit_payload(str(sub / "foo.py")), cwd=sub)
            self.assertEqual(r.returncode, 0, f"expected ask (exit 0), got rc={r.returncode}, stderr={r.stderr}")
            self.assertIn("WORKTREE GUARD", r.stdout + r.stderr)
        finally:
            main_tmp.cleanup()


    def test_main_ask_msg_includes_routing_and_safelist(self):
        """Regression for chore/wg-ask-mode: the ask reason must surface the
        worktree-routing hint AND name the safelist (so users see why their
        edit is being asked about and which paths are auto-allowed). jq
        missing -> skip.
        """
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        main_tmp, _, _ = _init_main_with_worktree()
        try:
            r = _run_hook(
                "worktree-guard.sh",
                _edit_payload("/some/file.py"),
                cwd=Path(main_tmp.name),
            )
            self.assertEqual(r.returncode, 0, f"expected ask (exit 0), got rc={r.returncode}, stderr={r.stderr}")
            combined = r.stdout + r.stderr
            ask_lines = [ln for ln in combined.splitlines()
                          if ln.strip().startswith("{")]
            self.assertTrue(
                ask_lines,
                f"no JSON line in output: stdout={r.stdout!r} stderr={r.stderr!r}",
            )
            reason = ""
            for line in ask_lines:
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rsn = doc.get("hookSpecificOutput", {}).get(
                    "permissionDecisionReason", ""
                )
                if "WORKTREE GUARD" in rsn:
                    reason = rsn
                    break
            self.assertTrue(
                reason,
                f"WORKTREE GUARD ask JSON not found in output: {combined!r}",
            )
            for needle in (
                # ask semantics
                "explicit approval",
                # routing
                "worktree add -b",
                ".worktrees/<slug>",
                "a Claude session",
                # Iron Laws recap (references iron-laws/index.md L1/L3/L4/L5)
                "Iron Laws",
                "iron-laws/index.md",
                "L1",
                # safelist call-out
                ".dev-kit/**",
                "settings.local.json",
                # round-* exception preserved for orchestrator handoff
                ".dev-kit/round-*/**",
            ):
                self.assertIn(
                    needle, reason,
                    f"missing {needle!r} in ask reason: {reason!r}",
                )
        finally:
            main_tmp.cleanup()

    def test_orch_branch_denies_code_path(self):
        """Regression for PR #270 (B): when file_path points inside a
        .worktrees/<name>/... tree AND that worktree's branch is
        orch/*, the hook must DENY protected paths with the ORCH
        ISOLATION reason. .dev-kit/round-*/** paths must be ALLOWED
        (exit 0) so the orchestrator can leave round-N hand-off notes
        even if cwd is the main checkout. jq missing -> skip.
        """
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        main_tmp, orch_path = _init_orch_worktree()
        main_root = Path(main_tmp.name)
        try:
            # Sanity: orch worktree is on orch/test branch
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(orch_path), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip(),
                "orch/test",
            )
            # DENY sub-case: protected path inside orch worktree.
            r = _run_hook(
                "worktree-guard.sh",
                _edit_payload(str(orch_path / "lib" / "foo.py")),
                cwd=main_root,
            )
            self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
            deny_lines = [ln for ln in (r.stdout + r.stderr).splitlines()
                          if ln.strip().startswith("{")]
            self.assertTrue(deny_lines, f"no JSON line in output: stdout={r.stdout!r} stderr={r.stderr!r}")
            reason = ""
            for line in deny_lines:
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rsn = doc.get("hookSpecificOutput", {}).get(
                    "permissionDecisionReason", ""
                )
                if "ORCH ISOLATION" in rsn:
                    reason = rsn
                    break
            self.assertTrue(
                reason,
                f"ORCH ISOLATION deny not found: stdout={r.stdout!r} stderr={r.stderr!r}",
            )
            self.assertIn("orch/*", reason)
            self.assertIn(".dev-kit/round-*/**", reason)
            self.assertIn("feature worktree", reason)
            # ALLOW sub-case: .dev-kit/round-*/** hand-off tmp note.
            r2 = _run_hook(
                "worktree-guard.sh",
                _edit_payload(str(orch_path / ".dev-kit" / "round-foo" / "note.md")),
                cwd=main_root,
            )
            self.assertEqual(
                r2.returncode, 0,
                f"expected allow on .dev-kit/round-*/**, got rc={r2.returncode}, stderr={r2.stderr}",
            )
        finally:
            main_tmp.cleanup()

class TestWorktreeGuardSafelist(unittest.TestCase):
    """worktree-guard.sh must ALLOW (exit 0, no JSON envelope) on main checkout
    when FILE_PATH is in the safelist. The safelist covers bootstrap / hand-off
    / per-user override paths that are NOT real code changes under Iron Law L1.
    """

    def setUp(self):
        if not (HOOKS / "worktree-guard.sh").exists():
            self.skipTest("worktree-guard.sh not found")

    def _assert_allows_on_main(self, file_path: str) -> None:
        main_tmp, _, _ = _init_main_with_worktree()
        try:
            r = _run_hook(
                "worktree-guard.sh",
                _edit_payload(str(Path(main_tmp.name) / file_path)),
                cwd=Path(main_tmp.name),
            )
            self.assertEqual(
                r.returncode, 0,
                f"expected allow (exit 0) on safelist path {file_path!r}, "
                f"got rc={r.returncode}, stderr={r.stderr}",
            )
            combined = r.stdout + r.stderr
            # Safelisted path must NOT emit any permissionDecision envelope —
            # the hook short-circuits with `exit 0` before the ask path.
            self.assertNotIn(
                "permissionDecision", combined,
                f"safelist path {file_path!r} unexpectedly emitted a "
                f"permissionDecision envelope: {combined!r}",
            )
        finally:
            main_tmp.cleanup()

    def test_allows_dev_kit_path(self):
        """`.dev-kit/<anything>` is bootstrap / hand-off → auto-allow."""
        for p in (
            ".dev-kit/round-foo/note.md",
            ".dev-kit/scratch/tmp.txt",
            ".dev-kit/round-bar/sub/deep/note.md",
        ):
            with self.subTest(path=p):
                self._assert_allows_on_main(p)

    def test_allows_claude_settings_local(self):
        """`.claude/settings.local.json` is per-user Claude override → auto-allow."""
        self._assert_allows_on_main(".claude/settings.local.json")

    def test_allows_codex_settings_local(self):
        """`.codex/settings.local.json` is per-user Codex override → auto-allow."""
        self._assert_allows_on_main(".codex/settings.local.json")

    def test_allows_worktrees_gitignore(self):
        """`.worktrees/.gitignore` is worktree bookkeeping → auto-allow."""
        self._assert_allows_on_main(".worktrees/.gitignore")

    def test_asks_non_safelist_path(self):
        """`.dev-kit.bak/...` (different name) is NOT on the safelist → ask."""
        main_tmp, _, _ = _init_main_with_worktree()
        try:
            r = _run_hook(
                "worktree-guard.sh",
                _edit_payload(str(Path(main_tmp.name) / ".dev-kit.bak" / "x.py")),
                cwd=Path(main_tmp.name),
            )
            self.assertEqual(
                r.returncode, 0,
                f"expected ask (exit 0) on non-safelist path, "
                f"got rc={r.returncode}, stderr={r.stderr}",
            )
            self.assertIn('"ask"', r.stdout + r.stderr)
        finally:
            main_tmp.cleanup()


class TestWorktreeGuardAllows(unittest.TestCase):
    """worktree-guard.sh must ALLOW (exit 0) edits inside a worktree."""

    def setUp(self):
        if not (HOOKS / "worktree-guard.sh").exists():
            self.skipTest("worktree-guard.sh not found")

    def test_allows_edit_in_worktree(self):
        _, wt_parent, wt_path = _init_main_with_worktree()
        try:
            r = _run_hook("worktree-guard.sh", _edit_payload(str(wt_path / "foo.py")), cwd=wt_path)
            self.assertEqual(r.returncode, 0, f"expected allow, got rc={r.returncode}, stderr={r.stderr}")
        finally:
            wt_parent.cleanup()

    def test_allows_edit_in_worktree_subdir(self):
        _, wt_parent, wt_path = _init_main_with_worktree()
        try:
            sub = wt_path / "src" / "deep"
            sub.mkdir(parents=True, exist_ok=True)
            r = _run_hook("worktree-guard.sh", _edit_payload(str(sub / "foo.py")), cwd=sub)
            self.assertEqual(r.returncode, 0, f"expected allow, got rc={r.returncode}, stderr={r.stderr}")
        finally:
            wt_parent.cleanup()

    def test_allows_edit_outside_any_git_repo(self):
        """Non-git directory → hook does not apply → exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook("worktree-guard.sh", _edit_payload(str(Path(tmp) / "foo.py")), cwd=Path(tmp))
            self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_no_op_on_missing_payload(self):
        """Empty stdin → hook should not crash, exit 0."""
        r = subprocess.run(
            ["bash", str(HOOKS / "worktree-guard.sh")],
            input="", capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")


class TestWorktreeGuardJqMissing(unittest.TestCase):
    """worktree-guard.sh must FAIL CLOSED when jq is missing."""

    def setUp(self):
        if not (HOOKS / "worktree-guard.sh").exists():
            self.skipTest("worktree-guard.sh not found")
        import shutil as _sh
        self._bash = _sh.which("bash")
        self._jq = _sh.which("jq")
        if not self._bash:
            self.skipTest("bash not on PATH")
        if not self._jq:
            self.skipTest("jq not on host — cannot simulate missing-jq")

    def test_denies_when_jq_missing(self):
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(self._jq))
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps(_edit_payload("/tmp/foo.py"))
        r = subprocess.run(
            [self._bash, str(HOOKS / "worktree-guard.sh")],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)
        self.assertIn("permissionDecision", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
