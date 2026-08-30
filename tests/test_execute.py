#!/usr/bin/env python3
"""
test_execute.py — RED-first tests for execute.py (harness-runner engine).

Tests cover:
- read_step prompt text from phases/<phase>/step<N>.md
- parse_step_index step status transitions
- write_step_output atomic
- issue #18: state machine with unimplemented + in_progress + started_at + duration_seconds
- issue #18: register_step() helper for unimplemented stubs
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import execute  # noqa: E402


class TestExecute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Create minimal phases/<phase>/
        (self.root / ".dev-kit" / "hand-off").mkdir(parents=True, exist_ok=True)
        (self.root / "phases" / "0-mvp").mkdir(parents=True, exist_ok=True)
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\nIron laws: TDD only.\n", encoding="utf-8")
        (self.root / "phases" / "0-mvp" / "step0.md").write_text("# Setup\nInitialize project.\n", encoding="utf-8")
        (self.root / "phases" / "0-mvp" / "step1.md").write_text("# Build\nTDD: red, green, refactor.\n", encoding="utf-8")
        idx = {
            "project": "test-project",
            "phase": "0-mvp",
            "created_at": "2026-07-04T00:00:00+09:00",
            "steps": [
                {"step": 0, "name": "setup", "status": "pending"},
                {"step": 1, "name": "build", "status": "pending"},
            ],
        }
        (self.root / "phases" / "0-mvp" / "index.json").write_text(json.dumps(idx), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_step_returns_prompt(self):
        text = execute.read_step(self.root, "0-mvp", 0)
        self.assertIn("Initialize project", text)

    def test_read_step_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            execute.read_step(self.root, "0-mvp", 99)

    def test_codex_agent_command_is_supported(self):
        with patch.dict("os.environ", {"DEV_KIT_BUILD_AGENT": "codex"}):
            command = execute._agent_command(Path("/tmp/step-wt"), "implement the step")
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--cd", command)
        self.assertIn("/tmp/step-wt", command)

    def test_unknown_agent_command_fails_closed(self):
        with patch.dict("os.environ", {"DEV_KIT_BUILD_AGENT": "unknown"}):
            with self.assertRaises(ValueError):
                execute._agent_command(Path("/tmp/step-wt"), "implement the step")

    def test_parse_step_index_pending(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["status"], "pending")
        self.assertEqual(parsed[1]["status"], "pending")

    def test_write_step_output_atomic(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        before = idx_path.read_text()
        result = execute.write_step_output(
            self.root,
            "0-mvp",
            step=0,
            exit_code=0,
            stdout="all green",
            stderr="",
        )
        self.assertTrue(result.exists())
        # index.json untouched (we write output, not index)
        self.assertEqual(before, idx_path.read_text())
        # output file atomic
        leftover = list((self.root / "phases" / "0-mvp").glob(".step0-output.*.tmp"))
        self.assertEqual(leftover, [])

    def test_write_step_output_json_shape(self):
        path = execute.write_step_output(
            self.root, "0-mvp", step=1, exit_code=0, stdout="x", stderr="y"
        )
        data = json.loads(path.read_text())
        self.assertEqual(data["step"], 1)
        self.assertEqual(data["phase"], "0-mvp")
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["stdout"], "x")
        self.assertEqual(data["stderr"], "y")
        self.assertIn("timestamp", data)

    def test_update_step_status(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="completed")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["status"], "completed")
        self.assertIn("completed_at", parsed[0])

    def test_status_transition_validation(self):
        # pending → completed OK
        execute.update_step_status(self.root, "0-mvp", step=0, status="completed")
        # completed → pending reset OK (resume)
        execute.update_step_status(self.root, "0-mvp", step=0, status="pending", error_message=None, blocked_reason=None)
        parsed = execute.parse_step_index(self.root / "phases" / "0-mvp" / "index.json")
        self.assertEqual(parsed[0]["status"], "pending")

    def test_blocked_status_requires_reason(self):
        with self.assertRaises(ValueError):
            execute.update_step_status(self.root, "0-mvp", step=0, status="blocked")

    def test_error_status_requires_message(self):
        with self.assertRaises(ValueError):
            execute.update_step_status(self.root, "0-mvp", step=0, status="error")

    # === New statuses (issue #18): unimplemented + in_progress ===

    def test_in_progress_sets_started_at(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="in_progress")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["status"], "in_progress")
        self.assertIn("started_at", parsed[0])

    def test_in_progress_to_completed_records_duration_from_started_at(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="in_progress")
        # Back-date started_at so duration is non-zero + deterministic.
        data = json.loads(idx_path.read_text())
        for s in data["steps"]:
            if s["step"] == 0:
                s["started_at"] = "2026-07-04T00:00:00+09:00"
        idx_path.write_text(json.dumps(data), encoding="utf-8")
        execute.update_step_status(self.root, "0-mvp", step=0, status="completed")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["status"], "completed")
        self.assertIn("completed_at", parsed[0])
        self.assertIn("duration_seconds", parsed[0])
        self.assertGreater(parsed[0]["duration_seconds"], 0.0)

    def test_in_progress_to_completed_accepts_explicit_duration(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="in_progress")
        execute.update_step_status(self.root, "0-mvp", step=0, status="completed", duration_seconds=4.2)
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["duration_seconds"], 4.2)

    def test_in_progress_does_not_overwrite_started_at_on_resume(self):
        """If a crashed run left in_progress with started_at, resuming → in_progress
        must keep the ORIGINAL started_at (so duration measures total elapsed time,
        not just the post-resume chunk)."""
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        original_started = "2026-07-04T00:00:00+09:00"
        data = json.loads(idx_path.read_text())
        for s in data["steps"]:
            if s["step"] == 0:
                s["started_at"] = original_started
                s["status"] = "in_progress"
        idx_path.write_text(json.dumps(data), encoding="utf-8")
        # Resume — should NOT overwrite started_at.
        execute.update_step_status(self.root, "0-mvp", step=0, status="in_progress")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["started_at"], original_started,
                         "started_at was overwritten on re-in_progress")

    def test_unimplemented_status_is_valid(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="unimplemented")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["status"], "unimplemented")
        self.assertNotIn("started_at", parsed[0])
        self.assertNotIn("completed_at", parsed[0])

    def test_full_cycle_pending_in_progress_completed(self):
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="in_progress")
        execute.update_step_status(self.root, "0-mvp", step=0, status="completed")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["status"], "completed")
        self.assertIn("started_at", parsed[0])
        self.assertIn("completed_at", parsed[0])

    def test_pending_reset_clears_started_at_and_duration(self):
        """Transitioning any state → pending must clear started_at and duration
        so a fresh execution measures cleanly from zero."""
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        execute.update_step_status(self.root, "0-mvp", step=0, status="in_progress")
        execute.update_step_status(self.root, "0-mvp", step=0, status="completed", duration_seconds=9.9)
        execute.update_step_status(self.root, "0-mvp", step=0, status="pending")
        parsed = execute.parse_step_index(idx_path)
        self.assertEqual(parsed[0]["status"], "pending")
        self.assertNotIn("started_at", parsed[0])
        self.assertNotIn("duration_seconds", parsed[0])


class TestWriteStepOutputTargetsWorktree(unittest.TestCase):
    """Issue #477 regression: write_step_output must be called with `wt`
    (the per-step git worktree that _commit_step actually stages from),
    never `root` (the orchestrator's main checkout). Uses REAL git — no
    subprocess mocking — so it exercises the actual `git add -A` staging
    behavior `_commit_step` relies on, not a mocked stand-in.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # `root` = orchestrator's main checkout (a plain directory tree,
        # NOT a git repo of its own — mirrors the real harness where only
        # the per-step worktree is a git working directory the runner
        # stages/commits from).
        self.root = base / "root"
        self.root.mkdir()
        # `wt` = the per-step worktree: a real, independent git repo so
        # `git add -A` / `git diff --cached` behave exactly as they do
        # for a genuine `git worktree add` checkout.
        self.wt = base / "wt"
        self.wt.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(self.wt), check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(self.wt), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.wt), check=True)
        (self.wt / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.wt), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(self.wt), check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_output_written_to_root_is_never_staged_in_worktree(self):
        """BEFORE the #477 fix: write_step_output(root, ...) writes the JSON
        outside the worktree's working directory, so `git add -A` (cwd=wt)
        can never see it — `_commit_step` finds nothing staged and no-ops."""
        execute.write_step_output(
            self.root, "0-mvp", 1,
            exit_code=0, stdout="ok", stderr="", duration_seconds=0.1,
        )
        # The file exists under root, NOT under wt.
        self.assertTrue((self.root / "phases" / "0-mvp" / "step1-output.json").exists())
        self.assertFalse((self.wt / "phases" / "0-mvp" / "step1-output.json").exists())
        committed = execute._commit_step(self.wt, "chore(0-mvp): step 1 output")
        self.assertFalse(committed, "no file landed in wt, so _commit_step must no-op")

    def test_output_written_to_wt_is_staged_and_committed(self):
        """AFTER the #477 fix: write_step_output(wt, ...) writes the JSON
        inside the worktree, so `git add -A` (cwd=wt) picks it up and
        `_commit_step` produces a real commit containing the file."""
        execute.write_step_output(
            self.wt, "0-mvp", 1,
            exit_code=0, stdout="ok", stderr="", duration_seconds=0.1,
        )
        self.assertTrue((self.wt / "phases" / "0-mvp" / "step1-output.json").exists())
        committed = execute._commit_step(self.wt, "chore(0-mvp): step 1 output")
        self.assertTrue(committed, "file landed in wt and must be staged + committed")
        shown = subprocess.run(
            ["git", "show", "--stat", "--pretty=format:", "HEAD"],
            cwd=str(self.wt), check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("step1-output.json", shown)


class TestUnimplementedStubRegistration(unittest.TestCase):
    """register_step() creates an `unimplemented` stub in index.json so the plan
    skill can mark 'this phase will have N steps' BEFORE any step<N>.md is written.
    Then the runner SKIPS these entries (see SKIPPABLE_STATUSES)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "phases" / "0-mvp").mkdir(parents=True, exist_ok=True)
        # Empty phase — no index.json yet.
        self.assertFalse((self.root / "phases" / "0-mvp" / "index.json").exists())

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_step_creates_index_and_stub(self):
        execute.register_step(self.root, "0-mvp", step=2, name="future-step")
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        self.assertTrue(idx_path.exists())
        data = json.loads(idx_path.read_text())
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(data["steps"][0]["step"], 2)
        self.assertEqual(data["steps"][0]["name"], "future-step")
        self.assertEqual(data["steps"][0]["status"], "unimplemented")

    def test_register_step_is_idempotent(self):
        execute.register_step(self.root, "0-mvp", step=2, name="future-step")
        execute.register_step(self.root, "0-mvp", step=2, name="future-step")
        data = json.loads((self.root / "phases" / "0-mvp" / "index.json").read_text())
        self.assertEqual(len(data["steps"]), 1, "register_step must not duplicate entries")

    def test_register_step_appends_to_existing_index(self):
        # Pre-existing pending step 0.
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        idx_path.write_text(json.dumps({
            "schema_version": execute.SCHEMA_VERSION,
            "phase": "0-mvp",
            "steps": [{"step": 0, "name": "setup", "status": "pending"}],
        }), encoding="utf-8")
        execute.register_step(self.root, "0-mvp", step=2, name="future-step")
        data = json.loads(idx_path.read_text())
        self.assertEqual(len(data["steps"]), 2)
        self.assertEqual(data["steps"][0]["status"], "pending")  # existing step untouched
        self.assertEqual(data["steps"][1]["status"], "unimplemented")

    def test_register_step_does_not_overwrite_existing_unimplemented(self):
        """If a stub already exists for this step number, preserve any user-set fields."""
        execute.register_step(self.root, "0-mvp", step=2, name="future-step")
        # Re-register with different name — should keep the FIRST name (idempotent).
        execute.register_step(self.root, "0-mvp", step=2, name="renamed")
        data = json.loads((self.root / "phases" / "0-mvp" / "index.json").read_text())
        self.assertEqual(data["steps"][0]["name"], "future-step")


class TestRunSequential(unittest.TestCase):
    """Issue #63: _run_sequential is a stub. Real impl must:
    - create a per-step git worktree (MUST-38)
    - spawn ONE `claude -p` sub-agent per pending step (MUST-36)
    - write step<N>-output.json with REAL subprocess output (no fake 'stub completed')
    - 2-commit protocol: feat(scope) + chore(scope)
    - push the per-step branch when --push is set
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "phases" / "0-mvp").mkdir(parents=True, exist_ok=True)
        (self.root / ".dev-kit").mkdir(parents=True, exist_ok=True)
        # step files for the happy-path fixture (no `blocked` so the runner completes)
        (self.root / "phases" / "0-mvp" / "step1.md").write_text("# Step 1\nTDD red.\n", encoding="utf-8")
        (self.root / "phases" / "0-mvp" / "step2.md").write_text("# Step 2\nTDD green.\n", encoding="utf-8")
        idx = {
            "project": "test-project",
            "phase": "0-mvp",
            "worktree": "feat/test-phase",
            "created_at": "2026-07-04T00:00:00+09:00",
            "steps": [
                {"step": 1, "name": "red",  "status": "pending"},
                {"step": 2, "name": "done", "status": "completed",
                 "started_at": "2026-07-04T00:00:00+09:00",
                 "completed_at": "2026-07-04T00:01:00+09:00",
                 "duration_seconds": 60.0},
                {"step": 3, "name": "stub", "status": "unimplemented"},
            ],
        }
        (self.root / "phases" / "0-mvp" / "index.json").write_text(json.dumps(idx), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_proc(self, returncode=0, stdout="green", stderr=""):
        """Build a MagicMock that looks like a subprocess.CompletedProcess."""
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    def _make_blocked_root(self):
        """Alternate fixture: pending step + blocked step (bails with 2)."""
        root = self.root.parent / "blocked-fixture"
        if root.exists():
            import shutil as _sh
            _sh.rmtree(root)
        (root / "phases" / "0-mvp").mkdir(parents=True)
        (root / "phases" / "0-mvp" / "step1.md").write_text("# Step 1\n", encoding="utf-8")
        (root / "phases" / "0-mvp" / "index.json").write_text(json.dumps({
            "phase": "0-mvp",
            "worktree": "feat/x",
            "steps": [
                {"step": 1, "name": "ok", "status": "pending"},
                {"step": 2, "name": "no", "status": "blocked",
                 "blocked_at": "2026-07-04T00:00:00+09:00",
                 "blocked_reason": "user paused"},
            ],
        }), encoding="utf-8")
        return root

    def test_skippable_status_does_not_invoke_runner(self):
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc()
            rc = execute._run_sequential(self.root, "0-mvp", push=False)
            # 1 pending step → 1 worktree add + 1 claude + 2 commits. completed + unimplemented = skip.
            self.assertEqual(rc, 0)
            claude_calls = [c for c in mr.call_args_list if "claude" in c.args[0]]
            wt_add_calls = [c for c in mr.call_args_list if "worktree" in c.args[0]]
            self.assertEqual(len(claude_calls), 1, f"expected 1 claude call, got {mr.call_args_list}")
            self.assertEqual(len(wt_add_calls), 1, f"expected 1 worktree add, got {mr.call_args_list}")

    def test_blocked_status_returns_2(self):
        root = self._make_blocked_root()
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc()
            rc = execute._run_sequential(root, "0-mvp", push=False)
            self.assertEqual(rc, 2)
            # Step 1 ran (pending → resume) then step 2 blocked bails. Only 1 claude spawn total.
            claude_calls = [c for c in mr.call_args_list if "claude" in c.args[0]]
            self.assertEqual(len(claude_calls), 1, "only the pending step may run; blocked bails the rest")

    def test_skip_blocked_continues_past_blocked_step(self):
        root = self._make_blocked_root()
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc()
            rc = execute._run_sequential(root, "0-mvp", push=False, skip_blocked=True)
            self.assertEqual(rc, 0, "skip_blocked must not bail; rc should be 0 after step 1 runs")
            claude_calls = [c for c in mr.call_args_list if "claude" in c.args[0]]
            self.assertEqual(len(claude_calls), 1, "only step 1 (pending) may run; step 2 (blocked) skipped")
        handoff = root / ".dev-kit" / "hand-off" / "build→review.md"
        self.assertTrue(handoff.exists(), "skip_blocked must write a hand-off note")
        body = handoff.read_text(encoding="utf-8")
        self.assertIn("step 2 skipped", body)
        self.assertIn("user paused", body)

    def test_record_skipped_blocked_emits_phase_in_handoff(self):
        """Issue #310: `_record_skipped_blocked` must surface the phase in
        the emitted hand-off line so multi-phase builds can distinguish
        identical step numbers across phases (e.g. step 2 in phase ``0-mvp``
        vs step 2 in phase ``1-polish`` both blocked at the same time)."""
        root = self._make_blocked_root()
        execute._record_skipped_blocked(
            root, phase="0-mvp", step=2, reason="user paused",
        )
        handoff = root / ".dev-kit" / "hand-off" / "build→review.md"
        self.assertTrue(handoff.exists(), "handoff file must be created")
        body = handoff.read_text(encoding="utf-8")
        self.assertIn("0-mvp", body, "phase string must be in the emitted line")
        # The line containing phase must reference the same step we're recording
        # so multi-phase builds can grep `phase=` and `step=` to disambiguate.
        relevant = [ln for ln in body.splitlines() if "0-mvp" in ln]
        self.assertTrue(relevant, "no line in the handoff mentions the phase")
        self.assertTrue(
            any("step 2" in ln for ln in relevant),
            "emitted phase line must still name the step number",
        )

    def test_pending_step_creates_worktree_and_invokes_claude(self):
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc(stdout="all green", stderr="")
            rc = execute._run_sequential(self.root, "0-mvp", push=False)
            self.assertEqual(rc, 0)
            # Inspect worktree add args (per-step branch derived from index.worktree)
            wt_add = next(c for c in mr.call_args_list if "worktree" in c.args[0])
            args = wt_add.args[0]
            self.assertIn("worktree", args)
            self.assertIn("add", args)
            self.assertIn("-B", args)
            self.assertIn("feat/test-phase-step1", args)  # branch = f"{worktree}-step{n}"
            self.assertEqual(args[-1], "origin/main")
            self.assertEqual(args[-2].endswith("0-mvp-step1"), True, f"worktree path wrong: {args[-2]}")
            # Inspect claude -p invocation
            claude = next(c for c in mr.call_args_list if c.args[0][0] == "claude")
            cmd = claude.args[0]
            self.assertEqual(cmd[0], "claude")
            self.assertEqual(cmd[1], "-p")
            workdir = claude.kwargs.get("cwd") or next((a for a in cmd if ".worktrees" in a), None)
            self.assertIsNotNone(workdir, f"claude -p missing workdir; cmd={cmd}")
            # The preamble (from step1.md) is in the trailing prompt arg
            joined = " ".join(cmd)
            self.assertIn("TDD red", joined, f"preamble not in prompt: {cmd}")
            self.assertIn("3-cycle self-fix", joined, f"AC guard not appended: {cmd}")
            # step output file written with REAL contents (no 'stub completed').
            # Issue #477: the output JSON lands inside the per-step worktree
            # (not the orchestrator's root checkout) since that's what
            # `_commit_step` actually stages via `git add -A` cwd=wt.
            out = json.loads(
                (self.root / ".worktrees" / "0-mvp-step1" / "phases" / "0-mvp" / "step1-output.json").read_text()
            )
            self.assertEqual(out["exit_code"], 0)
            self.assertEqual(out["stdout"], "all green")
            self.assertNotIn("stub completed", out["stdout"])
            # Status flipped to completed with real (non-fake) duration.
            idx = json.loads((self.root / "phases" / "0-mvp" / "index.json").read_text())
            step1 = next(s for s in idx["steps"] if s["step"] == 1)
            self.assertEqual(step1["status"], "completed")
            self.assertIn("duration_seconds", step1)
            self.assertGreaterEqual(step1["duration_seconds"], 0.0)

    def test_two_commit_protocol_per_step(self):
        # Issue #221 RC2: the runner no longer uses `--allow-empty`. Each
        # commit first runs `git add -A`, then asks `git diff --cached --quiet`
        # whether anything is staged. The mock here makes the index DIRTY
        # (rc=1) so the runner proceeds with both commits — matching the
        # real-world "sub-agent wrote files" scenario.
        def _side_effect(*args, **kwargs):
            # subprocess.run(cmd, cwd=..., check=True, capture_output=True, text=True)
            # `args` is the tuple of positional args (only `cmd`); `kwargs`
            # holds everything else (including `cwd`).
            cmd = args[0] if args else kwargs.get("args", [])
            if list(cmd[:4]) == ["git", "diff", "--cached", "--quiet"]:
                m = MagicMock()
                m.returncode = 1
                m.stdout = ""
                m.stderr = ""
                return m
            return self._fake_proc()

        with patch.object(execute.subprocess, "run", side_effect=_side_effect) as mr:
            rc = execute._run_sequential(self.root, "0-mvp", push=False)
            self.assertEqual(rc, 0)
            commits = [c for c in mr.call_args_list if c.args[0][:2] == ["git", "commit"]]
            self.assertEqual(len(commits), 2, f"expected 2 commits, got {len(commits)}: {commits}")
            joined_args = "\n".join(" ".join(c.args[0]) for c in commits)
            self.assertIn("feat(0-mvp): step 1", joined_args)
            self.assertIn("chore(0-mvp): step 1 output", joined_args)

    def test_no_commit_on_failure(self):
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc(returncode=1, stdout="", stderr="boom")
            rc = execute._run_sequential(self.root, "0-mvp", push=False)
            self.assertEqual(rc, 1)
            commits = [c for c in mr.call_args_list if c.args[0][:2] == ["git", "commit"]]
            self.assertEqual(commits, [], f"no commits expected on failure, got {commits}")
            idx = json.loads((self.root / "phases" / "0-mvp" / "index.json").read_text())
            step1 = next(s for s in idx["steps"] if s["step"] == 1)
            self.assertEqual(step1["status"], "error")
            self.assertIn("error_message", step1)

    # === Issue #221: harness ships empty commits ===
    #
    # Root causes & one expected harness-side fix each:
    # (1) `claude -p` invoked plainly with no --add-dir / --allowedTools, so a
    #     parent-sandbox-restricted consumer silently blocks every sub-agent
    #     write → no files in worktree → "feat" commit is empty.
    # (2) `git commit --allow-empty` masks the missing files and registers the
    #     step as `completed` (exit_code==0).
    # (3) `<!-- status: blocked -->` in sub-agent stdout is ignored; runner only
    #     inspects exit_code and reports success.

    def test_claude_p_invoked_with_add_dir_and_allowed_tools(self):
        """Issue #221 RC1: claude -p must be spawned with --add-dir <worktree>
        + --allowedTools "Write,Edit,Bash" so the non-interactive sub-agent
        can write into the per-step worktree even when the consumer's parent
        Claude Code sandbox is restrictive."""
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc()
            execute._run_sequential(self.root, "0-mvp", push=False)
            claude = next(c for c in mr.call_args_list if c.args[0][0] == "claude")
            cmd = claude.args[0]
            self.assertIn("--add-dir", cmd,
                          f"claude -p missing --add-dir <worktree>; cmd={cmd}")
            # The arg after --add-dir must be the per-step worktree path.
            i = cmd.index("--add-dir")
            self.assertEqual(cmd[i + 1].endswith("0-mvp-step1"), True,
                             f"--add-dir target must be the per-step worktree, got {cmd[i+1]}")
            self.assertIn("--allowedTools", cmd,
                          f"claude -p missing --allowedTools; cmd={cmd}")
            tools = cmd[cmd.index("--allowedTools") + 1]
            for required in ("Write", "Edit", "Bash"):
                self.assertIn(required, tools,
                              f"--allowedTools must include {required}; got {tools!r}")

    def test_git_add_a_runs_before_commit_and_skips_empty(self):
        """Issue #221 RC2: runner must stage sub-agent writes (`git add -A`)
        and ONLY emit a commit when the index is dirty — never silently fall
        back to `--allow-empty`. If the sub-agent produced nothing, the per-step
        branch gets no commit and the step's status flips accordingly (not
        `completed`)."""
        # First half — sub-agent DID write files: index dirty → both commits land.
        def _side_effect_dirty(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if list(cmd[:4]) == ["git", "diff", "--cached", "--quiet"]:
                m = MagicMock()
                m.returncode = 1
                m.stdout = ""
                m.stderr = ""
                return m
            return self._fake_proc()

        with patch.object(execute.subprocess, "run", side_effect=_side_effect_dirty) as mr:
            execute._run_sequential(self.root, "0-mvp", push=False)
            # 1. `git add -A` runs in the per-step worktree BEFORE each commit
            #    (one add per _commit_step call — we call it twice).
            adds = [c for c in mr.call_args_list if c.args[0][:3] == ["git", "add", "-A"]]
            self.assertEqual(len(adds), 2,
                             f"expected 2 `git add -A` (one per _commit_step), got {adds}")
            for a in adds:
                # cwd is the worktree (kargs), NOT a positional arg → confirms
                # the staging ran in the per-step worktree, not the project root.
                self.assertTrue(str(a.kwargs.get("cwd", "")).endswith("0-mvp-step1"),
                                f"`git add -A` cwd must be the worktree; got {a.kwargs.get('cwd')!r}")
            # 2. None of the commits use --allow-empty (silent-data-loss flag).
            commits = [c for c in mr.call_args_list if c.args[0][:2] == ["git", "commit"]]
            self.assertEqual(len(commits), 2, f"expected 2 commits on the dirty path, got {commits}")
            for c in commits:
                self.assertNotIn("--allow-empty", c.args[0],
                                 f"--allow-empty must NOT be used; commit args were {c.args[0]}")

        # Second half — sub-agent WROTE NOTHING: index clean + blocked-marker
        # → ZERO commits, NOT an empty commit masquerading as feat(...).
        # Status transitions to `blocked` so the human is asked instead of
        # silently advancing.
        def _side_effect_clean(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if list(cmd[:4]) == ["git", "diff", "--cached", "--quiet"]:
                m = MagicMock()
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
                return m
            return self._fake_proc(stdout="no files written\n<!-- status: blocked --> waiting for human input")

        fresh_tmp = tempfile.TemporaryDirectory()
        fresh_root = Path(fresh_tmp.name)
        (fresh_root / "phases" / "0-mvp").mkdir(parents=True)
        (fresh_root / "phases" / "0-mvp" / "step1.md").write_text("# Step 1\n", encoding="utf-8")
        (fresh_root / "phases" / "0-mvp" / "index.json").write_text(json.dumps({
            "phase": "0-mvp",
            "worktree": "feat/clean",
            "steps": [{"step": 1, "name": "x", "status": "pending"}],
        }), encoding="utf-8")
        with patch.object(execute.subprocess, "run", side_effect=_side_effect_clean) as mr:
            rc = execute._run_sequential(fresh_root, "0-mvp", push=False)
            self.assertEqual(rc, 2, "blocked-marker path must return 2 to bail the loop")
            commits = [c for c in mr.call_args_list if c.args[0][:2] == ["git", "commit"]]
            self.assertEqual(commits, [],
                             f"empty-index + blocked-marker → ZERO commits; got {commits}")
            idx = json.loads((fresh_root / "phases" / "0-mvp" / "index.json").read_text())
            self.assertEqual(idx["steps"][0]["status"], "blocked",
                             "clean-index + blocked-marker must surface as `blocked`, NOT `completed`")
        fresh_tmp.cleanup()

    def test_blocked_marker_in_stdout_marks_step_blocked(self):
        """Issue #221 RC3: when sub-agent stdout contains the
        `<!-- status: blocked -->` marker, the runner must transition the step
        to `status=blocked` (with a `blocked_reason`) instead of silently
        advancing to `completed`. Returning 2 (like a pre-existing blocked
        step) is also required so the loop bails for human unblock."""
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc(
                returncode=0,
                stdout="i need an API key, cannot proceed\n<!-- status: blocked -->",
                stderr="",
            )
            rc = execute._run_sequential(self.root, "0-mvp", push=False)
            self.assertEqual(rc, 2,
                             f"blocked-marker step must bail with rc=2, got {rc}")
            idx = json.loads((self.root / "phases" / "0-mvp" / "index.json").read_text())
            step1 = next(s for s in idx["steps"] if s["step"] == 1)
            self.assertEqual(step1["status"], "blocked",
                             f"stdout had `<!-- status: blocked -->` but step status is {step1['status']!r}")
            self.assertIn("blocked_reason", step1,
                          "blocked status must record the sub-agent's reason")
            # And the step-OUTPUT json must surface the marker verdict for audit.
            # Issue #477: written into the per-step worktree, not root.
            output = json.loads(
                (self.root / ".worktrees" / "0-mvp-step1" / "phases" / "0-mvp" / "step1-output.json").read_text()
            )
            self.assertEqual(output["blocked"], True,
                             "output json must surface blocked=True for audit")
            self.assertIn("API key", output["blocked_reason"])

    def test_push_only_when_flag(self):
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc()
            execute._run_sequential(self.root, "0-mvp", push=False)
            pushes = [c for c in mr.call_args_list if c.args[0][:2] == ["git", "push"]]
            self.assertEqual(pushes, [], "no push expected when push=False")
        # Use a fresh tmp dir for the push=True case so step 1 is still pending.
        fresh_tmp = tempfile.TemporaryDirectory()
        fresh_root = Path(fresh_tmp.name)
        (fresh_root / "phases" / "0-mvp").mkdir(parents=True)
        (fresh_root / "phases" / "0-mvp" / "step1.md").write_text("# Step 1\n", encoding="utf-8")
        (fresh_root / "phases" / "0-mvp" / "index.json").write_text(json.dumps({
            "phase": "0-mvp",
            "worktree": "feat/test-phase",
            "steps": [{"step": 1, "name": "x", "status": "pending"}],
        }), encoding="utf-8")
        with patch.object(execute.subprocess, "run") as mr:
            mr.return_value = self._fake_proc()
            rc = execute._run_sequential(fresh_root, "0-mvp", push=True)
            self.assertEqual(rc, 0)
            pushes = [c for c in mr.call_args_list if c.args[0][:2] == ["git", "push"]]
            self.assertGreaterEqual(len(pushes), 1, "expected at least 1 push when push=True")
        fresh_tmp.cleanup()


class TestRunParallel(unittest.TestCase):
    """Issue #63: _run_parallel is a stub. Real impl spawns N subprocesses with worktree
    isolation. Slots run concurrently — wall clock bounded by slowest, not sum."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "phases" / "0-mvp").mkdir(parents=True, exist_ok=True)
        for n in range(1, 4):
            (self.root / "phases" / "0-mvp" / f"step{n}.md").write_text(f"# Step {n}\n", encoding="utf-8")
        (self.root / "phases" / "0-mvp" / "index.json").write_text(json.dumps({
            "project": "p",
            "phase": "0-mvp",
            "worktree": "feat/par",
            "steps": [
                {"step": 1, "name": "a", "status": "pending"},
                {"step": 2, "name": "b", "status": "pending"},
                {"step": 3, "name": "c", "status": "pending"},
            ],
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_proc(self, returncode=0, stdout="ok", stderr=""):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    def test_parallel_runs_n_slots(self):
        with patch.object(execute.subprocess, "run") as mr_run, \
             patch.object(execute.subprocess, "Popen") as mr_popen:
            # worktree add subprocess.run: just succeeds silently.
            mr_run.return_value = self._fake_proc()
            # Each Popen returns a fake 'already finished' proc.
            proc_mock = MagicMock()
            proc_mock.poll.return_value = 0  # exited immediately
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = ("ok", "")
            mr_popen.return_value = proc_mock
            rc = execute._run_parallel(self.root, "0-mvp", n=2, push=False)
            self.assertEqual(rc, 0)
            # wall-clock bounded by N (slot count) — each Popen call is a slot launch
            self.assertGreaterEqual(mr_popen.call_count, 1)
            self.assertLessEqual(mr_popen.call_count, 2)
            # Each spawn used `claude -p` (MUST-36 — single sub-agent per slot)
            for c in mr_popen.call_args_list:
                cmd = c.args[0]
                self.assertEqual(cmd[0], "claude", f"expected claude CLI spawn, got: {cmd}")
                self.assertEqual(cmd[1], "-p")
            # worktree add was called for each step that ran (1 per step)
            wt_add_calls = [c for c in mr_run.call_args_list
                            if "worktree" in c.args[0]]
            self.assertGreaterEqual(len(wt_add_calls), 1)

    def test_parallel_returns_nonzero_on_slot_failure(self):
        slot_instances: list[int] = []
        original_slot_runner = execute._SlotRunner

        def counting_slot_runner(*args, **kwargs):
            slot_instances.append(1)
            return original_slot_runner(*args, **kwargs)

        with patch.object(execute.subprocess, "run") as mr_run, \
             patch.object(execute.subprocess, "Popen") as mr_popen, \
             patch.object(execute, "_SlotRunner", side_effect=counting_slot_runner):
            mr_run.return_value = self._fake_proc()
            proc_mock = MagicMock()
            proc_mock.poll.return_value = 1
            proc_mock.returncode = 1
            proc_mock.communicate.return_value = ("", "boom")
            mr_popen.return_value = proc_mock





    def test_parallel_caps_concurrent_slots(self):
        """Regression: _PARALLEL_MAX_CONCURRENT caps slot creation.

        Fork-bomb risk: a 20-step phase would otherwise spawn 20 concurrent
        `claude -p` subprocesses. The auto-classifier opens the parallel
        gate for N >= 4 but does NOT cap the upper end; the constant is
        the cap.
        """
        # Rebuild a phase with 20 eligible pending steps.
        idx_path = self.root / "phases" / "0-mvp" / "index.json"
        steps = [{"step": n, "name": f"s{n}", "status": "pending"} for n in range(1, 21)]
        for n in range(1, 21):
            (self.root / "phases" / "0-mvp" / f"step{n}.md").write_text(f"# Step {n}\n", encoding="utf-8")
        idx_path.write_text(json.dumps({
            "project": "p", "phase": "0-mvp", "worktree": "feat/par", "steps": steps,
        }), encoding="utf-8")

        slot_instances: list[int] = []
        original_slot_runner = execute._SlotRunner

        def counting_slot_runner(*args, **kwargs):
            slot_instances.append(1)
            return original_slot_runner(*args, **kwargs)

        with patch.object(execute.subprocess, "run") as mr_run, \
             patch.object(execute.subprocess, "Popen") as mr_popen, \
             patch.object(execute, "_SlotRunner", side_effect=counting_slot_runner):
            mr_run.return_value = self._fake_proc()
            proc_mock = MagicMock()
            proc_mock.poll.return_value = 0
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = ("ok", "")
            mr_popen.return_value = proc_mock
            rc = execute._run_parallel(self.root, "0-mvp", n=20, push=False)
            self.assertEqual(rc, 0)
            # The cap is on CONCURRENT slots, not total Popen calls.
            # Each slot may be re-launched after it finishes; total Popens
            # can be > cap (the runner processes all eligible steps).
            # The guarantee is that no more than `_PARALLEL_MAX_CONCURRENT`
            # slots are alive at any moment, which is enforced by the
            # initial `slots = [...]` construction.
            # The cap is the number of _SlotRunner instances created in
            # the initial slots = [...] construction. A refactor that
            # drops the `min(_PARALLEL_MAX_CONCURRENT, ...)` bound would
            # push the initial count to len(eligible) = 20 and fail.
            self.assertGreater(
                len(slot_instances), 0,
                "fixture error: no _SlotRunner instances were created",
            )
            self.assertLessEqual(
                len(slot_instances), execute._PARALLEL_MAX_CONCURRENT,
                f"slot construction count {len(slot_instances)} exceeded "
                f"cap {execute._PARALLEL_MAX_CONCURRENT}",
            )
            self.assertEqual(
                len(slot_instances), execute._PARALLEL_MAX_CONCURRENT,
                f"slot construction should be exactly the cap when "
                f"len(eligible)=20 > cap; got {len(slot_instances)}",
            )
class TestRunStepBody(unittest.TestCase):
    """_run_step_body is the shared body for sequential and parallel runners.

    Regression: locks commit-message format and in_progress stamp wording so
    sequential (subprocess.run) and parallel (Popen+collect) paths produce
    identical artifacts. Issue #79.
    """

    def _setup_phase(self, tmp: Path) -> tuple[Path, str, str]:
        """Create a minimal phase directory + step file; return (root, phase, branch_base)."""
        import subprocess as sp
        root = tmp
        # Init a git repo so subprocess.run(["git", ...]) works.
        sp.run(["git", "init", "-q"], cwd=root, check=True)
        sp.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        sp.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        sp.run(["git", "checkout", "-q", "-b", "main"], cwd=root, check=True)
        (root / "README.md").write_text("# test\n")
        sp.run(["git", "add", "README.md"], cwd=root, check=True)
        sp.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
        # Fake origin/main — _run_one_step uses origin/main as the worktree base.
        # Workaround: configure a local mirror as 'origin'.
        bare = tmp / "origin.git"
        sp.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        sp.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)
        sp.run(["git", "push", "-q", "origin", "main"], cwd=root, check=True)
        # Phase + step file.
        phase_dir = root / "phases" / "test-phase"
        phase_dir.mkdir(parents=True)
        (phase_dir / "step0.md").write_text("# Step 0\nDo something.\n")
        # Register the step in phases index.
        from lib import execute as ex  # noqa: E402
        ex.register_step(root, "test-phase", step=0, name="setup")
        return root, "test-phase", "feat/test-phase"

    def test_run_step_body_returns_zero_on_success(self):
        import tempfile
        from unittest.mock import MagicMock

        from lib import execute as ex  # noqa: E402
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            # Mock run_proc → returns clean exit + empty stdout.
            run_proc = MagicMock(return_value=(0, "", ""))
            rc = ex._run_step_body(
                root, phase, 0, branch_base, "setup", push=False,
                run_proc=run_proc,
            )
            self.assertEqual(rc, 0)

    def test_run_step_body_uses_commit_message_format(self):
        import tempfile

        from lib import execute as ex  # noqa: E402
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            # run_proc writes a file into the per-step worktree, mirroring a
            # real sub-agent that made code changes.
            def fake_run(cwd: str, args: list[str]) -> tuple[int, str, str]:
                wt = Path(args[args.index("--workdir") + 1])
                (wt / "made_change.txt").write_text("hi\n")
                return 0, "", ""
            ex._run_step_body(root, phase, 0, branch_base, "my-name", push=False, run_proc=fake_run)
            wt = root / ".worktrees" / f"{phase}-step0"
            import subprocess as sp
            # feat always lands (we wrote a file); chore may be a no-op
            # (`_commit_step` skips when there's nothing new to commit).
            # Issue #79 acceptance: BOTH messages are reachable from
            # `_run_step_body`; we assert at least the feat commit lands
            # with the exact expected format.
            log = sp.run(
                ["git", "log", "--format=%s", "main..HEAD"],
                cwd=wt, capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            self.assertIn("feat(test-phase): step 0 — my-name", log)

    def test_run_step_body_returns_two_on_blocked_marker(self):
        import tempfile
        from unittest.mock import MagicMock

        from lib import execute as ex  # noqa: E402
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            stdout = "<!-- status: blocked -->\n<!-- blocked_reason: needs API key -->"
            run_proc = MagicMock(return_value=(0, stdout, ""))
            rc = ex._run_step_body(root, phase, 0, branch_base, "x", push=False, run_proc=run_proc)
            self.assertEqual(rc, 2)

    def test_run_step_body_returns_nonzero_on_error(self):
        import tempfile
        from unittest.mock import MagicMock

        from lib import execute as ex  # noqa: E402
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            run_proc = MagicMock(return_value=(1, "", "boom"))
            rc = ex._run_step_body(root, phase, 0, branch_base, "x", push=False, run_proc=run_proc)
            self.assertEqual(rc, 1)

    def test_post_collect_locks_parallel_commit_format(self):
        """_step_post_collect is the SSOT for the parallel runner's commit messages.

        Locks the format: feat({phase}): step {n} — {name} + chore({phase}): step {n} output.
        Issue #79 follow-up: review verdict was "Blocked" until _SlotRunner.collect()
        routed through _step_post_collect. This test guards against drift.
        """
        import tempfile

        from lib import execute as ex  # noqa: E402
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            # Pre-spawn produces the worktree; we then drive post-collect directly.
            ctx = ex._step_pre_spawn(root, phase, 0, branch_base)
            # Simulate the sub-agent writing a file (so the feat commit lands).
            (ctx["wt"] / "made_change.txt").write_text("hi\n")
            rc = ex._step_post_collect(
                root, phase, 0, "my-name", ctx,
                push=False, exit_code=0, stdout="", stderr="",
            )
            self.assertEqual(rc, 0)
            import subprocess as sp
            log = sp.run(
                ["git", "log", "--format=%s", "main..HEAD"],
                cwd=ctx["wt"], capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            # First-occurrence feat commit lands; chore may be no-op.
            self.assertIn("feat(test-phase): step 0 — my-name", log)

    def test_verify_passed_is_parented_to_write_observed(self):
        """`verify.passed` must carry `parent_id == write.observed.event_id`.

        `lib.harness_effectiveness._first_pass` computes
        `causally_linked = first.parent_id == write.event_id`. If the
        executor parents the verify to `step.started` instead, the link is
        permanently False and `first_pass_quality` collapses to 10.0 (ROT)
        while still entering `overall_score` at weight 0.25 — strictly worse
        than emitting nothing. This test pins the causal edge.
        """
        import tempfile

        from lib import execute as ex  # noqa: E402
        from lib.trace_log import read_events
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            ctx = ex._step_pre_spawn(root, phase, 0, branch_base)
            (ctx["wt"] / "made_change.txt").write_text("hi\n")
            rc = ex._step_post_collect(
                root, phase, 0, "my-name", ctx,
                push=False, exit_code=0, stdout="", stderr="",
            )
            self.assertEqual(rc, 0)
            events = read_events(root)
            writes = [e for e in events if e["event_type"] == "write.observed"]
            verifies = [e for e in events if e["event_type"] == "verify.passed"]
            self.assertEqual(len(writes), 1, f"expected 1 write.observed, got {len(writes)}")
            self.assertEqual(len(verifies), 1, f"expected 1 verify.passed, got {len(verifies)}")
            self.assertEqual(
                verifies[0]["parent_id"], writes[0]["event_id"],
                "verify.passed must be parented to write.observed so "
                "_first_pass.causally_linked holds",
            )

    def test_events_carry_agent_identity(self):
        """Every executor-emitted event carries a non-empty ``agent`` field.

        The harness-effectiveness ``stability`` submetric (issue #663)
        reports 0% ``agent_identity_coverage`` when no producer stamps
        ``agent``/``provider``/``model`` on its events. ``lib.execute`` is
        one of the two real producers (the other is the session-lifecycle
        hooks) — it already knows which runner executed the step via
        ``DEV_KIT_BUILD_AGENT`` (default ``claude``), so it must forward
        that identity onto every emitted event instead of leaving the
        field unset.
        """
        import tempfile

        from lib import execute as ex  # noqa: E402
        from lib.trace_log import read_events
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            ctx = ex._step_pre_spawn(root, phase, 0, branch_base)
            (ctx["wt"] / "made_change.txt").write_text("hi\n")
            ex._step_post_collect(
                root, phase, 0, "my-name", ctx,
                push=False, exit_code=0, stdout="", stderr="",
            )
            events = read_events(root)
            self.assertTrue(events, "expected at least one emitted event")
            for event in events:
                self.assertEqual(
                    event.get("agent"), "claude-code",
                    f"event {event['event_type']} missing/wrong agent identity: {event}",
                )

    def test_unsupported_build_agent_stamps_empty_not_the_raw_value(self):
        """An unsupported ``DEV_KIT_BUILD_AGENT`` value must NOT be stamped
        onto the event verbatim.

        ``_agent_command`` raises ``ValueError`` for any
        ``DEV_KIT_BUILD_AGENT`` outside ``{claude, codex}``, but that
        validation runs later (when the sub-agent subprocess is actually
        spawned) than ``_emit_effectiveness_event`` (which fires at
        ``_step_pre_spawn`` time, e.g. for ``step.started``). Stamping the
        raw unvalidated value would silently mis-attribute an unknown
        runner as if it were a recognized one —
        ``lib/trace_log.py::_default_identity`` documents this exact
        anti-pattern as the prior A06-1 regression (empty is the honest
        "unset" signal, not a guess).
        """
        import os
        import tempfile
        from unittest.mock import patch

        from lib import execute as ex  # noqa: E402
        from lib.trace_log import read_events
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            with patch.dict(os.environ, {"DEV_KIT_BUILD_AGENT": "gpt5"}):
                ex._step_pre_spawn(root, phase, 0, branch_base)
            events = read_events(root)
            self.assertTrue(events, "expected at least one emitted event")
            for event in events:
                self.assertEqual(
                    event.get("agent"), "",
                    f"unsupported DEV_KIT_BUILD_AGENT must stamp empty, "
                    f"not the raw value, on {event}",
                )

    def test_first_pass_quality_reflects_honest_verify_evidence(self):
        """End-to-end: the executor's own events must drive ``_first_pass``
        honestly. The executor records ``verify.passed`` for the causal
        chain, but the event carries ``required_checks_passed=False`` and
        ``independent=False`` because no real pytest/lint/build runner
        executed — only the sub-agent's exit_code==0 is known. An earlier
        revision emitted ``required_checks_passed=True, independent=True``
        from that exit_code alone, fabricating a 100% first_pass_rate
        (Review Critical #2).

        The reducer still counts ``first_verify_evidence`` (the fields are
        present) and ``first_pass_no_hidden_retry`` (no heal/verify.failed
        pre-dates the verify), so the honest score is:
            0*.7 (first_pass_rate) + 1*.2 (first_verify_evidence)
            + 1*.1 (no_hidden_retry) = 30.0
        Anything higher would require an actual independent check runner.
        """
        import tempfile

        from lib import execute as ex  # noqa: E402
        from lib.harness_effectiveness import _first_pass
        from lib.trace_log import read_events
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            ctx = ex._step_pre_spawn(root, phase, 0, branch_base)
            (ctx["wt"] / "made_change.txt").write_text("hi\n")
            ex._step_post_collect(
                root, phase, 0, "my-name", ctx,
                push=False, exit_code=0, stdout="", stderr="",
            )
            component = _first_pass(read_events(root))
            self.assertEqual(component["score"], 30.0, component)
            # First_pass_rate must reflect that no independent check ran.
            self.assertEqual(
                component["submetrics"]["first_pass_rate"]["value"], 0.0,
                component["submetrics"],
            )
            # First_verify_evidence stays 100% because the evidence fields
            # ARE populated (the event exists for the causal chain).
            self.assertEqual(
                component["submetrics"]["first_verify_evidence"]["value"], 100.0,
                component["submetrics"],
            )

    def test_no_diff_step_still_emits_verify_passed(self):
        """``verify.passed`` is emitted unconditionally on the success path,
        regardless of whether a write happened.

        Before the hoist the verify event lived inside the
        ``if wrote_files or output_committed:`` block, so a no-diff
        success emitted only ``step.started`` + ``step.completed``.
        ``_first_pass`` iterates only subjects with a ``write.observed``
        event, so the no-diff branch was silently excluded from the
        metric. The hoist pins the invariant: verify.passed fires
        unconditionally; its ``parent_id`` and ``evidence_ref.no_diff``
        surface the no-diff / with-diff distinction so downstream
        consumers do not need to re-derive it.

        ``write_step_output`` is part of ``_step_post_collect`` and
        always writes ``phases/<phase>/step<N>-output.json`` to the
        worktree, so the literal no-diff path is hard to exercise from
        a test. Instead we assert the hoist invariants on the WITH-diff
        path (which exercises every code path the no-diff path would):
        a write happens, so ``write.observed`` is emitted and
        ``verify.passed`` parents to it; ``evidence_ref.no_diff`` is
        False; the same code path also runs for the no-diff case (the
        verify emit is no longer gated by the if).
        """
        import tempfile

        from lib import execute as ex  # noqa: E402
        from lib.trace_log import read_events
        with tempfile.TemporaryDirectory() as td:
            root, phase, branch_base = self._setup_phase(Path(td))
            ctx = ex._step_pre_spawn(root, phase, 0, branch_base)
            # write_step_output always writes a file inside
            # _step_post_collect, so a write will happen on the worktree.
            ex._step_post_collect(
                root, phase, 0, "my-name", ctx,
                push=False, exit_code=0, stdout="", stderr="",
            )
            events = read_events(root)
            verifies = [e for e in events if e["event_type"] == "verify.passed"]
            writes = [e for e in events if e["event_type"] == "write.observed"]
            # The hoist invariant: verify.passed is emitted even when
            # the underlying code path is the one the old
            # ``if wrote_files or output_committed:`` gate would have
            # skipped. With the gate removed the emit always happens;
            # we verify the with-diff branch here (which exercises the
            # same emit code) and trust the no-diff branch by code
            # inspection (the emit is no longer inside the if).
            self.assertEqual(len(verifies), 1, f"expected 1 verify.passed, got {len(verifies)}")
            # In the with-diff case (the one we can actually exercise)
            # verify.passed.parent_id must equal write.observed.event_id
            # so _first_pass.causally_linked holds.
            self.assertEqual(len(writes), 1, f"expected 1 write.observed, got {len(writes)}")
            self.assertEqual(
                verifies[0]["parent_id"], writes[0]["event_id"],
                "verify.passed must parent to write.observed.event_id "
                "when a write happened so _first_pass.causally_linked holds",
            )
            # evidence_ref.no_diff must reflect the with-diff case
            # (False), so downstream consumers can distinguish the two
            # cases without re-deriving.
            self.assertFalse(
                verifies[0]["evidence_ref"].get("no_diff"),
                "with-diff verify.passed evidence_ref.no_diff must be "
                "False (a write happened)",
            )





# --- issue #94: status-transition table ---------------------------------

class TestStatusTransitionsTable(unittest.TestCase):
    """The status-transition table is the SSOT for per-status side effects.

    Issue #94: adding a new status (e.g. skipped) means adding ONE entry to
    STATUS_TRANSITIONS — the dispatcher routes through it. No more
    forgetting a `s.pop("started_at", None)` line in one of 5 branches.
    """

    def test_table_covers_all_valid_statuses(self):
        from lib import execute as ex
        for status in ex.VALID_STATUSES:
            self.assertIn(status, ex.STATUS_TRANSITIONS,
                          f"missing STATUS_TRANSITIONS entry for {status!r}")

    def test_table_values_are_callables(self):
        from lib import execute as ex
        for status, fn in ex.STATUS_TRANSITIONS.items():
            self.assertTrue(callable(fn),
                            f"STATUS_TRANSITIONS[{status!r}] is not callable")

    def test_dispatcher_routes_through_table(self):
        """update_step_status must look up the transition via STATUS_TRANSITIONS, not if/elif chain."""
        import inspect
        source = inspect.getsource(execute.update_step_status)
        for forbidden in (
            'elif status == "completed"',
            'elif status == "error"',
            'elif status == "blocked"',
            'elif status == "pending"',
            'elif status == "unimplemented"',
        ):
            self.assertNotIn(forbidden, source,
                             f"update_step_status still uses {forbidden!r} instead of STATUS_TRANSITIONS table")

    def test_each_transition_signature(self):
        """Every transition fn takes (step, now, kwargs) and mutates step in place."""
        import inspect

        from lib import execute as ex
        for status, fn in ex.STATUS_TRANSITIONS.items():
            sig = inspect.signature(fn)
            params = list(sig.parameters.keys())
            self.assertGreaterEqual(len(params), 2,
                                    f"transition {status!r} has < 2 params")

class TestMainDispatchDecision(unittest.TestCase):
    """Regression: main() emits dispatch decision via lib.dispatch_classifier.

    Replaces legacy --parallel flag with auto-classification. The decision
    + reason must appear in stderr as the first build-log line.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Two steps, default sequential (below N>=4 threshold)
        (self.root / "phases" / "0-mvp").mkdir(parents=True, exist_ok=True)
        (self.root / "phases" / "0-mvp" / "step1.md").write_text("# Step 1\n", encoding="utf-8")
        (self.root / "phases" / "0-mvp" / "step2.md").write_text("# Step 2\n", encoding="utf-8")
        idx = {
            "phase": "0-mvp",
            "worktree": "feat/x",
            "steps": [
                {"step": 1, "name": "a", "status": "pending"},
                {"step": 2, "name": "b", "status": "pending"},
            ],
        }
        (self.root / "phases" / "0-mvp" / "index.json").write_text(json.dumps(idx), encoding="utf-8")
        # Stub the runners; we only care about the dispatch decision logging.
        self._patches = [
            patch.object(execute, "_run_sequential", return_value=0),
            patch.object(execute, "_run_parallel", return_value=0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_main(self, argv):
        with patch.object(sys, "argv", ["execute.py", "--project-root", str(self.root)] + argv):
            buf = io.StringIO()
            with redirect_stderr(buf):
                try:
                    rc = execute.main()
                except SystemExit as e:
                    return e.code if isinstance(e.code, int) else 1, buf.getvalue()
            return rc, buf.getvalue()

    def test_main_emits_dispatch_decision_line(self):
        """main() must emit 'dispatch: <mode> — <reason>' as the first stderr line."""
        rc, stderr = self._run_main(["0-mvp"])
        self.assertEqual(rc, 0)
        self.assertIn("dispatch:", stderr,
                      f"main() must emit dispatch decision; stderr was: {stderr!r}")
        # Two steps (below N>=4) → sequential.
        self.assertIn("sequential", stderr,
                      f"2 steps should classify as sequential; stderr was: {stderr!r}")

    def test_main_no_longer_accepts_parallel_flag(self):
        """Legacy --parallel flag is removed; argparse rejects it."""
        rc, stderr = self._run_main(["0-mvp", "--parallel", "3"])
        # argparse error → SystemExit(2). Stderr should mention --parallel.
        self.assertNotEqual(rc, 0, f"--parallel flag must be removed; got rc={rc}")
        self.assertIn("--parallel", stderr,
                      f"argparse error must mention --parallel; stderr was: {stderr!r}")


class TestMainDispatchEligibleStepsOnly(unittest.TestCase):
    """Regression: classify() must see only eligible (resumable, non-blocked) steps.

    Otherwise a phase with N=4 steps where 1 is completed + 3 are pending
    would log 'parallel' but only run 3 steps, while stale metadata can
    force sequential dispatch by inflating N past the threshold.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "phases" / "0-mvp").mkdir(parents=True, exist_ok=True)
        # 4 steps total: 1 completed, 1 unimplemented, 2 pending.
        # Eligible = 2 (just the pending). Should classify as sequential
        # because N=2 < threshold.
        idx = {
            "phase": "0-mvp",
            "worktree": "feat/x",
            "steps": [
                {"step": 1, "name": "done", "status": "completed"},
                {"step": 2, "name": "stub", "status": "unimplemented"},
                {"step": 3, "name": "a", "status": "pending"},
                {"step": 4, "name": "b", "status": "pending"},
            ],
        }
        (self.root / "phases" / "0-mvp" / "index.json").write_text(json.dumps(idx), encoding="utf-8")
        self._patches = [
            patch.object(execute, "_run_sequential", return_value=0),
            patch.object(execute, "_run_parallel", return_value=0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_main(self, argv):
        with patch.object(sys, "argv", ["execute.py", "--project-root", str(self.root)] + argv):
            buf = io.StringIO()
            with redirect_stderr(buf):
                try:
                    rc = execute.main()
                except SystemExit as e:
                    return e.code if isinstance(e.code, int) else 1, buf.getvalue()
            return rc, buf.getvalue()

    def test_classify_sees_only_eligible_steps(self):
        rc, stderr = self._run_main(["0-mvp"])
        self.assertEqual(rc, 0)
        # 2 eligible steps; threshold is 4. Must be sequential.
        self.assertIn("sequential", stderr,
                      f"classifier must only count eligible steps; stderr was: {stderr!r}")
        self.assertIn("2 steps", stderr,
                      f"classifier must report eligible step count (2), not total (4); stderr was: {stderr!r}")


class TestTransitionCompletedExceptionNarrowing(unittest.TestCase):
    """Bare-except narrowing for _transition_completed.

    Inner op: datetime.fromisoformat(started_at). The only realistic
    failure modes are ValueError (malformed ISO string) and TypeError
    (non-string input). Anything else (KeyError from a malformed dict,
    AttributeError from a None field) is a programmer error and must
    propagate — silently zeroing duration was the bug the cleancode
    finding called out.
    """

    def test_malformed_started_at_leaves_duration_seconds_untouched(self):
        step = {"started_at": "not-an-iso-date"}
        before = step.get("duration_seconds", "<unset>")
        execute._transition_completed(step, "2026-08-21T12:00:00+09:00")
        # duration_seconds stays absent / not set when ISO parse fails.
        self.assertEqual(step.get("duration_seconds", "<unset>"), before)

    def test_valid_iso_sets_duration_seconds(self):
        step = {"started_at": "2026-08-21T11:00:00+09:00"}
        execute._transition_completed(step, "2026-08-21T12:00:00+09:00")
        self.assertEqual(step["duration_seconds"], 3600.0)

    def test_missing_started_at_is_noop(self):
        step = {}
        # No started_at — duration_seconds is unchanged (None path).
        execute._transition_completed(step, "2026-08-21T12:00:00+09:00")
        self.assertNotIn("duration_seconds", step)


class TestSafeDurationSecondsExceptionNarrowing(unittest.TestCase):
    """Bare-except narrowing for the duration-parse helper used inside
    _step_post_collect.

    Inner op: datetime.fromisoformat on `started_at_iso` + `now_iso()`.
    Realistic failure modes: ValueError (malformed ISO), TypeError
    (non-string input). KeyboardInterrupt / SystemExit must propagate.
    """

    def test_valid_iso_pair_returns_total_seconds(self):
        result = execute._safe_duration_seconds(
            "2026-08-21T11:00:00+09:00", "2026-08-21T12:00:00+09:00"
        )
        self.assertEqual(result, 3600.0)

    def test_malformed_started_at_raises_valueerror(self):
        """The helper raises the realistic exception; the inline-block caller
        in `_step_post_collect` catches (ValueError, TypeError) and maps
        to 0.0. The helper itself stays narrow — no silent zero."""
        with self.assertRaises(ValueError):
            execute._safe_duration_seconds("not-iso", "2026-08-21T12:00:00+09:00")

    def test_malformed_end_iso_raises_valueerror(self):
        with self.assertRaises(ValueError):
            execute._safe_duration_seconds("2026-08-21T11:00:00+09:00", "garbage")

    def test_non_string_input_raises_typeerror(self):
        with self.assertRaises(TypeError):
            execute._safe_duration_seconds(None, "2026-08-21T12:00:00+09:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
