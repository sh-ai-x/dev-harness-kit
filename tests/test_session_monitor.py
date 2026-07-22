#!/usr/bin/env python3
"""test_session_monitor.py — unit tests for the /dev-kit:session-monitor tool.

Covers the pure logic (status derivation, process attribution, agent-graph
builder, process/worktree mapping, resume-command builder, session
collection, picker row construction). The interactive picker and the
os.execvp resume hand-off require a real TTY and are verified manually
per the skill doc.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import session_monitor as sm  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "session_monitor"
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _agg(**kw):
    base = {"session_id": "s", "source": "claude-code", "worktree": "(main)",
            "branch": "main", "model": "m", "last_ts": NOW,
            "tool_counts": {}, "log_path": ""}
    base.update(kw)
    return base


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class TestDeriveStatus(unittest.TestCase):
    def test_recent_turn_is_live(self):
        agg = _agg(last_ts=NOW - timedelta(seconds=60))
        self.assertIs(sm.derive_status(agg, "main", NOW), sm.Status.LIVE)

    def test_old_turn_is_idle(self):
        agg = _agg(last_ts=NOW - timedelta(minutes=30))
        self.assertIs(sm.derive_status(agg, "main", NOW), sm.Status.IDLE)

    def test_merged_worktree_is_stale(self):
        agg = _agg(last_ts=NOW)  # recent, but worktree merged
        self.assertIs(sm.derive_status(agg, "merged", NOW), sm.Status.STALE)

    def test_gone_worktree_is_stale(self):
        self.assertIs(sm.derive_status(_agg(), "gone", NOW), sm.Status.STALE)

    def test_missing_last_ts_is_idle(self):
        self.assertIs(sm.derive_status(_agg(last_ts=None), "live", NOW),
                      sm.Status.IDLE)


class TestAttachLiveProcesses(unittest.TestCase):
    def _sess(self, sid, wt, ts, status):
        return sm.Session(agg=_agg(session_id=sid, worktree=wt, last_ts=ts),
                          worktree_state="live", status=status)

    def test_newest_nonstale_session_gets_process(self):
        old = self._sess("old", "wt", NOW - timedelta(hours=5), sm.Status.IDLE)
        new = self._sess("new", "wt", NOW - timedelta(hours=1), sm.Status.IDLE)
        sm.attach_live_processes([old, new], {"wt": [4242]})
        self.assertIs(new.status, sm.Status.LIVE)
        self.assertEqual(new.pids, [4242])
        self.assertIs(old.status, sm.Status.IDLE)
        self.assertEqual(old.pids, [])

    def test_stale_session_never_elevated(self):
        stale = self._sess("s", "wt", NOW, sm.Status.STALE)
        sm.attach_live_processes([stale], {"wt": [1]})
        self.assertIs(stale.status, sm.Status.STALE)
        self.assertEqual(stale.pids, [])

    def test_no_pids_no_change(self):
        s = self._sess("s", "wt", NOW, sm.Status.IDLE)
        sm.attach_live_processes([s], {"other": [1]})
        self.assertIs(s.status, sm.Status.IDLE)


class TestAgentGraph(unittest.TestCase):
    def test_cc_two_subagents(self):
        g = sm.build_agent_graph(FIXTURES / "cc-subagents.jsonl")
        self.assertEqual(len(g.nodes), 2)
        self.assertEqual(g.nodes[0].subagent_type, "Explore")
        self.assertEqual(g.nodes[0].description, "scan the API layer")
        self.assertEqual(g.nodes[0].turn_count, 2)   # s1a, s1b
        self.assertEqual(g.nodes[1].subagent_type, "Plan")
        self.assertEqual(g.nodes[1].turn_count, 3)   # s2a, s2b, s2c
        self.assertIn("two helpers", g.root_user_prompt)

    def test_codex_has_no_subagents(self):
        g = sm.build_agent_graph(FIXTURES / "codex-plain.jsonl")
        self.assertEqual(g.nodes, [])

    def test_missing_file_is_empty(self):
        g = sm.build_agent_graph(FIXTURES / "does-not-exist.jsonl")
        self.assertEqual(g.nodes, [])


class TestProcessDetection(unittest.TestCase):
    def test_is_cli_process(self):
        self.assertTrue(sm._is_cli_process("claude --dangerously-skip-permissions"))
        self.assertTrue(sm._is_cli_process("/usr/local/bin/codex resume abc"))
        self.assertFalse(sm._is_cli_process("/bin/zsh -c 'echo claude'"))
        self.assertFalse(sm._is_cli_process("Claude.app/Contents/MacOS/Claude"))

    def test_is_resume_process(self):
        self.assertTrue(sm._is_resume_process("claude -r"))
        self.assertTrue(sm._is_resume_process("claude --resume abc"))
        self.assertTrue(sm._is_resume_process("codex resume abc"))
        self.assertFalse(sm._is_resume_process("claude --print"))

    def test_list_cli_processes_filters(self):
        def runner(cmd, **kw):
            return FakeCompleted(
                "  100 claude --dangerously-skip-permissions\n"
                "  101 /bin/zsh -c something\n"
                "  102 codex resume xyz\n")
        procs = sm.list_cli_processes(runner=runner)
        pids = {p["pid"] for p in procs}
        self.assertEqual(pids, {100, 102})
        codex = next(p for p in procs if p["pid"] == 102)
        self.assertTrue(codex["is_resume"])

    def test_list_cli_processes_missing_binary(self):
        def runner(cmd, **kw):
            raise FileNotFoundError("ps")
        self.assertEqual(sm.list_cli_processes(runner=runner), [])

    def test_pid_cwd_parses_lsof(self):
        def runner(cmd, **kw):
            return FakeCompleted("p100\nfcwd\nn/repo/.worktrees/foo\n")
        self.assertEqual(sm.pid_cwd(100, runner=runner),
                         Path("/repo/.worktrees/foo"))

    def test_map_processes_drops_outside_repo(self):
        wt_paths = {"(main)": Path("/repo"),
                    "foo": Path("/repo/.worktrees/foo")}

        def runner(cmd, **kw):
            if cmd[0] == "lsof":
                pid = cmd[cmd.index("-p") + 1]
                mapping = {"1": "/repo/.worktrees/foo/src",
                           "2": "/somewhere/else"}
                return FakeCompleted(f"p{pid}\nfcwd\nn{mapping[pid]}\n")
            return FakeCompleted("")
        procs = [{"pid": 1, "command": "claude", "is_resume": False},
                 {"pid": 2, "command": "claude", "is_resume": False}]
        result = sm.map_processes_to_worktrees(procs, wt_paths, runner=runner)
        self.assertEqual(result, {"foo": [1]})  # pid 2 (other repo) dropped


class TestBuildResume(unittest.TestCase):
    def test_claude_argv_in_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            cwd, argv, warn = sm.build_resume(
                _agg(session_id="abc", source="claude-code"),
                Path("/repo"), wt)
            self.assertEqual(argv, ["claude", "--resume", "abc"])
            self.assertEqual(cwd, wt)
            self.assertIsNone(warn)

    def test_codex_argv(self):
        with tempfile.TemporaryDirectory() as d:
            cwd, argv, warn = sm.build_resume(
                _agg(session_id="xyz", source="codex"), Path("/repo"), Path(d))
            self.assertEqual(argv, ["codex", "resume", "xyz"])

    def test_gone_worktree_falls_back_to_main(self):
        cwd, argv, warn = sm.build_resume(
            _agg(session_id="abc", worktree="ghost"), Path("/repo"), None)
        self.assertEqual(cwd, Path("/repo"))
        self.assertIsNotNone(warn)
        self.assertIn("ghost", warn)


class TestCollectSessions(unittest.TestCase):
    def test_collects_cc_and_codex(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            (logs / "codex" / "main").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            shutil.copy(FIXTURES / "codex-plain.jsonl",
                        logs / "codex" / "main" / "019f-codex-plain.jsonl")
            aggs = sm.collect_sessions(root, logs, "", 3650)
            sources = {a["source"] for a in aggs}
            self.assertEqual(sources, {"claude-code", "codex"})
            self.assertEqual(len(aggs), 2)


class TestPrintResumeCommand(unittest.TestCase):
    """Dry-run path: --print-resume-command prints the cwd + argv that
    Enter would have exec'd, without entering the picker or calling exec.
    Lets CI verify the resume-argv synthesis + worktree resolution."""

    def _run(self, repo_root: Path, logs_dir: Path, days: int = 3650) -> str:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            original = sm.discover_repo_root
            sm.discover_repo_root = lambda *a, **kw: repo_root
            try:
                rc = sm.main([
                    "--logs-dir", str(logs_dir),
                    "--days", str(days),
                    "--print-resume-command",
                ])
            finally:
                sm.discover_repo_root = original
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_claude_session(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            out = self._run(root, logs)
        self.assertIn("cd ", out)
        self.assertIn("--resume", out)
        self.assertIn("cc-subagents", out)

    def test_codex_session(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "codex" / "main").mkdir(parents=True)
            shutil.copy(FIXTURES / "codex-plain.jsonl",
                        logs / "codex" / "main" / "019f-codex-plain.jsonl")
            out = self._run(root, logs)
        self.assertIn("codex", out)
        self.assertIn("resume", out)
        self.assertIn("019f-codex-plain", out)

    def test_no_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            import io
            from contextlib import redirect_stderr, redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                original = sm.discover_repo_root
                sm.discover_repo_root = lambda *a, **kw: root
                try:
                    rc = sm.main([
                        "--logs-dir", str(logs),
                        "--days", "3650",
                        "--print-resume-command",
                    ])
                finally:
                    sm.discover_repo_root = original
        self.assertEqual(rc, 0)
        self.assertIn("no sessions", buf.getvalue())


class TestPickerRows(unittest.TestCase):
    """Pure-logic tests for the inline-picker row builder + cursor movement."""

    def _sess(self, sid="s", wt="(main)", status=sm.Status.IDLE):
        return sm.Session(
            agg=_agg(session_id=sid, worktree=wt, last_ts=NOW),
            worktree_state="live", status=status)

    def _model(self):
        return [
            sm.WorktreeInfo("alpha", "live", None,
                            [self._sess("a1"), self._sess("a2")]),
            sm.WorktreeInfo("beta", "live", None,
                            [self._sess("b1")]),
        ]

    def test_build_rows_alternates_header_then_sessions(self):
        rows = sm.build_rows(self._model(), now=NOW)
        # Layout: section + (header + columns + N sessions) per worktree
        # Both worktrees are "live" so they collapse into a single section.
        self.assertEqual(len(rows), 1 + (1 + 1 + 2) + (1 + 1 + 1))
        self.assertEqual([r["kind"] for r in rows],
                         ["section", "header", "columns", "session", "session",
                          "header", "columns", "session"])
        self.assertIn("LIVE", rows[0]["text"])
        self.assertIn("alpha", rows[1]["text"])
        self.assertIn("BRANCH", rows[2]["text"])
        self.assertIn("COMMIT", rows[2]["text"])  # new column header
        self.assertEqual(rows[3]["session"].session_id, "a1")

    def test_build_rows_skips_agt_marker_when_no_subagents(self):
        rows = sm.build_rows(self._model(), now=NOW)
        for r in rows:
            if r["kind"] == "session":
                self.assertNotIn("+0agt", r["text"])
                self.assertNotIn("+1agt", r["text"])

    def test_selectable_indices_contains_only_sessions(self):
        rows = sm.build_rows(self._model(), now=NOW)
        idx = sm._selectable_indices(rows)
        self.assertEqual(idx, [3, 4, 7])
        for i in idx:
            self.assertEqual(rows[i]["kind"], "session")

    def test_move_selectable_never_lands_on_header(self):
        rows = sm.build_rows(self._model(), now=NOW)
        for start in (3, 4, 7):
            for delta in (-3, -1, +1, +5):
                moved = sm._move_selectable(rows, start, delta)
                self.assertEqual(rows[moved]["kind"], "session")

    def test_move_selectable_clamps_at_edges(self):
        rows = sm.build_rows(self._model(), now=NOW)
        self.assertEqual(sm._move_selectable(rows, 3, -10), 3)
        self.assertEqual(sm._move_selectable(rows, 7, +10), 7)

    def test_move_selectable_from_header_lands_on_nearest_session(self):
        rows = sm.build_rows(self._model(), now=NOW)
        # cursor on the "beta" header (row 5) moving down -> first beta session
        self.assertEqual(sm._move_selectable(rows, 5, +1), 7)
        # cursor on the "beta" header moving up -> last alpha session
        self.assertEqual(sm._move_selectable(rows, 5, -1), 4)

    def test_render_picker_writes_ansi_for_each_row(self):
        import io
        rows = sm.build_rows(self._model(), now=NOW)
        buf = io.StringIO()
        sm._render_picker(buf, rows, cursor=3, scroll=0, max_x=120, max_y=12)
        out = buf.getvalue()
        # header rendered, cursor row reverse-video'd
        self.assertIn("session-monitor", out)
        self.assertIn("BRANCH", out)  # column-label row present
        self.assertIn("COMMIT", out)  # new column
        self.assertIn("\x1b[7m", out)  # reverse video on cursor row
        self.assertIn("\x1b[2m", out)  # dim worktree header + columns row
        # 8 visible rows + body_h=10 => 2 padding clear-line to pin the footer
        self.assertEqual(out.count("\x1b[K"), 2)
        self.assertGreaterEqual(out.count("\n"), 8)

    def test_render_picker_no_padding_when_body_filled(self):
        import io
        rows = sm.build_rows(self._model(), now=NOW)
        buf = io.StringIO()
        # body_h = max_y - 2 = 8, exactly fits the 8 visible rows -> no padding
        sm._render_picker(buf, rows, cursor=1, scroll=0, max_x=120, max_y=10)
        out = buf.getvalue()
        self.assertNotIn("\x1b[K", out)


class TestEnrichBranches(unittest.TestCase):
    """Branch enrichment: override the log-captured branch with the
    worktree's current ``git rev-parse --abbrev-ref HEAD``. Verified with a
    real git repo in a tempdir (no mocking) so subprocess / git edge cases
    are exercised."""

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args],
                       check=True, capture_output=True, text=True)

    def _init_repo(self, root: Path, branch: str) -> None:
        self._git(root, "init", "-q", "-b", branch)
        self._git(root, "config", "user.email", "x@example.com")
        self._git(root, "config", "user.name", "x")
        (root / "f").write_text("x")
        self._git(root, "add", ".")
        self._git(root, "commit", "-q", "-m", "i")

    def test_enrich_overrides_logged_branch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root, "feat-x")
            # log says "main" (stale), worktree is actually on feat-x
            sess = sm.Session(
                agg=_agg(branch="main"),
                worktree_state="live", status=sm.Status.IDLE, wt_path=root,
            )
            sm._enrich_branches_from_worktrees([sess], runner=subprocess.run)
            self.assertEqual(sess.branch, "feat-x")

    def test_enrich_skips_stale_worktrees(self):
        sess = sm.Session(
            agg=_agg(branch="main"),
            worktree_state="merged", status=sm.Status.STALE,
        )
        sm._enrich_branches_from_worktrees([sess], runner=subprocess.run)
        self.assertEqual(sess.branch, "main")  # log branch preserved

    def test_enrich_skips_missing_wt_path(self):
        sess = sm.Session(
            agg=_agg(branch="main"),
            worktree_state="live", status=sm.Status.IDLE, wt_path=None,
        )
        sm._enrich_branches_from_worktrees([sess], runner=subprocess.run)
        self.assertEqual(sess.branch, "main")

    def test_enrich_keeps_log_on_non_git_path(self):
        with tempfile.TemporaryDirectory() as d:
            sess = sm.Session(
                agg=_agg(branch="main"),
                worktree_state="live", status=sm.Status.IDLE,
                wt_path=Path(d),  # not a git repo
            )
            sm._enrich_branches_from_worktrees([sess], runner=subprocess.run)
            self.assertEqual(sess.branch, "main")

    def test_enrich_skips_detached_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root, "feat-x")
            self._git(root, "checkout", "--quiet", "--detach")
            sess = sm.Session(
                agg=_agg(branch="main"),
                worktree_state="live", status=sm.Status.IDLE, wt_path=root,
            )
            sm._enrich_branches_from_worktrees([sess], runner=subprocess.run)
            # detached -> "HEAD" sentinel -> log branch preserved
            self.assertEqual(sess.branch, "main")


class TestPrintJson(unittest.TestCase):
    """--json emits the AskUserQuestion-flow contract: stable top-level keys
    + full session_id + worktree abs path so the skill can synthesize the
    resume command without re-running the tool."""

    def _run(self, repo_root: Path, logs_dir: Path) -> dict:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            original = sm.discover_repo_root
            sm.discover_repo_root = lambda *a, **kw: repo_root
            try:
                rc = sm.main([
                    "--logs-dir", str(logs_dir),
                    "--days", "3650",
                    "--json",
                ])
            finally:
                sm.discover_repo_root = original
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_claude_session_full_id_and_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            payload = self._run(root, logs)
        self.assertTrue(
            {"logs_dir", "generated_at", "total_sessions",
             "live_sessions", "worktrees",
             "skill_usage_total"}.issubset(payload.keys()),
            f"missing expected keys: {[k for k in ('logs_dir', 'generated_at', 'total_sessions', 'live_sessions', 'worktrees', 'skill_usage_total') if k not in payload]}; got {sorted(payload.keys())}")
        self.assertEqual(payload["total_sessions"], 1)
        # Without a real worktree in the tempdir, worktree_from_path falls
        # back to the (main) sentinel. The exact mapping is verified by
        # the live runtime; here we just confirm the JSON shape is stable.
        wt = payload["worktrees"][0]
        self.assertEqual(wt["name"], "(main)")
        self.assertTrue(wt["sessions"])
        sess = wt["sessions"][0]
        self.assertEqual(sess["source"], "claude-code")
        # fixture's last_ts is ~2h before NOW, so it falls outside the
        # 180s LIVE window and lands as IDLE. Status transitions are
        # covered by TestDeriveStatus; here we only assert shape.
        self.assertEqual(sess["status"], "idle")
        self.assertIn("-", sess["session_id"])  # full UUID, not truncated
        self.assertTrue(sess["log_path"].endswith("cc-subagents.jsonl"))

    def test_codex_session_status_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "codex" / "main").mkdir(parents=True)
            shutil.copy(FIXTURES / "codex-plain.jsonl",
                        logs / "codex" / "main" / "019f-codex-plain.jsonl")
            payload = self._run(root, logs)
        sess = payload["worktrees"][0]["sessions"][0]
        self.assertEqual(sess["source"], "codex")
        self.assertEqual(sess["status"], "idle")  # fixture is older than window
        self.assertTrue(sess["last_rel"].endswith("ago"))

    def test_empty_logs_emits_zero_totals(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            payload = self._run(root, logs)
        self.assertEqual(payload["total_sessions"], 0)
        self.assertEqual(payload["live_sessions"], 0)
        self.assertEqual(payload["worktrees"], [])


class TestCliAliasSetup(unittest.TestCase):
    """--cli-setup managed-block renderer + installer."""

    def test_shell_rc_picks_zshrc(self):
        rc = sm._shell_rc({"SHELL": "/bin/zsh"})
        self.assertEqual(rc.name, ".zshrc")

    def test_shell_rc_picks_bashrc(self):
        rc = sm._shell_rc({"SHELL": "/usr/bin/bash"})
        self.assertEqual(rc.name, ".bashrc")

    def test_shell_rc_falls_back_to_profile(self):
        rc = sm._shell_rc({"SHELL": "/usr/bin/fish"})
        self.assertEqual(rc.name, ".profile")

    def test_render_rc_appends_block_to_existing(self):
        block = sm._alias_block(Path("/repo/tools/session_monitor.py"), "python3")
        out = sm._render_rc("export FOO=1\n", block)
        self.assertIn("export FOO=1", out)
        self.assertIn("alias session-monitor='python3 "
                      "/repo/tools/session_monitor.py'", out)
        self.assertTrue(out.endswith("\n"))

    def test_render_rc_is_idempotent(self):
        block = sm._alias_block(Path("/repo/tools/session_monitor.py"), "python3")
        once = sm._render_rc("export FOO=1\n", block)
        twice = sm._render_rc(once, block)
        self.assertEqual(once, twice)

    def test_render_rc_replaces_stale_block(self):
        old = sm._alias_block(Path("/old/session_monitor.py"), "python2")
        new = sm._alias_block(Path("/repo/tools/session_monitor.py"), "python3")
        rc_text = sm._render_rc("export FOO=1\n", old)
        out = sm._render_rc(rc_text, new)
        self.assertNotIn("python2", out)
        self.assertNotIn("/old/session_monitor.py", out)
        self.assertEqual(out.count(sm._CLI_BEGIN), 1)

    def test_render_rc_on_empty_file(self):
        block = sm._alias_block(Path("/repo/tools/session_monitor.py"), "python3")
        out = sm._render_rc("", block)
        self.assertTrue(out.startswith(sm._CLI_BEGIN))
        self.assertTrue(out.endswith("\n"))

    def test_install_writes_and_refreshes(self):
        with tempfile.TemporaryDirectory() as d:
            rc = Path(d) / ".zshrc"
            rc.write_text("export FOO=1\n")
            script = Path("/repo/tools/session_monitor.py")
            rc_ = sm.install_cli_alias(script_path=script, python_exe="python3",
                                       rc=rc)
            self.assertEqual(rc_, 0)
            first = rc.read_text()
            self.assertIn("alias session-monitor=", first)
            # second run must not duplicate the managed block
            sm.install_cli_alias(script_path=script, python_exe="python3", rc=rc)
            self.assertEqual(rc.read_text().count(sm._CLI_BEGIN), 1)

    def test_install_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            rc = Path(d) / ".zshrc"
            rc.write_text("export FOO=1\n")
            sm.install_cli_alias(script_path=Path("/repo/x.py"),
                                 python_exe="python3", rc=rc, dry_run=True)
            self.assertEqual(rc.read_text(), "export FOO=1\n")


class TestGetLastCommitSubject(unittest.TestCase):
    """Per-worktree `git log -1 --pretty=%s` subject resolver.

    Returns ``None`` on any git failure (no git, no commits, non-repo dir,
    subprocess error) so the listing never crashes. Real subprocess runs
    against a tempdir repo (no mocking) so git edge cases are covered.
    """

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args],
                       check=True, capture_output=True, text=True)

    def _init_repo(self, root: Path, subject: str) -> None:
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "x@example.com")
        self._git(root, "config", "user.name", "x")
        (root / "f").write_text("x")
        self._git(root, "add", ".")
        self._git(root, "commit", "-q", "-m", subject)

    def test_returns_subject_for_real_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root, "feat: hello world")
            self.assertEqual(sm.get_last_commit_subject(root),
                             "feat: hello world")

    def test_returns_latest_subject_after_multiple_commits(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.email", "x@example.com")
            self._git(root, "config", "user.name", "x")
            (root / "f").write_text("x")
            self._git(root, "add", ".")
            self._git(root, "commit", "-q", "-m", "first")
            self._git(root, "commit", "-q", "--allow-empty", "-m", "second")
            self._git(root, "commit", "-q", "--allow-empty", "-m", "third")
            self.assertEqual(sm.get_last_commit_subject(root), "third")

    def test_returns_none_for_non_git_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sm.get_last_commit_subject(Path(d)))

    def test_returns_none_for_missing_dir(self):
        self.assertIsNone(sm.get_last_commit_subject(Path("/no/such/path")))

    def test_subject_with_special_chars_round_trips(self):
        # %n (newline), %' (apostrophe), unicode — the parser must not
        # truncate or strip.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root, "fix: 'quoted' subject with émoji 🚀")
            subj = sm.get_last_commit_subject(root)
            self.assertIsNotNone(subj)
            self.assertIn("fix:", subj)
            self.assertIn("quoted", subj)
            self.assertIn("🚀", subj)


class TestFilterModel(unittest.TestCase):
    """Substring search across session fields.

    Case-insensitive. Empty pattern = identity. Matches against
    session_id, branch, model, source, log_path, worktree, status.
    A WorktreeInfo is dropped if all of its sessions are filtered out.
    """

    def _sess(self, sid="s1", wt="(main)", branch="main", model="opus",
              source="claude-code", status=sm.Status.IDLE,
              log_path="/tmp/x.jsonl"):
        return sm.Session(
            agg=_agg(session_id=sid, worktree=wt, branch=branch,
                     model=model, source=source, log_path=log_path,
                     last_ts=NOW),
            worktree_state="live", status=status)

    def _model(self):
        return [
            sm.WorktreeInfo("alpha", "live", None, [
                self._sess(sid="aaaa1111", branch="feat-x", model="opus"),
                self._sess(sid="aaaa2222", branch="main", model="haiku"),
            ]),
            sm.WorktreeInfo("beta", "live", None, [
                self._sess(sid="bbbb1111", branch="feat-y", model="sonnet",
                           source="codex"),
            ]),
            sm.WorktreeInfo("gamma", "merged", None, [
                self._sess(sid="cccc1111", branch="feat-z", model="opus",
                           status=sm.Status.STALE),
            ]),
        ]

    def test_empty_pattern_keeps_all(self):
        out = sm.filter_model(self._model(), "")
        self.assertEqual(len(out), 3)
        self.assertEqual(sum(len(w.sessions) for w in out), 4)

    def test_substring_matches_branch(self):
        out = sm.filter_model(self._model(), "feat-y")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dirname, "beta")
        self.assertEqual(len(out[0].sessions), 1)

    def test_case_insensitive(self):
        out = sm.filter_model(self._model(), "FEAT-X")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sessions[0].branch, "feat-x")

    def test_matches_session_id(self):
        out = sm.filter_model(self._model(), "2222")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sessions[0].session_id, "aaaa2222")

    def test_matches_model_field(self):
        out = sm.filter_model(self._model(), "haiku")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sessions[0].model, "haiku")

    def test_matches_source(self):
        out = sm.filter_model(self._model(), "codex")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sessions[0].source, "codex")

    def test_matches_worktree_name(self):
        out = sm.filter_model(self._model(), "gamma")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dirname, "gamma")

    def test_matches_status(self):
        out = sm.filter_model(self._model(), "stale")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sessions[0].status, sm.Status.STALE)

    def test_no_match_drops_all(self):
        self.assertEqual(sm.filter_model(self._model(), "zzz-nope"), [])

    def test_partial_match_inside_worktree(self):
        # "alpha" has 2 sessions, only one matches branch=feat-x.
        # The matching one survives; the worktree bucket stays open.
        out = sm.filter_model(self._model(), "main")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dirname, "alpha")
        self.assertEqual(len(out[0].sessions), 1)
        self.assertEqual(out[0].sessions[0].branch, "main")


class TestGroupByState(unittest.TestCase):
    """Group worktrees into LIVE / MERGED / GONE / UNKNOWN sections.

    Sections appear in this fixed order regardless of input order:
    live -> merged -> gone -> unknown. Within a section the input
    ordering is preserved (group_by_worktree already sorts by
    recency, so this composes).
    """

    def _wt(self, name: str, state: str) -> sm.WorktreeInfo:
        return sm.WorktreeInfo(name, state, None, [])

    def test_live_first(self):
        model = [self._wt("gone1", "gone"), self._wt("live1", "live"),
                 self._wt("merged1", "merged")]
        sections = sm.group_by_state(model)
        self.assertEqual([s[0] for s in sections], ["live", "merged", "gone"])
        self.assertEqual([s[1][0].dirname for s in sections],
                         ["live1", "merged1", "gone1"])

    def test_empty_buckets_are_skipped(self):
        model = [self._wt("live1", "live")]
        sections = sm.group_by_state(model)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0], "live")

    def test_unknown_state_bucketed(self):
        model = [self._wt("u", "unknown"), self._wt("l", "live")]
        sections = sm.group_by_state(model)
        self.assertEqual([s[0] for s in sections], ["live", "unknown"])

    def test_section_counts(self):
        model = [self._wt("a", "live"), self._wt("b", "live"),
                 self._wt("c", "merged")]
        sections = sm.group_by_state(model)
        live = next(s for s in sections if s[0] == "live")
        self.assertEqual(len(live[1]), 2)


class TestAttachLastCommit(unittest.TestCase):
    """``attach_last_commit_subjects`` mutates each WorktreeInfo in place,
    setting ``last_commit_subject`` from a real git repo per worktree dir.
    Non-git / missing dirs leave the field None.
    """

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args],
                       check=True, capture_output=True, text=True)

    def _init_repo(self, root: Path, subject: str) -> None:
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "x@example.com")
        self._git(root, "config", "user.name", "x")
        (root / "f").write_text("x")
        self._git(root, "add", ".")
        self._git(root, "commit", "-q", "-m", subject)

    def test_attaches_subject_for_real_wt(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            self._init_repo(wt, "feat: latest commit")
            info = sm.WorktreeInfo("alpha", "live", wt, [])
            sm.attach_last_commit_subjects([info])
            self.assertEqual(info.last_commit_subject, "feat: latest commit")

    def test_attaches_none_for_missing_path(self):
        info = sm.WorktreeInfo("alpha", "live", Path("/no/such"), [])
        sm.attach_last_commit_subjects([info])
        self.assertIsNone(info.last_commit_subject)

    def test_attaches_none_for_non_git_path(self):
        with tempfile.TemporaryDirectory() as d:
            info = sm.WorktreeInfo("alpha", "live", Path(d), [])
            sm.attach_last_commit_subjects([info])
            self.assertIsNone(info.last_commit_subject)


class TestPrintJsonIncludesCommit(unittest.TestCase):
    """--json output surfaces ``last_commit_subject`` on each worktree so
    downstream consumers (the skill's AskUserQuestion flow) can show it."""

    def _run(self, repo_root: Path, logs_dir: Path) -> dict:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            original = sm.discover_repo_root
            sm.discover_repo_root = lambda *a, **kw: repo_root
            try:
                rc = sm.main([
                    "--logs-dir", str(logs_dir),
                    "--days", "3650",
                    "--json",
                ])
            finally:
                sm.discover_repo_root = original
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_worktree_has_last_commit_key(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            payload = self._run(root, logs)
        wt = payload["worktrees"][0]
        self.assertIn("last_commit_subject", wt)


class TestSearchFlag(unittest.TestCase):
    """--filter substring search applied at the CLI layer."""

    def _run(self, repo_root: Path, logs_dir: Path,
             pattern: str) -> dict:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            original = sm.discover_repo_root
            sm.discover_repo_root = lambda *a, **kw: repo_root
            try:
                argv = ["--logs-dir", str(logs_dir),
                        "--days", "3650",
                        "--json"]
                if pattern:
                    argv += ["--filter", pattern]
                rc = sm.main(argv)
            finally:
                sm.discover_repo_root = original
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_filter_narrows_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            (logs / "codex" / "feat-y").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            shutil.copy(FIXTURES / "codex-plain.jsonl",
                        logs / "codex" / "feat-y" / "019f-codex-plain.jsonl")
            full = self._run(root, logs, "")
            payload = self._run(root, logs, "codex")
        self.assertGreater(full["total_sessions"], 0)
        # Filter "codex" matches source and log_path substring; the
        # codex-only fixture survives while the claude-code one is dropped.
        self.assertLess(payload["total_sessions"], full["total_sessions"])
        for w in payload["worktrees"]:
            for s in w["sessions"]:
                self.assertIn("codex", s["log_path"].lower())

    def test_filter_no_match_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            payload = self._run(root, logs, "zzz-no-match")
        self.assertEqual(payload["total_sessions"], 0)


class TestSkillUsageFlag(unittest.TestCase):
    """Smoke test for ``--skill-usage``: confirms the flag is wired into
    ``main`` without breaking existing flows, and the additive JSON key
    carries the aggregate when the flag is on.
    """

    def _run(self, root: Path, logs: Path, *extra: str) -> tuple[int, str]:
        original = sm.discover_repo_root
        sm.discover_repo_root = lambda: root
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = sm.main(["--logs-dir", str(logs), "--days", "3650",
                              "--json", *extra])
        finally:
            sm.discover_repo_root = original
        return rc, buf.getvalue()

    def test_default_no_skill_usage_key(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            rc, out = self._run(root, logs)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        # Additive-only contract: with no --skill-usage flag, the new key
        # is present but empty so downstream consumers can rely on its
        # existence without an extra ``if``.
        self.assertIn("skill_usage_total", payload)
        self.assertEqual(payload["skill_usage_total"], {})

    def test_skill_usage_with_empty_logs_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            (logs / "claude-code" / "feat-x").mkdir(parents=True)
            shutil.copy(FIXTURES / "cc-subagents.jsonl",
                        logs / "claude-code" / "feat-x" / "cc-subagents.jsonl")
            rc, out = self._run(root, logs, "--skill-usage", "--skill-days", "1")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        # Fixture has no attributionSkill -> aggregate is empty.
        self.assertEqual(payload["skill_usage_total"], {})


class TestDecodeTranscript(unittest.TestCase):
    """Pure-decoder split: `_decode_transcript(path)` yields parsed
    records from a jsonl session log with narrow exception handling.

    The split separates transcript IO/JSON decoding (this function)
    from spawn/sidechain correlation (the rest of build_agent_graph),
    so each layer can be tested in isolation. Malformed lines and
    IO failures are skipped narrowly: only `OSError` (file open /
    read) and `json.JSONDecodeError` (parse) are swallowed. Anything
    else (e.g. ``AttributeError`` from a bug upstream) propagates."""

    def _write(self, lines):
        fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        fh.write("\n".join(lines) + "\n")
        fh.close()
        self.addCleanup(lambda: Path(fh.name).unlink(missing_ok=True))
        return Path(fh.name)

    def test_yields_each_parseable_record(self):
        p = self._write([
            json.dumps({"type": "user", "uuid": "u1"}),
            json.dumps({"type": "assistant", "uuid": "a1"}),
        ])
        out = list(sm._decode_transcript(p))
        self.assertEqual([r["uuid"] for r in out], ["u1", "a1"])

    def test_skips_blank_lines(self):
        p = self._write(["", json.dumps({"type": "user", "uuid": "u1"}), ""])
        out = list(sm._decode_transcript(p))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["uuid"], "u1")

    def test_skips_malformed_json_without_crashing(self):
        p = self._write([
            "not json",
            json.dumps({"type": "user", "uuid": "u1"}),
            "{broken",
        ])
        out = list(sm._decode_transcript(p))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["uuid"], "u1")

    def test_missing_file_yields_empty(self):
        out = list(sm._decode_transcript(FIXTURES / "no-such-file.jsonl"))
        self.assertEqual(out, [])

    def test_build_agent_graph_still_matches_fixture(self):
        """End-to-end: refactor preserves the existing fixture contract."""
        g = sm.build_agent_graph(FIXTURES / "cc-subagents.jsonl")
        self.assertEqual(len(g.nodes), 2)
        self.assertEqual(g.nodes[0].subagent_type, "Explore")
        self.assertEqual(g.nodes[1].subagent_type, "Plan")


class TestCorrelateNodesToChains(unittest.TestCase):
    """Pure correlation step: pair spawn nodes to sidechain chains by
    encounter order. Each spawn edge attaches the matching chain's
    turn_count + last_ts. Orphan chains (no matching spawn) are dropped;
    orphan spawns (no matching chain) keep turn_count=0."""

    def _node(self, tool_use_id, subagent_type="Explore"):
        return sm.AgentNode(tool_use_id=tool_use_id,
                            subagent_type=subagent_type, description="",
                            prompt_excerpt="")

    def test_pairs_in_order(self):
        nodes = [self._node("n1"), self._node("n2")]
        chains = [
            ("c1", {"turns": 2, "last_ts": NOW}),
            ("c2", {"turns": 3, "last_ts": NOW}),
        ]
        sm._correlate_nodes_to_chains(nodes, chains)
        self.assertEqual(nodes[0].turn_count, 2)
        self.assertEqual(nodes[1].turn_count, 3)

    def test_orphan_chain_dropped(self):
        nodes = [self._node("n1")]
        chains = [
            ("c1", {"turns": 2, "last_ts": NOW}),
            ("c2", {"turns": 9, "last_ts": NOW}),
        ]
        sm._correlate_nodes_to_chains(nodes, chains)
        self.assertEqual(nodes[0].turn_count, 2)
        self.assertEqual(len(nodes), 1)

    def test_orphan_spawn_keeps_zero_turn_count(self):
        nodes = [self._node("n1"), self._node("n2")]
        chains = [("c1", {"turns": 5, "last_ts": NOW})]
        sm._correlate_nodes_to_chains(nodes, chains)
        self.assertEqual(nodes[0].turn_count, 5)
        self.assertEqual(nodes[1].turn_count, 0)


class TestConcurrentSpawnCorrelation(unittest.TestCase):
    """Concurrent subagent spawns reorder sidechain records in the wire
    log so the encounter index no longer maps spawn N to chain N. The
    correlation step must match each spawn to the chain whose head text
    names that spawn, not the chain that happens to arrive Nth.

    Reproducer: parent transcript spawns Explore + Plan as two parallel
    ``Agent`` ``tool_use`` blocks. The two sidechains then interleave --
    Plan's head (``s2a``) lands first, then Explore's head (``s1a``),
    then alternating child records. The legacy position-by-index
    correlation would assign the 3-turn Plan chain to the Explore node
    and the 2-turn Explore chain to the Plan node (turn counts swap).
    """

    def _write_log(self, lines):
        fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        fh.write("\n".join(lines) + "\n")
        fh.close()
        self.addCleanup(lambda: Path(fh.name).unlink(missing_ok=True))
        return Path(fh.name)

    def test_concurrent_spawn_keeps_per_agent_turn_counts(self):
        records = [
            # parent prompt
            {"type": "user", "isSidechain": False, "uuid": "u1",
             "message": {"role": "user",
                         "content": [{"type": "text",
                                      "text": "build the thing with two helpers"}]}},
            # parent assistant: spawns Explore + Plan in encounter order
            {"type": "assistant", "isSidechain": False, "uuid": "a1",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "call_A", "name": "Agent",
                  "input": {"subagent_type": "Explore",
                            "description": "scan the API layer",
                            "prompt": "look at all handlers"}},
                 {"type": "tool_use", "id": "call_B", "name": "Agent",
                  "input": {"subagent_type": "Plan",
                            "description": "design the migration",
                            "prompt": "produce a step plan"}},
             ]}},
            # sidechains INTERLEAVED (concurrent spawns)
            {"type": "user", "isSidechain": True, "uuid": "s2a",
             "parentUuid": None,
             "message": {"role": "user",
                         "content": [{"type": "text",
                                      "text": "design the migration"}]}},
            {"type": "user", "isSidechain": True, "uuid": "s1a",
             "parentUuid": None,
             "message": {"role": "user",
                         "content": [{"type": "text",
                                      "text": "scan the API layer"}]}},
            {"type": "assistant", "isSidechain": True, "uuid": "s2b",
             "parentUuid": "s2a",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "step 1..."}]}},
            {"type": "assistant", "isSidechain": True, "uuid": "s1b",
             "parentUuid": "s1a",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "found 3 handlers"}]}},
            {"type": "assistant", "isSidechain": True, "uuid": "s2c",
             "parentUuid": "s2b",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "step 2..."}]}},
        ]
        path = self._write_log([json.dumps(r) for r in records])
        g = sm.build_agent_graph(path)
        self.assertEqual(len(g.nodes), 2)
        # Order in g.nodes matches encounter order in the parent transcript
        # (Explore first, Plan second). Each node must keep its OWN turn
        # count, not the count of whichever chain happens to be at that
        # index in the file.
        by_desc = {n.description: n for n in g.nodes}
        self.assertEqual(by_desc["scan the API layer"].turn_count, 2)
        self.assertEqual(by_desc["design the migration"].turn_count, 3)


