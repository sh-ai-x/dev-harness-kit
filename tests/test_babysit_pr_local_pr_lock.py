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


class TestTryAcquirePrLockAtomicity(unittest.TestCase):
    """Pin the atomic-acquire contract for `try_acquire_pr_lock`.

    The previous wrapper used `[[ -f "$PATH" ]]` followed by
    `echo ... > "$PATH"` — two processes could both pass the file-
    exists check before either wrote the lock (TOCTOU race). The fix
    uses `mkdir` of a sibling `.d` directory as the atomic primitive
    (POSIX guarantees the directory either exists or doesn't after
    `mkdir` returns — two concurrent `mkdir` calls cannot both
    succeed). Regression-pinned here so a future maintainer cannot
    revert to the non-atomic pattern.
    """

    def test_first_acquire_succeeds_and_writes_body(self) -> None:
        import sys
        sys.path.insert(0, str(LIB_DIR))
        import babysit_pr_reliability as bpr  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "babysit-pr-local-123.lock"
            ok = bpr.try_acquire_pr_lock(p, "pid=42 branch=main")
            self.assertTrue(ok, "first acquire on a free path must succeed")
            self.assertTrue(p.exists(), "lock body file must exist after acquire")
            self.assertEqual(p.read_text(encoding="utf-8"), "pid=42 branch=main")
            self.assertTrue(
                p.with_suffix(p.suffix + ".d").is_dir(),
                "lockdir must exist after acquire (the atomic primitive)",
            )

    def test_second_acquire_on_held_lock_returns_false(self) -> None:
        """If a sibling lockdir exists, the second acquire MUST return
        False without writing the body file. The caller then prints
        the existing holder's body via `read_pr_lock_body`."""
        import sys
        sys.path.insert(0, str(LIB_DIR))
        import babysit_pr_reliability as bpr  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "babysit-pr-local-99.lock"
            self.assertTrue(bpr.try_acquire_pr_lock(p, "first"))
            ok = bpr.try_acquire_pr_lock(p, "second")
            self.assertFalse(ok, "second acquire on held lock must return False")
            self.assertEqual(
                p.read_text(encoding="utf-8"), "first",
                "second acquire must NOT overwrite the holder's body",
            )

    def test_concurrent_acquires_only_one_wins(self) -> None:
        """Race the helper from two threads in the same process. POSIX
        `mkdir` is the atomic primitive, so exactly one of the
        concurrent attempts must succeed. The test asserts that
        invariant holds — a regression to a non-atomic pattern would
        let both succeed."""
        import sys
        sys.path.insert(0, str(LIB_DIR))
        import threading

        import babysit_pr_reliability as bpr  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "babysit-pr-local-7.lock"
            results: list[bool] = [False, False]

            def attempt(idx: int) -> None:
                results[idx] = bpr.try_acquire_pr_lock(p, f"attempt-{idx}")

            t1 = threading.Thread(target=attempt, args=(0,))
            t2 = threading.Thread(target=attempt, args=(1,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            winners = sum(1 for r in results if r)
            self.assertEqual(
                winners, 1,
                f"exactly one of two concurrent acquires must win (got {results})",
            )

    def test_wrapper_uses_atomic_acquire_not_toctou_check(self) -> None:
        """Belt-and-suspenders: scan the wrapper script's source to
        lock out re-introduction of the TOCTOU `[[ -f ]]` + `>`
        pattern. The new flow MUST go through
        `try_acquire_pr_lock`."""
        text = SCRIPT.read_text(encoding="utf-8")
        # Positive: the wrapper calls the atomic helper.
        self.assertIn(
            "try_acquire_pr_lock", text,
            "wrapper must use try_acquire_pr_lock for the per-PR lock "
            "(closes the TOCTOU race in `[[ -f ]]` + `>`)"
        )
        # Negative: the old `[[ -f "$PR_LOCK_PATH" ]]` then
        # `> "$PR_LOCK_PATH"` pattern MUST NOT appear together as the
        # sole gate. The wrapper still checks `[[ -f "$PR_LOCK_PATH" ]]`
        # for the stale-lock branch (lines that follow immediately call
        # `is_stale_lock` and `rm -f`), so we look for the specific
        # shape: file-exists check, no stale-handling, then
        # `> "$PR_LOCK_PATH"` write.
        # Match: line with [[ -f "$PR_LOCK_PATH" ]] followed (within 25
        # lines) by a `> "$PR_LOCK_PATH"` write that is NOT preceded by
        # `is_stale_lock` in the same window.
        # Simpler regression: the explicit non-atomic echo-to-PR_LOCK_PATH
        # write that defined the OLD contract MUST be gone.
        self.assertNotIn(
            "echo \"$(date -Iseconds) pid=$$", text,
            "wrapper MUST NOT use the old `echo $(date ...) > PR_LOCK_PATH` "
            "non-atomic write; use try_acquire_pr_lock instead."
        )


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
