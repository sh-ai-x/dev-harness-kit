#!/usr/bin/env python3
"""
test_save_log_branch.py — Coverage for the per-branch write side of
tools/save_log.py.

Spawns ``python3 tools/save_log.py --tool claude-code`` against synthetic
fixtures and asserts the JSONL lands in ``logs/claude-code/<branch>/<sid>.jsonl``
under the correct branch bucket. Covers:

- Attached HEAD on a feature branch → ``logs/claude-code/feature-foo/<sid>.jsonl``
- Detached HEAD (commit SHA) → ``logs/claude-code/detached-<sha>/<sid>.jsonl``
- Non-git cwd → ``logs/claude-code/no-git/<sid>.jsonl``
- Branch name with slashes → sanitized to single segment
- ``git`` binary missing (empty PATH) → exit 0 + ``no-git`` fallback
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
TOOL_PY = PROJECT_ROOT / "tools" / "save_log.py"
# Allow `from save_log import ...` in the unit-test case.
sys.path.insert(0, str(TOOL_PY.parent))


def _run_save_log(cwd: Path, *, session_id: str = "sid",
                  transcript: Path, env: dict | None = None,
                  experiment_id: str | None = None) -> subprocess.CompletedProcess:
    """Invoke save_log.py with a minimal payload, returning the CompletedProcess."""
    payload_data = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": str(cwd),
    }
    if experiment_id:
        payload_data["experiment_id"] = experiment_id
    payload = json.dumps(payload_data)
    return subprocess.run(
        [sys.executable, str(TOOL_PY), "--tool", "claude-code"],
        input=payload, capture_output=True, text=True,
        env=env, timeout=15, check=False,
    )


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in ``cwd``; ``check=True`` raises on non-zero."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=check, timeout=10,
    )


class TestSaveLogBranch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="save-log-branch-"))
        self.transcript = self.tmpdir / "transcript.jsonl"
        # Minimal valid JSONL — save_log will keep only conversation lines
        # but its fallback path handles empty/garbage input by copying verbatim.
        self.transcript.write_text("{}\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- attached HEAD ----------------------------------------------------

    def test_attached_head_creates_branch_subdir(self):
        repo = self.tmpdir / "r"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "checkout", "-q", "-b", "feature/foo")
        rc = _run_save_log(repo, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        self.assertTrue(
            (repo / "logs" / "claude-code" / "feature-foo" / "sid.jsonl").exists(),
            f"feature-foo subdir missing under {repo / 'logs' / 'claude-code'}",
        )

    # ---- detached HEAD ----------------------------------------------------

    def test_detached_head_creates_detached_short_sha_subdir(self):
        repo = self.tmpdir / "r"
        repo.mkdir()
        _git(repo, "init", "-q")
        # Need at least one commit so HEAD resolves to a real SHA.
        (repo / "f").write_text("x")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "-m", "init")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()[:7]
        # ``git checkout <sha>`` detaches HEAD (the revision form is what
        # triggers detach; ``git checkout HEAD`` without a rev just stays
        # on the current branch).
        _git(repo, "checkout", "-q", sha)
        rc = _run_save_log(repo, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        d = repo / "logs" / "claude-code"
        matched = [p for p in d.iterdir() if p.name.startswith(f"detached-{sha}")]
        self.assertTrue(
            matched,
            f"no detached-{sha}* subdir under {d}; got {[p.name for p in d.iterdir()]}",
        )

    # ---- non-git cwd ------------------------------------------------------

    def test_non_git_cwd_uses_no_git_subdir(self):
        nonrepo = self.tmpdir / "n"
        nonrepo.mkdir()
        rc = _run_save_log(nonrepo, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        self.assertTrue(
            (nonrepo / "logs" / "claude-code" / "no-git" / "sid.jsonl").exists(),
        )

    # ---- branch name sanitization -----------------------------------------

    def test_branch_with_slashes_sanitized_to_dash(self):
        repo = self.tmpdir / "r"
        repo.mkdir()
        _git(repo, "init", "-q")
        # Modern git (≥2.30) allows slashes in branch names — pick one with
        # a single slash so it round-trips through ``symbolic-ref`` cleanly.
        _git(repo, "checkout", "-q", "-b", "feature/foo")
        rc = _run_save_log(repo, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        d = repo / "logs" / "claude-code"
        matched = [p for p in d.iterdir() if p.name == "feature-foo"]
        self.assertTrue(
            matched,
            f"feature-foo subdir missing under {d}; got {[p.name for p in d.iterdir()]}",
        )

    # ---- git binary missing -----------------------------------------------

    def test_git_missing_falls_back_to_no_git_subdir(self):
        repo = self.tmpdir / "r"
        repo.mkdir()
        # Set up a git repo so cwd is otherwise valid; PATH removal ensures
        # detect_branch() cannot find git.
        _git(repo, "init", "-q")
        _git(repo, "checkout", "-q", "-b", "feature/whatever")
        empty_bin = self.tmpdir / "empty-bin"
        empty_bin.mkdir()
        env = {
            "PATH": str(empty_bin),
            "HOME": str(self.tmpdir),
            "TMPDIR": str(self.tmpdir),
        }
        rc = _run_save_log(repo, transcript=self.transcript, env=env)
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        self.assertTrue(
            (repo / "logs" / "claude-code" / "no-git" / "sid.jsonl").exists(),
            "expected no-git fallback when git is not on PATH",
        )

    # ---- worktree session capture: dual-write for analyzer attribution ----

    def test_worktree_session_dual_writes_main_and_worktree_logs(self):
        # Regression: a session started inside a worktree must capture to
        # both the main checkout's logs/ (so the analyzer finds it under
        # a single canonical location) AND the worktree's own logs/ (so
        # the analyzer's worktree_from_path() can bucket the session
        # under the right worktree name, not (main)).
        main = self.tmpdir / "main"
        main.mkdir()
        _git(main, "init", "-q")
        (main / "f").write_text("x")
        _git(main, "add", ".")
        _git(main, "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "-m", "init")
        wt = main / "wt"
        _git(main, "worktree", "add", "-q", "-b", "fix-x", str(wt))
        rc = _run_save_log(wt, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        # Primary: main checkout (canonical scan location).
        self.assertTrue(
            (main / "logs" / "claude-code" / "fix-x" / "sid.jsonl").exists(),
            "main repo logs/ missing the captured transcript",
        )
        # Secondary: worktree's own logs/ (analyzer attribution).
        self.assertTrue(
            (wt / "logs" / "claude-code" / "fix-x" / "sid.jsonl").exists(),
            f"worktree logs/ missing the dual-write copy: {wt / 'logs' / 'claude-code' / 'fix-x' / 'sid.jsonl'}",
        )

    def test_find_main_repo_root_walks_to_shared_git(self):
        from save_log import find_main_repo_root
        main = self.tmpdir / "r2"
        main.mkdir()
        _git(main, "init", "-q")
        self.assertEqual(
            os.path.realpath(find_main_repo_root(str(main)) or ""),
            os.path.realpath(str(main)),
        )
        wt = main / "wt2"
        _git(main, "worktree", "add", "-q", "-b", "feat-y", str(wt))
        self.assertEqual(
            os.path.realpath(find_main_repo_root(str(wt)) or ""),
            os.path.realpath(str(main)),
        )

    def test_worktree_capture_dual_writes_main_and_worktree(self):
        # Regression: per-worktree cost attribution depends on the JSONL
        # living under <main>/.claude/worktrees/<name>/logs/... — the
        # analyzer's worktree_from_path() reads the worktree name from the
        # file path. With single-write to <main>/logs/<branch>/, the path
        # has no worktree segment and the session is bucketed as (main).
        # save_log.py must dual-write: main logs + worktree logs.
        main = self.tmpdir / "dual"
        main.mkdir()
        _git(main, "init", "-q")
        (main / "f").write_text("init")
        _git(main, "add", ".")
        _git(main, "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "-m", "init")
        wt = main / ".claude" / "worktrees" / "wt-x"
        _git(main, "worktree", "add", "-q", "-b", "fix-x", str(wt))
        rc = _run_save_log(wt, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, f"save_log failed: {rc.stderr}")
        # Main checkout capture (existing behavior preserved).
        self.assertTrue(
            (main / "logs" / "claude-code" / "fix-x" / "sid.jsonl").exists(),
            "main logs/ missing the dual-write copy",
        )
        # NEW: worktree-local copy for analyzer attribution.
        wt_capture = wt / "logs" / "claude-code" / "fix-x" / "sid.jsonl"
        self.assertTrue(wt_capture.exists(),
                        f"worktree logs/ missing the dual-write copy: {wt_capture}")

    def test_main_checkout_capture_does_not_dual_write(self):
        # A session in the main checkout has no worktree to write to —
        # only the main logs/ gets a copy.
        # Force the initial branch to "main" — `git init`'s default
        # (init.defaultBranch) varies by host (macOS: main, ubuntu CI:
        # master), and we want a deterministic test, not a host-flaky one.
        main = self.tmpdir / "mainonly"
        main.mkdir()
        _git(main, "init", "-q", "-b", "main")
        (main / "f").write_text("x")
        _git(main, "add", ".")
        _git(main, "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "-m", "init")
        # Sanity: confirm the actual branch is what we asked for.
        # Without the explicit `-b main` init flag, CI ubuntu defaults
        # to master and the assertion below fails because the JSONL
        # lands under master/, not main/.
        actual_branch = _git(main, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(actual_branch, "main",
                         f"setup failed to force main branch: {actual_branch!r}")
        rc = _run_save_log(main, transcript=self.transcript)
        self.assertEqual(rc.returncode, 0, f"save_log failed: {rc.stderr}")
        self.assertTrue(
            (main / "logs" / "claude-code" / "main" / "sid.jsonl").exists(),
        )

    def test_external_root_survives_worktree_removal_and_redacts_metadata(self):
        main = self.tmpdir / "main-external"
        main.mkdir()
        _git(main, "init", "-q", "-b", "main")
        (main / "f").write_text("x")
        _git(main, "add", ".")
        _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
        wt = main / "wt"
        _git(main, "worktree", "add", "-q", "-b", "fix-x", str(wt))
        transcript = self.tmpdir / "secret-transcript.jsonl"
        transcript.write_text(json.dumps({
            "type": "user",
            "message": {"content": (
                "use OPENAI_API_KEY=myCustomValueThatDoesNotStartWithSk, "
                "ghp_abc123def456ghi789, sk-abc123def456ghi789, "
                "Authorization: Bearer abc123def456ghi789, "
                "Basic YWJjZGVmZ2hpamtsbW5vcA==, "
                "AKIAIOSFODNN7EXAMPLE, xoxb-1234567890ab, "
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature" )},
        }) + "\n")
        external = self.tmpdir / "agent-logs"
        env = os.environ.copy()
        env["AGENT_LOG_ROOT"] = str(external)
        rc = _run_save_log(wt, transcript=transcript, env=env, experiment_id="exp-42")
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        log = external / "main-external" / "claude-code" / "fix-x" / "sid.jsonl"
        meta = external / "main-external" / "claude-code" / "fix-x" / "sid.meta.json"
        self.assertTrue(log.exists())
        self.assertTrue(meta.exists())
        captured = log.read_text()
        for secret in (
            "myCustomValueThatDoesNotStartWithSk",
            "ghp_abc123def456ghi789",
            "sk-abc123def456ghi789",
            "abc123def456ghi789",
            "YWJjZGVmZ2hpamtsbW5vcA==",
            "AKIAIOSFODNN7EXAMPLE",
            "xoxb-1234567890ab",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
        ):
            self.assertNotIn(secret, captured)
        self.assertEqual(json.loads(meta.read_text())["branch"], "fix-x")
        self.assertEqual(json.loads(meta.read_text())["experiment_id"], "exp-42")
        _git(main, "worktree", "remove", "--force", str(wt))
        self.assertTrue(log.exists(), "central telemetry must outlive worktree removal")

    def test_find_worktree_for_cwd_returns_wt_dir(self):
        from save_log import find_worktree_for_cwd
        main = self.tmpdir / "fw"
        main.mkdir()
        _git(main, "init", "-q")
        wt = main / ".claude" / "worktrees" / "wt-y"
        _git(main, "worktree", "add", "-q", "-b", "feat-y", str(wt))
        self.assertEqual(
            os.path.realpath(find_worktree_for_cwd(str(wt), str(main)) or ""),
            os.path.realpath(str(wt)),
        )

    def test_find_worktree_for_cwd_returns_none_for_main(self):
        from save_log import find_worktree_for_cwd
        main = self.tmpdir / "fwmain"
        main.mkdir()
        _git(main, "init", "-q")
        self.assertIsNone(find_worktree_for_cwd(str(main), str(main)))

    def test_find_worktree_for_cwd_returns_none_for_unrelated(self):
        from save_log import find_worktree_for_cwd
        main = self.tmpdir / "fwu_main"
        main.mkdir()
        _git(main, "init", "-q")
        other = self.tmpdir / "fwu_other"
        other.mkdir()
        self.assertIsNone(find_worktree_for_cwd(str(other), str(main)))

    def test_find_main_repo_root_returns_none_for_non_git(self):
        from save_log import find_main_repo_root
        nonrepo = self.tmpdir / "n2"
        nonrepo.mkdir()
        self.assertIsNone(find_main_repo_root(str(nonrepo)))

    # ---- detect_branch unit-level (no subprocess) -------------------------

    def test_detect_branch_unit_sanitize(self):
        """Direct unit coverage for the sanitizer edge cases."""
        from save_log import _sanitize_branch, detect_branch
        self.assertEqual(_sanitize_branch("main"), "main")
        self.assertEqual(_sanitize_branch("feature/foo"), "feature-foo")
        self.assertEqual(_sanitize_branch(""), "detached")
        self.assertEqual(_sanitize_branch("."), "detached")
        self.assertEqual(_sanitize_branch(".."), "detached")
        self.assertEqual(_sanitize_branch("/"), "detached")
        # Long names get truncated to 120 chars.
        long = "a" * 200
        self.assertEqual(len(_sanitize_branch(long)), 120)
        # A branch name that sanitizes to nothing becomes "detached".
        self.assertEqual(_sanitize_branch("///"), "detached")
        # non-git path → "no-git"
        bogus = self.tmpdir / "no_such_repo_xyz"
        self.assertEqual(detect_branch(str(bogus)), "no-git")


class TestSlimClaudeUsageCapture(unittest.TestCase):
    """Regression: slim_transcript must retain tool-call-only assistant turns.

    Those turns carry no conversation text (so _claude_has_text drops them) but
    still hold message.usage (input/output/cache tokens) and tool_use blocks that
    tokens_efficiency_analyzer reads. Dropping them under-counts spend by ~50%.
    Mirrors the codex-side _codex_has_event_tokens fix.
    """

    def _lines(self):
        # (a) user text, (b) assistant text+usage, (c) assistant tool_use-only+usage,
        # (d) user tool_result (no text/usage), (e) assistant isMeta.
        a = {"type": "user", "message": {"role": "user", "content": "hello"}}
        b = {"type": "assistant", "message": {
            "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 100}}}
        c = {"type": "assistant", "message": {
            "role": "assistant", "model": "m",
            "content": [{"type": "tool_use", "name": "Read",
                         "input": {"file_path": "/x"}}],
            "usage": {"input_tokens": 8, "output_tokens": 2,
                      "cache_read_input_tokens": 200}}}
        d = {"type": "user", "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t",
                         "content": "big tool output " * 100}]}}
        e = {"type": "assistant", "isMeta": True, "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "meta"}],
            "usage": {"input_tokens": 1}}}
        return a, b, c, d, e

    def test_tool_only_usage_line_is_retained(self):
        from save_log import slim_transcript
        a, b, c, d, e = self._lines()
        raw = "\n".join(json.dumps(x) for x in (a, b, c, d, e)) + "\n"
        out = slim_transcript(raw, "claude-code")
        self.assertIsNotNone(out)
        kept = [json.loads(ln) for ln in out.splitlines() if ln.strip()]

        # (a) user text, (b) assistant text+usage, (c) assistant tool-only+usage kept.
        self.assertIn(a, kept)
        self.assertIn(b, kept)
        self.assertIn(c, kept, "tool-call-only assistant turn (usage-bearing) was dropped")
        # (d) tool_result and (e) isMeta dropped.
        self.assertNotIn(d, kept)
        self.assertNotIn(e, kept)

        # Token accounting is now complete: both usage-bearing assistant turns survive.
        usage_msgs = [k for k in kept
                      if k.get("type") == "assistant" and (k.get("message") or {}).get("usage")]
        self.assertEqual(len(usage_msgs), 2)
        total_input = sum(int(k["message"]["usage"].get("input_tokens") or 0) for k in usage_msgs)
        total_cache_read = sum(
            int(k["message"]["usage"].get("cache_read_input_tokens") or 0) for k in usage_msgs)
        self.assertEqual(total_input, 18)
        self.assertEqual(total_cache_read, 300)

    def test_claude_has_usage_predicate(self):
        from save_log import _claude_has_text, _claude_has_usage
        a, b, c, d, e = self._lines()
        # tool-only assistant turn: no text but has usage -> retained by the new predicate.
        self.assertFalse(_claude_has_text(c))
        self.assertTrue(_claude_has_usage(c))
        # user text turn: no usage.
        self.assertFalse(_claude_has_usage(a))
        # isMeta assistant turn: excluded even though usage is present.
        self.assertFalse(_claude_has_usage(e))


if __name__ == "__main__":
    unittest.main()
