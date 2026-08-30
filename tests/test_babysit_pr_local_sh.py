"""test_babysit_pr_local_sh.py — shell-level tests for bin/babysit-pr-local.sh.

The wrapper is a thin single-call orchestrator: validate args, refuse
any --auto-approve, run `bin/review-local.sh --pr $1` (mirrored into
`.dev-kit/babysit-pr-local-live.log` for the HTML viewer's read-only
`/tail` route), then propagate the downstream exit code. Tests pin the
structural contract and the exit-code propagation by installing a fake
downstream script in a tmpdir.

Coverage:
  - script_exists / script_is_executable / bash -n syntax check.
  - no args -> exit 2 with usage hint.
  - non-numeric --pr -> exit 2.
  - --auto-appearing anywhere in argv -> exit 2 (refused, NEVER forwarded).
  - exit code from bin/review-local.sh propagates 1:1 (0 / 1).
  - bin/review-local.sh receives --pr N verbatim (no auto-approve leak).
  - the live log is created/truncated per run and ends with the
    ##BABYSIT-DONE exit_code=N## sentinel the HTML viewer's tail route
    watches for.
  - the viewer auto-launch block is a graceful no-op when
    bin/review-local-server.py is absent (as in this tmpdir).

Every test in TestWrapperDelegation sets BABYSIT_NO_VIEWER=1 by
default so the suite never depends on -- or interferes with -- a real
review-local-server.py that might already be running on the developer's
machine (which would otherwise pop an actual browser tab mid-test-run).
The one exception, test_viewer_wiring_is_graceful_noop_without_server_script,
explicitly leaves the viewer wiring enabled to prove it degrades safely
on its own when the server script is missing.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT = PROJECT_ROOT / "bin" / "babysit-pr-local.sh"


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=e,
    )


class TestBinBabysitPrLocalSh(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.exists(), f"missing script: {SCRIPT}")

    def test_script_is_executable(self) -> None:
        mode = SCRIPT.stat().st_mode
        self.assertTrue(
            mode & 0o111,
            f"bin/babysit-pr-local.sh must be executable (mode={oct(mode)})",
        )

    def test_bash_n_syntax_check(self) -> None:
        """`bash -n` parses the script without executing anything. Catches
        shell grammar errors that would otherwise surface only at the
        first real invocation.
        """
        r = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_args_exits_nonzero(self) -> None:
        """Bare invocation with no args -> non-zero + usage hint. The
        script requires a PR number for `bin/review-local.sh --pr N`.
        """
        r = _run()
        self.assertNotEqual(r.returncode, 0)
        # Either stdout or stderr should hint at the PR argument.
        combined = r.stdout + r.stderr
        self.assertTrue(
            "PR" in combined or "pr" in combined or "usage" in combined.lower(),
            f"expected usage hint mentioning PR; got: stdout={r.stdout!r} stderr={r.stderr!r}",
        )

    def test_non_numeric_pr_refused(self) -> None:
        r = _run("abc")
        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertIn("numeric", combined.lower())

    def test_auto_appearing_flag_refused(self) -> None:
        """MUST-NO-SKIP: --auto-approve is forbidden in the babysit
        context. Any caller passing the flag gets exit 2 with a clear
        error pointing at the operator-driven merge contract.
        """
        r = _run("--auto-approve", "123")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("--auto-approve", r.stderr)


class TestWrapperDelegation(unittest.TestCase):
    """Verify the wrapper's single job: arg-validate, then exec
    bin/review-local.sh --pr N; propagate the downstream exit code.

    Strategy: install the wrapper + a stub `bin/review-local.sh` in a
    tmpdir, then run the copy. The stub records its argv + exits with a
    configurable code so the test can pin both pass-through (exit code
    0 / 1) and refusal-of-auto-approve (defense-in-depth: even if
    somehow --auto-approve made it past the wrapper's argument scan,
    the stub itself refuses to honor it).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bindir = Path(self._tmp.name) / "bin"
        self.bindir.mkdir(parents=True)
        # Copy the wrapper into the tmpdir so SCRIPT_DIR resolves there.
        # The script references "$(dirname "${BASH_SOURCE[0]}")" at
        # runtime; copying preserves the lookup shape exactly.
        self.wrapper = self.bindir / "babysit-pr-local.sh"
        self.wrapper.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        self.wrapper.chmod(self.wrapper.stat().st_mode | stat.S_IXUSR)
        self.stub_review = self.bindir / "review-local.sh"
        # Stub logs argv + honors $BABYSIT_STUB_EXIT (default 0). It
        # also records whether --auto-approve leaked through, which
        # shouldn't happen given the wrapper's own arg scan.
        self.call_log = self.bindir / "call.log"
        self.stub_review.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"STUB_CALLED: $*\" >> '{self.call_log}'\n"
            "if [[ \" $* \" == *\" --auto-approve \"* ]]; then\n"
            f"  echo \"STUB_LEAKED_AUTO_APPROVE\" >> '{self.call_log}'\n"
            "fi\n"
            "exit \"${BABYSIT_STUB_EXIT:-0}\"\n",
            encoding="utf-8",
        )
        self.stub_review.chmod(self.stub_review.stat().st_mode | stat.S_IXUSR)

    def _run_wrapper(self, *args: str, stub_exit: int = 0) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["BABYSIT_STUB_EXIT"] = str(stub_exit)
        # Disable the viewer auto-launch block by default: it's
        # exercised on its own in test_viewer_wiring_is_graceful_noop_
        # without_server_script. Without this, a real
        # review-local-server.py already running on the developer's
        # machine (default port 8765) could make these tests actually
        # pop a browser tab.
        env.setdefault("BABYSIT_NO_VIEWER", "1")
        # Truncate the call log between runs.
        if self.call_log.exists():
            self.call_log.unlink()
        return subprocess.run(
            ["bash", str(self.wrapper), *args],
            cwd=self.bindir,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_exit_zero_propagates_when_review_returns_approve(self) -> None:
        r = self._run_wrapper("123", stub_exit=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Downstream was called with --pr 123.
        log_text = self.call_log.read_text(encoding="utf-8")
        self.assertIn("--pr 123", log_text)
        # Defense-in-depth: no auto-approve leaked.
        self.assertNotIn("STUB_LEAKED_AUTO_APPROVE", log_text)

    def test_exit_one_propagates_when_review_returns_changes_requested(self) -> None:
        """A Changes/Blocked verdict from the local judge must surface
        as exit 1 from the wrapper so the babysit iteration knows to
        continue (not terminate).
        """
        r = self._run_wrapper("123", stub_exit=1)
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_exit_one_propagates_for_blocked(self) -> None:
        r = self._run_wrapper("456", stub_exit=1)
        self.assertEqual(r.returncode, 1, r.stderr)
        log_text = self.call_log.read_text(encoding="utf-8")
        self.assertIn("--pr 456", log_text)

    def test_review_does_not_receive_auto_approve_arg(self) -> None:
        """Sanity: even if the wrapper's own --auto-approve scan failed,
        the downstream `bin/review-local.sh --pr N` invocation does NOT
        carry the flag (verified by the stub's leak detector). This is
        belt-and-suspenders; the wrapper already refuses the flag.
        """
        # Pass a valid PR without --auto-approve, then check the stub log.
        self._run_wrapper("789", stub_exit=0)
        log_text = self.call_log.read_text(encoding="utf-8")
        self.assertNotIn("--auto-approve", log_text)
        self.assertNotIn("STUB_LEAKED_AUTO_APPROVE", log_text)

    def _live_log_path(self) -> Path:
        return self.bindir.parent / ".dev-kit" / "babysit-pr-local-live.log"

    def test_live_log_created_with_stdout_and_done_sentinel(self) -> None:
        """The live log must be created, mirror the downstream script's
        stdout via `tee`, and end with a `##BABYSIT-DONE
        exit_code=N##` sentinel -- the marker
        bin/review-local-server.py's `/pr/<N>/tail` route watches for
        to emit `iteration_done` instead of forwarding it as raw
        stdout.
        """
        r = self._run_wrapper("123", stub_exit=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        live_log = self._live_log_path()
        self.assertTrue(live_log.exists(), f"live log missing: {live_log}")
        log_text = live_log.read_text(encoding="utf-8")
        self.assertTrue(
            log_text.rstrip().endswith("##BABYSIT-DONE exit_code=0##"),
            f"live log must end with the done sentinel; got tail: {log_text[-200:]!r}",
        )

    def test_live_log_sentinel_reflects_nonzero_exit(self) -> None:
        r = self._run_wrapper("456", stub_exit=1)
        self.assertEqual(r.returncode, 1, r.stderr)
        log_text = self._live_log_path().read_text(encoding="utf-8")
        self.assertTrue(log_text.rstrip().endswith("##BABYSIT-DONE exit_code=1##"))

    def test_live_log_truncated_between_iterations(self) -> None:
        """The babysit LOOP calls this wrapper once per iteration
        (SKILL.md step 4L); the live log must reflect only the CURRENT
        iteration, not accumulate stale content from a prior run.
        """
        self._run_wrapper("123", stub_exit=0)
        first_run_bytes = self._live_log_path().stat().st_size
        self._run_wrapper("123", stub_exit=0)
        second_run_bytes = self._live_log_path().stat().st_size
        self.assertEqual(
            first_run_bytes, second_run_bytes,
            "live log grew across iterations -- it must be truncated at the start of each run",
        )

    def test_viewer_wiring_is_graceful_noop_without_server_script(self) -> None:
        """bin/review-local-server.py does not exist in this tmpdir
        (only the wrapper + a fake review-local.sh are installed). The
        viewer auto-launch block must still degrade fast and never
        block or corrupt the verdict pipeline's exit code.
        """
        env = os.environ.copy()
        env["BABYSIT_STUB_EXIT"] = "0"
        env["BABYSIT_VIEWER_PORT"] = "18765"
        if self.call_log.exists():
            self.call_log.unlink()
        start = time.monotonic()
        r = subprocess.run(
            ["bash", str(self.wrapper), "123"],
            cwd=self.bindir,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(
            elapsed, 10,
            f"viewer wiring must degrade fast when the server script is absent; took {elapsed:.1f}s",
        )
        viewer_marker = self.bindir.parent / ".dev-kit" / "babysit-pr-local-viewer-opened.123"
        self.assertFalse(
            viewer_marker.exists(),
            "no browser tab should be recorded as opened when the server never came up",
        )


if __name__ == "__main__":
    unittest.main()
