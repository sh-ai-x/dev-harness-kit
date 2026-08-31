"""test_babysit_pr_local_pr_lock.py -- per-PR lock tests for bin/babysit-pr-local.sh.

The wrapper MUST refuse to start when another babysit-pr-local process
is already iterating the same PR, regardless of worktree. The lock
path encodes the PR number; the per-worktree lock in
skills/babysit-pr-local/SKILL.md is unchanged.

Coverage:

  - Lock acquire on a free PR path succeeds (no prior lock).
  - A second concurrent wrapper invocation on the same PR exits 1 with
    a one-line "already running" diagnostic that includes the holder's
    PID + branch (hermetic: a fake reviewer script simulates a long-
    running `bin/review-local.sh` via `sleep`, so the first wrapper is
    still alive when the second arrives).
  - A stale lock (recorded PID no longer alive) is detected by
    `lib/babysit_pr_reliability.is_stale_lock` and removed; the
    second wrapper proceeds.
  - A lock for a different PR does NOT block this wrapper (the lock
    path encodes the PR number).
  - `lib.babysit_pr_reliability.read_pr_lock_body` returns the file
    body verbatim, or "" on missing/unreadable.

These tests are hermetic: every `bin/review-local.sh` invocation is
intercepted by a tmpdir-installed fake script -- the real
`bin/review-local.sh` is never executed. The "PID alive" signal comes
from `kill -0` succeeding on the still-sleeping fake subprocess, so
the staleness check is meaningful without mocks.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT = PROJECT_ROOT / "bin" / "babysit-pr-local.sh"
LIB_DIR = PROJECT_ROOT / "lib"


def _setup_tmpdir_with_fake_reviewer(test_self: "TestPerPrLockAcquisition | TestPerPrLockIsolation") -> None:
    """Install the wrapper + a fake review-local.sh in a fresh tmpdir.

    The fake reviewer sleeps long enough that the first wrapper
    invocation is still alive when the second arrives, so the per-PR
    lock has a real (kill -0-able) PID to detect.

    Symlinks the real `lib/` into the tmpdir so the wrapper's
    `sys.path.insert(0, '$SCRIPT_DIR/../lib')` import resolves to the
    project's `babysit_pr_reliability.py` without per-test mocking.
    The symlink is read-only (no executable scripts in lib/, no
    write-through risk).
    """
    test_self._tmp = tempfile.TemporaryDirectory()
    test_self.addCleanup(test_self._tmp.cleanup)
    test_self.bindir = Path(test_self._tmp.name) / "bin"
    test_self.bindir.mkdir(parents=True)
    # Symlink lib/ so the wrapper's `bin/../lib` lookup resolves to
    # the real project lib/ (where babysit_pr_reliability.py lives).
    (Path(test_self._tmp.name) / "lib").symlink_to(LIB_DIR)
    test_self.wrapper = test_self.bindir / "babysit-pr-local.sh"
    test_self.wrapper.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    test_self.wrapper.chmod(test_self.wrapper.stat().st_mode | stat.S_IXUSR)
    test_self.fake_review = test_self.bindir / "review-local.sh"
    test_self.fake_review.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"FAKE_CALLED pid=$$ pr=$1\" >&2\n"
        "# Long enough that the wrapper holds the lock across the\n"
        "# second invocation but short enough that the test does not\n"
        "# block longer than ~3s. The second wrapper must see this\n"
        "# process still alive (kill -0 succeeds) -> lock is held.\n"
        "sleep 3\n"
        "exit \"${BABYSIT_STUB_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    test_self.fake_review.chmod(test_self.fake_review.stat().st_mode | stat.S_IXUSR)


class TestPerPrLockHelpers(unittest.TestCase):
    """Pure-helper coverage for `read_pr_lock_body`."""

    def test_read_pr_lock_body_returns_content(self) -> None:
        import sys
        sys.path.insert(0, str(LIB_DIR))
        import babysit_pr_reliability as bpr  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "babysit-pr-local-123.lock"
            p.write_text("2026-08-30T12:34:56+00:00 pid=9999 branch=feat/x source=babysit-pr-local pr=123\n", encoding="utf-8")
            body = bpr.read_pr_lock_body(p)
            self.assertIn("pid=9999", body)
            self.assertIn("branch=feat/x", body)

    def test_read_pr_lock_body_missing_returns_empty(self) -> None:
        import sys
        sys.path.insert(0, str(LIB_DIR))
        import babysit_pr_reliability as bpr  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "does-not-exist.lock"
            self.assertEqual(bpr.read_pr_lock_body(p), "")

    def test_read_pr_lock_body_is_a_directory_returns_empty(self) -> None:
        """A directory at the lock path (operator error or hostile fs)
        must not raise -- the caller's "is stale" gate already passed,
        so an empty body just means "treat as stale".
        """
        import sys
        sys.path.insert(0, str(LIB_DIR))
        import babysit_pr_reliability as bpr  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(bpr.read_pr_lock_body(t), "")


class TestPerPrLockAcquisition(unittest.TestCase):
    """End-to-end shell tests: two concurrent wrappers on the same PR."""

    def setUp(self) -> None:
        _setup_tmpdir_with_fake_reviewer(self)
        # Use an isolated git-common-dir so the per-PR lock files do
        # NOT land in the real repo's .dev-kit/ (which would race with
        # a developer's own babysit session).
        self.fake_common = Path(self._tmp.name) / "fake-git-common"
        self.fake_common.mkdir()
        self.env = os.environ.copy()
        self.env["BABYSIT_NO_VIEWER"] = "1"
        self.env["BABYSIT_LOCK_PARENT"] = str(self.fake_common)

    def _wrap(self) -> subprocess.CompletedProcess:
        """Run the wrapper once, in the foreground, with our env."""
        return subprocess.run(
            ["bash", str(self.wrapper), "42"],
            cwd=str(self.bindir),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=20,
        )

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["bash", str(self.wrapper), "42"],
            cwd=str(self.bindir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )

    def test_first_wrapper_acquires_lock_and_runs(self) -> None:
        r = self._wrap()
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        lock_path = self.fake_common / "dev-kit" / "babysit-pr-local-42.lock"
        self.assertFalse(
            lock_path.exists(),
            "lock must be released on exit",
        )

    def test_second_wrapper_on_same_pr_exits_1_with_already_running(self) -> None:
        """Launch two wrappers on PR #42 concurrently. The second must
        exit 1 immediately with a one-line "already running" diagnostic
        that mentions the first wrapper's PID + branch, NOT block on
        the fake reviewer's 3-second sleep.
        """
        proc1 = self._spawn()
        # Wait until the wrapper has acquired the lock. The lock file
        # is written BEFORE the fake reviewer is spawned, so a stat()
        # poll is safe. Cap at 5s in case the wrapper hits an unrelated
        # error.
        lock_path = self.fake_common / "dev-kit" / "babysit-pr-local-42.lock"
        deadline = time.monotonic() + 5
        holder_body = ""
        while time.monotonic() < deadline:
            if lock_path.exists():
                body = lock_path.read_text(encoding="utf-8")
                m = re.search(r"pid=(\d+)", body)
                if m:
                    pid = int(m.group(1))
                    try:
                        os.kill(pid, 0)
                        holder_body = body
                        break
                    except ProcessLookupError:
                        pass
            time.sleep(0.05)
        else:
            proc1.kill()
            err = proc1.stderr.read() if proc1.stderr else ""
            self.fail(f"first wrapper never acquired lock within 5s; stderr={err}")
        try:
            t0 = time.monotonic()
            proc2 = subprocess.run(
                ["bash", str(self.wrapper), "42"],
                cwd=str(self.bindir),
                capture_output=True,
                text=True,
                env=self.env,
                timeout=5,
            )
            elapsed = time.monotonic() - t0
            self.assertEqual(
                proc2.returncode, 1,
                f"second wrapper must exit 1 (already running); got rc={proc2.returncode} stderr={proc2.stderr}",
            )
            self.assertLess(
                elapsed, 2.0,
                f"second wrapper must refuse immediately, not block on the 3s sleep; took {elapsed:.2f}s",
            )
            combined = proc2.stdout + proc2.stderr
            self.assertIn("already running", combined.lower())
            m_holder = re.search(r"pid=(\d+)", holder_body)
            self.assertIsNotNone(m_holder, f"holder body missing pid=: {holder_body!r}")
            self.assertIn(
                m_holder.group(1), combined,
                f"second wrapper's diagnostic must include the holder's pid={m_holder.group(1)}; got: {combined!r}",
            )
        finally:
            try:
                proc1.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc1.kill()


class TestPerPrLockIsolation(unittest.TestCase):
    """Locks for DIFFERENT PRs must not collide."""

    def setUp(self) -> None:
        _setup_tmpdir_with_fake_reviewer(self)
        self.fake_common = Path(self._tmp.name) / "fake-git-common"
        self.fake_common.mkdir()
        self.env = os.environ.copy()
        self.env["BABYSIT_NO_VIEWER"] = "1"
        self.env["BABYSIT_LOCK_PARENT"] = str(self.fake_common)

    def _wrap(self, pr: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.wrapper), pr],
            cwd=str(self.bindir),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=20,
        )

    def test_lock_is_scoped_per_pr(self) -> None:
        """A lock for PR 42 must NOT block PR 99 on the same machine.

        The lock path encodes the PR number so two parallel babysit
        sessions on different PRs can co-exist (the parent skill body
        runs them in series, but the wrapper-level guarantee is the
        safety net for direct invocations).
        """
        lock_dir = self.fake_common / "dev-kit"
        lock_dir.mkdir(parents=True, exist_ok=True)
        planted = lock_dir / "babysit-pr-local-42.lock"
        planted.write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid={os.getpid()} branch=feat/x source=babysit-pr-local pr=42\n",
            encoding="utf-8",
        )
        try:
            r = self._wrap("99")
            self.assertEqual(
                r.returncode, 0,
                f"PR 99 must NOT be blocked by PR 42's lock; got rc={r.returncode} stderr={r.stderr}",
            )
        finally:
            planted.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