class TestModeValidation(unittest.TestCase):
    """main() rejects conflicting mode flags with a clear error and
    exit code 2. The legacy precedence path silently picked one
    (--list before --json before --print-resume-command); the explicit
    validation makes the conflict visible so callers can fix the
    invocation. Single-mode behavior is unchanged."""

    def _run(self, *argv):
        original = sm.discover_repo_root
        sm.discover_repo_root = lambda *a, **kw: Path("/repo")
        out, err = io.StringIO(), io.StringIO()
        rc = None
        try:
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    rc = sm.main(["--logs-dir", "/tmp/nope", *argv])
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        finally:
            sm.discover_repo_root = original
        return rc, out.getvalue(), err.getvalue()

    def test_no_mode_does_not_conflict(self):
        rc, _, err = self._run("--days", "1")
        self.assertNotEqual(rc, 2)
        self.assertNotIn("conflicting mode", err.lower())

    def test_single_mode_still_works(self):
        rc, out, _ = self._run("--list")
        self.assertEqual(rc, 0)
        self.assertIn("no sessions", out)

    def test_list_plus_json_conflict(self):
        rc, _, err = self._run("--list", "--json")
        self.assertEqual(rc, 2)
        self.assertIn("conflicting mode", err.lower())
        self.assertIn("--list", err)
        self.assertIn("--json", err)

    def test_json_plus_print_resume_command_conflict(self):
        rc, _, err = self._run("--json", "--print-resume-command")
        self.assertEqual(rc, 2)
        self.assertIn("conflicting mode", err.lower())

    def test_three_modes_conflict(self):
        rc, _, err = self._run("--list", "--json", "--print-resume-command")
        self.assertEqual(rc, 2)
        self.assertIn("conflicting mode", err.lower())

    def test_cli_setup_short_circuits_without_conflict(self):
        # --cli-setup is mutually exclusive with data modes but handled
        # by its own short-circuit; verify the conflict detector does not
        # fire when only --cli-setup is set alongside a non-conflicting
        # helper flag like --dry-run.
        rc, _, _ = self._run("--cli-setup", "--dry-run")
        self.assertNotEqual(rc, 2)

    def test_resume_picker_setup_are_mutually_exclusive(self):
        """Operator-mode family: --print-resume-command / --picker /
        --cli-setup each route the program to a distinct handler.
        argparse must reject any two as a usage error (exit 2) rather
        than letting one silently override the other."""
        for combo in (("--print-resume-command", "--picker"),
                      ("--print-resume-command", "--cli-setup"),
                      ("--picker", "--cli-setup")):
            with self.subTest(combo=combo):
                rc, _, err = self._run(*combo)
                self.assertEqual(rc, 2,
                                 f"{combo} should reject with exit 2; "
                                 f"got rc={rc}, stderr={err!r}")


