"""Tests for ``tools/save_log.py`` --archive-stale flag.

Pins the worktree-active detection that drives the stale-archive
write path (Fix C from the 2026-08-11 /dev-kit:token-analyzer
diagnostic, GitHub #624).

Behavior under test:
  * ``_is_worktree_active(cwd, main_root)`` returns True when cwd is
    listed in ``git worktree list --porcelain``.
  * Returns False when the cwd worktree has been removed.
  * Defaults to True (active) when git is missing or the porcelain
    call fails, so a degraded environment never silently drops logs.
  * ``main()`` routes the worktree-side write to
    ``logs/.archive/<branch>/<ts>/`` when ``--archive-stale`` is set
    AND the worktree is no longer active.
  * Without the flag, behavior is unchanged (regression guard).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent
TOOL_PY = PROJECT_ROOT / "tools" / "save_log.py"
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import save_log  # noqa: E402


def _invoke_save_log(argv: list[str], stdin_payload: str,
                    main_root: Path, wt_dir: Path, branch: str,
                    porcelain: str) -> int:
    """Run ``save_log.main()`` in-process with mocked git + patched stdin.

    Subprocess invocation would need a PATH-shadowing fake git; the
    in-process variant patches ``save_log.subprocess.run`` with a
    route() that answers the script's git calls deterministically.
    """
    route = _make_route(porcelain, main_root, wt_dir, branch)
    old_argv = sys.argv
    old_stdin = sys.stdin
    sys.argv = ["save_log.py", *argv]
    sys.stdin = io.StringIO(stdin_payload)
    try:
        with mock.patch.object(save_log.subprocess, "run", side_effect=route):
            return save_log.main()
    finally:
        sys.argv = old_argv
        sys.stdin = old_stdin


def _raw_jsonl(records: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _fake_payload(*, transcript: str, cwd: str, session_id: str) -> str:
    return json.dumps(
        {
            "transcript_path": transcript,
            "cwd": cwd,
            "session_id": session_id,
        }
    )


def _make_route(porcelain_output: str, main_root: Path, wt_dir: Path, branch: str):
    """Return a subprocess.run side-effect for the script's git calls.

    Handles: rev-parse (--git-common-dir / --git-dir / --show-toplevel /
    --abbrev-ref HEAD), symbolic-ref, worktree list. Falls through
    to a non-zero CompletedProcess for anything else (so
    _is_worktree_active's `out.returncode != 0` branch exercises
    without recursing into the patched mock).
    """
    porcelain = porcelain_output

    def _route(cmd, *args, **kwargs):
        if not isinstance(cmd, list) or not cmd or cmd[0] != "git":
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="faked",
            )
        # Skip past `git` and any `-C <cwd>` flag at the start.
        rest = cmd[1:]
        if len(rest) >= 2 and rest[0] == "-C":
            rest = rest[2:]
        verb = rest[0] if rest else ""
        if verb == "worktree" and "list" in rest:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=porcelain, stderr="",
            )
        if verb == "rev-parse":
            for flag in rest[1:]:
                if flag == "--git-common-dir":
                    # Real git returns the path to the common .git dir,
                    # not the repo toplevel. The script takes dirname()
                    # of the result, so we must hand back the .git path
                    # itself (one level deeper than main_root).
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0,
                        stdout=f"{main_root}/.git", stderr="",
                    )
                if flag == "--git-dir":
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0,
                        stdout=f"{main_root}/.git/worktrees/feat", stderr="",
                    )
                if flag == "--show-toplevel":
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout=str(wt_dir), stderr="",
                    )
                if flag == "--abbrev-ref":
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout=branch, stderr="",
                    )
        if verb == "symbolic-ref":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=branch, stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=128, stdout="", stderr="faked",
        )

    return _route


class TestIsWorktreeActive(unittest.TestCase):
    def _route(self, porcelain: str = "worktree /repo\n") -> callable:
        from pathlib import Path as _P
        return _make_route(
            porcelain, _P("/repo"), _P("/repo/.worktrees/feat"), "feat",
        )

    def test_main_checkout_is_active(self) -> None:
        # When cwd == main_root, the cwd path appears in `worktree <path>`
        # listing for the main checkout itself.
        porcelain = "worktree /repo\nHEAD abc123\nbranch refs/heads/main\n"
        with mock.patch.object(save_log.subprocess, "run",
                               side_effect=self._route(porcelain)):
            self.assertTrue(save_log._is_worktree_active("/repo", "/repo"))

    def test_active_worktree_returns_true(self) -> None:
        porcelain = (
            "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n"
            "worktree /repo/.worktrees/feat\nHEAD bbb\nbranch refs/heads/feat\n"
        )
        with mock.patch.object(save_log.subprocess, "run",
                               side_effect=self._route(porcelain)):
            self.assertTrue(save_log._is_worktree_active("/repo/.worktrees/feat", "/repo"))

    def test_removed_worktree_returns_false(self) -> None:
        porcelain = "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n"
        with mock.patch.object(save_log.subprocess, "run",
                               side_effect=self._route(porcelain)):
            self.assertFalse(save_log._is_worktree_active("/repo/.worktrees/gone", "/repo"))

    def test_git_failure_defaults_active(self) -> None:
        # git missing → default True (do not archive, do not drop logs).
        with mock.patch.object(save_log.subprocess, "run",
                               side_effect=FileNotFoundError("git")):
            self.assertTrue(save_log._is_worktree_active("/repo/x", "/repo"))

    def test_git_nonzero_defaults_active(self) -> None:
        # git returns non-zero → default True.
        from pathlib import Path as _P
        route = _make_route(
            "", _P("/repo"), _P("/repo/.worktrees/feat"), "feat",
        )
        def nonzero(cmd, *a, **kw):
            r = route(cmd, *a, **kw)
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout=r.stdout, stderr=r.stderr,
            )
        with mock.patch.object(save_log.subprocess, "run", side_effect=nonzero):
            self.assertTrue(save_log._is_worktree_active("/repo/x", "/repo"))


class TestArchiveStaleRouting(unittest.TestCase):
    def _setup_repo(self, tmp: Path) -> tuple[Path, Path]:
        """Create a tiny fake repo: main checkout + a worktree-style subdir."""
        main_root = (tmp / "repo").resolve()
        main_root.mkdir()
        wt_dir = main_root / ".worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        # find_worktree_for_cwd walks up looking for a `.git` *file*.
        # Without the marker, it returns None and the worktree-side
        # write is skipped entirely — the integration test wouldn't
        # exercise the archive branch. Use realpath so the script's
        # `.resolve()` math matches on macOS where /tmp is a symlink.
        gitdir = main_root / ".git" / "worktrees" / "feat"
        gitdir.mkdir(parents=True)
        (wt_dir / ".git").write_text(f"gitdir: {gitdir}\n")
        return main_root, wt_dir

    def _write_fake_transcript(self, tmp: Path) -> Path:
        tf = tmp / "transcript.jsonl"
        tf.write_text(_raw_jsonl([
            {"type": "assistant", "message": {
                "role": "assistant", "content": "hi",
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 0},
            }},
        ]))
        return tf

    def test_archive_stale_routes_to_archive_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            main_root, wt_dir = self._setup_repo(tmp)
            transcript = self._write_fake_transcript(tmp)
            # worktree list porcelain: only the main checkout, no feat
            porcelain = (
                f"worktree {os.path.realpath(main_root)}\n"
                "HEAD aaa\nbranch refs/heads/main\n"
            )

            old_cwd = os.getcwd()
            try:
                os.chdir(wt_dir)
                stdin_payload = _fake_payload(
                    transcript=str(transcript),
                    cwd=str(wt_dir),
                    session_id="sess-x",
                )
                rc = _invoke_save_log(
                    ["--tool", "claude-code", "--archive-stale"],
                    stdin_payload,
                    main_root=main_root, wt_dir=wt_dir,
                    branch="feat-branch", porcelain=porcelain,
                )
                self.assertEqual(rc, 0)
            finally:
                os.chdir(old_cwd)

            # The worktree-side write should land in
            # logs/.archive/feat-branch/<ts>/, NOT in
            # logs/<tool>/feat-branch/ on the worktree dir.
            archive_root = main_root / "logs" / ".archive" / "feat-branch"
            self.assertTrue(archive_root.exists(),
                            f"missing archive dir {archive_root}")
            archive_files = list(archive_root.rglob("sess-x.jsonl"))
            self.assertEqual(len(archive_files), 1,
                             f"expected 1 archived file, got {archive_files}")
            # The worktree's own logs/<tool>/<branch>/ must NOT exist.
            wt_logs = wt_dir / "logs" / "claude-code" / "feat-branch"
            self.assertFalse(wt_logs.exists(),
                             f"unexpected write to {wt_logs}")
            # The main-checkout canonical write should still happen.
            main_logs = main_root / "logs" / "claude-code" / "feat-branch"
            self.assertTrue(main_logs.exists(),
                            f"missing main write at {main_logs}")
            self.assertTrue((main_logs / "sess-x.jsonl").exists())

    def test_without_flag_does_not_archive(self) -> None:
        """Regression guard: without --archive-stale, behavior is unchanged."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            main_root, wt_dir = self._setup_repo(tmp)
            transcript = self._write_fake_transcript(tmp)
            porcelain = (
                f"worktree {os.path.realpath(main_root)}\n"
                "HEAD aaa\nbranch refs/heads/main\n"
            )

            old_cwd = os.getcwd()
            try:
                os.chdir(wt_dir)
                stdin_payload = _fake_payload(
                    transcript=str(transcript),
                    cwd=str(wt_dir),
                    session_id="sess-y",
                )
                rc = _invoke_save_log(
                    ["--tool", "claude-code"],
                    stdin_payload,
                    main_root=main_root, wt_dir=wt_dir,
                    branch="feat-branch", porcelain=porcelain,
                )
                self.assertEqual(rc, 0)
            finally:
                os.chdir(old_cwd)

            wt_logs = wt_dir / "logs" / "claude-code" / "feat-branch"
            self.assertTrue((wt_logs / "sess-y.jsonl").exists())
            archive_root = main_root / "logs" / ".archive"
            self.assertFalse(any(archive_root.rglob("sess-y.jsonl")),
                             "should not have archived without --archive-stale")


if __name__ == "__main__":
    unittest.main()