class TestScriptEntrypoint(unittest.TestCase):
    """Smoke-test the documented `python3 tools/session_monitor.py` invocation.

    The earlier sibling-module split introduced an absolute back-edge
    (``from session_monitor import ...``) that fired only when the file was
    loaded as ``__main__`` -- no top-level ``session_monitor`` module yet,
    so the sibling import started a second copy of the parent and
    re-entered before argparse ran. The existing test suite masked this
    because ``tests/test_session_monitor.py`` does
    ``sys.path.insert(0, tools/)`` and imports the module under the
    ``session_monitor`` name, which short-circuits the cycle.

    These tests execute the script as a subprocess (no sys.path tricks,
    no test-suite import aliasing) so the load graph is identical to what
    a user sees when they type ``python3 tools/session_monitor.py --help``
    on the command line.
    """

    SCRIPT = PROJECT_ROOT / "tools" / "session_monitor.py"

    def _run_script(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True, text=True, timeout=timeout,
        )

    def test_help_exits_zero(self) -> None:
        """`python3 tools/session_monitor.py --help` parses + exits 0."""
        cp = self._run_script("--help")
        self.assertEqual(cp.returncode, 0,
                         f"--help should exit 0; got {cp.returncode}\n"
                         f"stderr: {cp.stderr}\nstdout: {cp.stdout[:500]}")
        # argparse help output mentions the program name and a couple of
        # the documented flags; assert both so a regression in either the
        # parser wiring or the import order surfaces here.
        self.assertIn("session_monitor.py", cp.stdout)
        self.assertIn("--picker", cp.stdout)
        self.assertIn("--print-resume-command", cp.stdout)

    def test_imports_do_not_recurse(self) -> None:
        """Subprocess import-count guard: a clean `--help` only loads
        ``session_monitor`` once, even though four siblings reference it.

        The regression that prompted the cycle fix was a second copy of
        ``tools/session_monitor.py`` triggered by sibling imports during
        the first copy's load. Counting subprocess invocations inside the
        child is awkward, so this test instead asserts the simpler signal
        that argparse succeeds (which only happens if no ImportError
        fired mid-load) and that the script doesn't print to stderr.
        """
        cp = self._run_script("--help")
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stderr, "",
                         f"clean --help must not write to stderr; got: {cp.stderr}")


if __name__ == "__main__":
    unittest.main()
