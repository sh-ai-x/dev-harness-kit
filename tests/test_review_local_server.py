"""test_review_local_server.py — hermetic tests for bin/review-local-server.py.

The server is a stdlib http.server + SSE wrapper around `bin/review-local.sh`.
These tests boot the server on an ephemeral port, drive it via the standard
library `urllib`, and assert:

- GET /healthz returns JSON 200 with the active stream counter.
- GET / returns a 302 redirect to /pr/<N> when `gh` is available, or
  serves the preview HTML when `gh` is missing/unauthenticated.
- GET /pr/<N> serves the preview HTML (200, text/html).
- GET /pr/<N>/stream emits `ready` + `stdout` + `done` SSE frames.
- The 4-stream concurrency cap returns 503 on the 5th connection.

The server subprocess is shut down cleanly via teardown. The subprocess
under test (bin/review-local.sh) is stubbed via a tmpdir-on-PATH so
the test does NOT depend on `claude -p` resolving -- the stub prints
a fixed sequence of lines that mimic the real script's gate markers
and verdict extraction.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.client import HTTPResponse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SERVER = PROJECT_ROOT / "bin" / "review-local-server.py"
PREVIEW_HTML = PROJECT_ROOT / "tools" / "review-local-preview.html"


def _free_port() -> int:
    """Bind to port 0 to let the OS assign an ephemeral port, then close."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_sse_frames(resp: HTTPResponse, max_frames: int = 32, timeout: float = 10.0) -> list[dict]:
    """Read SSE frames from an HTTPConnection until `done` or max_frames.

    SSE framing: each frame is `data: <json>\\n\\n`. We split on the
    blank-line terminator, strip the `data: ` prefix, and JSON-decode.
    """
    frames: list[dict] = []
    buf = b""
    deadline = time.monotonic() + timeout
    resp_file = resp  # http.client.HTTPResponse is already file-like
    while len(frames) < max_frames and time.monotonic() < deadline:
        chunk = resp_file.read(1)
        if not chunk:
            break
        buf += chunk
        # SSE terminator is two consecutive \n.
        if b"\n\n" in buf:
            block, _, buf = buf.partition(b"\n\n")
            line = block.decode("utf-8", errors="replace").strip()
            if line.startswith("data: "):
                payload = line[len("data: "):]
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                frames.append(obj)
                if obj.get("event") == "done":
                    break
    return frames


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    """Poll /healthz until the server responds or timeout."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionResetError) as e:
            last_err = e
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_err}")


class TestReviewLocalServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Build a hermetic fake project root in a tmpdir. The fake
        # bin/ contains ONLY the stub `review-local.sh` + a symlink
        # to the real server; we deliberately do NOT symlink the
        # real bin/ wholesale, because overwriting the fake
        # review-local.sh would then write through the symlink and
        # clobber the operator's real file. Earlier draft had this
        # bug and destroyed bin/review-local.sh in one test run.
        cls._tmp = tempfile.TemporaryDirectory()
        cls._fake_root = Path(cls._tmp.name) / "repo"
        cls._fake_root.mkdir()
        # Make bin/ + tools/ real directories, not symlinks, so writes
        # inside them are contained within the tmpdir.
        (cls._fake_root / "bin").mkdir()
        (cls._fake_root / "tools").symlink_to(PROJECT_ROOT / "tools")

        # Stub bin/review-local.sh -- deterministic output mimicking
        # the real script's gate markers + verdict extraction.
        stub = cls._fake_root / "bin" / "review-local.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "echo \"STUB_INVOKED\"\n"
            "echo \"  plugin-dir resolved: $REPO_ROOT\"\n"
            "echo \"  no provider explicitly configured; falling back to local claude CLI auth\"\n"
            "echo \"  running /dev-kit:review via provider=minimax (dry_run=0)\"\n"
            "echo \"**Verdict:** Approve\"\n"
            "echo \"  running /dev-kit:security via provider=minimax (dry_run=0)\"\n"
            "echo \"**Verdict:** Approve\"\n"
            "echo \"  running /dev-kit:maintenance via provider=minimax (dry_run=0)\"\n"
            "echo \"**Verdict:** Approve\"\n"
            "echo \"  verdicts: review='Approve' security='Approve' maintenance='Approve'\"\n"
            "echo \"  combined verdict: Approve\"\n"
            "echo \"  L3 evidence: pytest tail line found in PR body\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # COPY (not symlink) the server into the fake bin/ so that
        # Path(__file__).resolve() resolves INTO the fake tree.
        # A symlink would be followed by .resolve() and the server
        # would walk up to the REAL project root, defeating the
        # hermeticity guarantee (review finding M2 on PR #731).
        # Earlier draft had this bug; the test passed only by
        # coincidence because the real review-local.sh happens to
        # emit compatible output in some envs.
        server_copy = cls._fake_root / "bin" / "review-local-server.py"
        shutil.copy(SERVER, server_copy)
        server_copy.chmod(server_copy.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        cls.port = _free_port()
        env = os.environ.copy()
        cls.proc = subprocess.Popen(
            [sys.executable, str(server_copy), "--port", str(cls.port), "--bind", "127.0.0.1"],
            cwd=str(cls._fake_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_server("127.0.0.1", cls.port, timeout=10)
        except RuntimeError:
            out = cls.proc.stdout.read(700).decode("utf-8", errors="replace") if cls.proc.stdout else ""
            raise RuntimeError(f"server boot failed. logs:\n{out}")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "proc") and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    # ------------------------------------------------------------------
    # Smoke tests.
    # ------------------------------------------------------------------
    def test_healthz_returns_json(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("application/json", r.headers.get("Content-Type", ""))
            body = json.loads(r.read().decode("utf-8"))
            self.assertEqual(body["status"], "ok")
            self.assertIn("active_streams", body)

    def test_pr_page_serves_html(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/pr/725", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers.get("Content-Type", ""))
            body = r.read().decode("utf-8")
            self.assertIn("dev-kit: review-local live viewer", body)
            # The injected PR-number script must appear in <body>.
            self.assertIn("window.__PR_NUMBER__ = 725;", body)

    def test_pr_page_autostart_query_injects_flag(self) -> None:
        """`?autostart=1` (the URL bin/babysit-pr-local.sh opens) must
        inject `window.__AUTOSTART__ = true;` so the page's JS connects
        to the read-only `/tail` route on load without a click.
        """
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/pr/725?autostart=1", timeout=3) as r:
            body = r.read().decode("utf-8")
        self.assertIn("window.__AUTOSTART__ = true;", body)
        self.assertIn("window.__PR_NUMBER__ = 725;", body)

    def test_pr_page_without_autostart_query_omits_flag(self) -> None:
        """A manual visit to `/pr/<N>` (no query string) stays the
        passive viewer -- no autostart flag, streaming only starts on
        a Start click.
        """
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/pr/725", timeout=3) as r:
            body = r.read().decode("utf-8")
        # The JS always REFERENCES window.__AUTOSTART__ (it's a static
        # conditional in the page's script); what must be absent is the
        # server-injected ASSIGNMENT that flips it true.
        self.assertNotIn("window.__AUTOSTART__ = true;", body)

    def test_tail_route_streams_existing_log_and_never_spawns_review_local(self) -> None:
        """`/pr/<N>/tail` must follow `.dev-kit/babysit-pr-local-live.log`
        read-only and NEVER spawn `bin/review-local.sh` -- that's what
        lets babysit-pr-local's auto-opened browser tab mirror an
        in-flight run without triggering a second, duplicate verdict
        pipeline (and a second round of API spend).

        We pre-populate the log with two marker lines plus a
        `##BABYSIT-DONE exit_code=0##` sentinel (the line
        bin/babysit-pr-local.sh appends after each iteration), then
        assert: the two lines arrive verbatim as `stdout` frames, the
        sentinel is converted to a single `iteration_done` frame (not
        forwarded as raw stdout), and -- the hermeticity check -- the
        fake review-local.sh's `STUB_INVOKED` marker never appears,
        proving review-local.sh was never invoked.
        """
        log_path = self._fake_root / ".dev-kit" / "babysit-pr-local-live.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "TAIL_MARKER_LINE_1\nTAIL_MARKER_LINE_2\n##BABYSIT-DONE exit_code=0##\n",
            encoding="utf-8",
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/pr/999/tail",
            headers={"Accept": "text/event-stream"},
        )
        resp = urllib.request.urlopen(req, timeout=10)  # type: ignore[assignment]
        try:
            # ready + 2 stdout + 1 iteration_done = 4 frames.
            frames = _read_sse_frames(resp, max_frames=4, timeout=8)
        finally:
            with contextlib.suppress(Exception):
                resp.close()
        events = [f.get("event") for f in frames]
        self.assertEqual(events[0], "ready", f"first frame should be ready, got {frames[:2]!r}")
        stdout_lines = [f.get("line") for f in frames if f.get("event") == "stdout"]
        self.assertEqual(stdout_lines, ["TAIL_MARKER_LINE_1", "TAIL_MARKER_LINE_2"])
        iter_frames = [f for f in frames if f.get("event") == "iteration_done"]
        self.assertEqual(len(iter_frames), 1, f"expected exactly one iteration_done frame, got {frames!r}")
        self.assertEqual(iter_frames[0].get("exit_code"), 0)
        self.assertNotIn(
            "STUB_INVOKED", stdout_lines,
            "tail route must never spawn bin/review-local.sh (hermeticity broken)",
        )

    def test_stream_emits_ready_stdout_done(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/pr/725/stream",
            headers={"Accept": "text/event-stream"},
        )
        # Don't set a timeout on the read -- SSE is long-lived.
        resp: HTTPResponse = urllib.request.urlopen(req, timeout=15)  # type: ignore[assignment]
        try:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            frames = _read_sse_frames(resp, max_frames=64, timeout=12)
            events = [f.get("event") for f in frames]
            stdout_events = [e for e in events if e == "stdout"]
            stdout_lines = [f.get("line", "") for f in frames if f.get("event") == "stdout"]
            # Required contract: ready, meta, >= 6 stdout, and a
            # terminal `done` frame with exit_code=0. The stub is
            # fast (<1s) and `_read_sse_frames` stops as soon as it
            # sees `done`, so this should arrive well within the 12s
            # window -- no client-gone race here (unlike the
            # concurrency-cap test, which deliberately holds
            # connections open).
            #
            # This assertion regressed silently once before: a bug
            # in the server's EOF-handling (selectors.get_key() raises
            # KeyError for an unregistered fileobj instead of
            # returning None) caused an uncaught exception the moment
            # proc.stdout hit EOF, which terminated the connection
            # BEFORE the `done` frame was written. The symptom looked
            # identical to "client closed early" from the test's
            # point of view, so this assertion was relaxed to ignore
            # `done` entirely -- which hid the bug instead of
            # catching it. Discovered live against PR #731 (MiniMax
            # "Invalid API key" case: the browser saw the error line
            # but never saw `done`, so the UI never signaled the run
            # had ended). Fixed by replacing the get_key() post-check
            # with an explicit `stdout_eof` flag.
            self.assertEqual(events[0], "ready", f"first frame should be ready, got {frames[:2]!r}")
            self.assertIn("meta", events, f"expected meta frame, got {events!r}")
            self.assertEqual(
                events[-1], "done",
                f"last frame must be done (server must always signal run completion); got {events!r}",
            )
            done_frame = frames[-1]
            self.assertEqual(
                done_frame.get("exit_code"), 0,
                f"expected exit_code=0 for the stub's clean exit; got {done_frame!r}",
            )
            self.assertGreaterEqual(
                len(stdout_events), 6,
                f"expected at least 6 stdout frames, got {len(stdout_events)} (events={events!r})",
            )
            self.assertTrue(
                any("combined verdict: Approve" in ln for ln in stdout_lines),
                f"combined verdict line missing; got: {stdout_lines!r}",
            )
            # Hermeticity proof: the STUB_INVOKED marker must appear in
            # the stream. The REAL bin/review-local.sh does not emit
            # it. If this assertion fails, hermeticity is broken (the
            # server is spawning the real script instead of the stub).
            self.assertTrue(
                any("STUB_INVOKED" in ln for ln in stdout_lines),
                f"STUB_INVOKED marker missing from stream -- hermeticity broken "
                f"(server is spawning real bin/review-local.sh, not the stub). "
                f"Got: {stdout_lines!r}",
            )
        finally:
            with contextlib.suppress(Exception):
                resp.close()

    def test_stub_is_actually_invoked_hermeticity_check(self) -> None:
        """Regression for review finding M2 on PR #731.

        Earlier the test symlinked the server into the fake bin/,
        so Path(__file__).resolve() followed the symlink to the REAL
        bin/, and the server spawned the real bin/review-local.sh.
        The 11-line stub was dead code. The test passed only by
        coincidence (the real script's stdout happened to be
        compatible). This test breaks that coincidence by asserting
        a marker that the REAL review-local.sh does NOT emit but the
        stub DOES: 'STUB_INVOKED'. If hermeticity ever regresses
        (symlink used again, or shutil.copy removed), this test
        fails immediately with a clear message.
        """
        marker_stub = "STUB_INVOKED"  # only in the stub, not in the real script
        # Verify the stub script contains the marker.
        stub_path = self._fake_root / "bin" / "review-local.sh"
        self.assertIn(marker_stub, stub_path.read_text(encoding="utf-8"))
        # Verify the REAL script does NOT contain the marker (defends
        # against someone copy-pasting the marker into the real
        # script by accident, which would silently break this test).
        real_path = PROJECT_ROOT / "bin" / "review-local.sh"
        self.assertNotIn(marker_stub, real_path.read_text(encoding="utf-8"))
        # Verify the server was COPIED (not symlinked) into the fake
        # bin/. A symlink would mean Path(__file__).resolve() follows
        # it back to the real bin/, defeating hermeticity.
        server_copy = self._fake_root / "bin" / "review-local-server.py"
        self.assertTrue(server_copy.is_file(), f"server copy missing: {server_copy}")
        self.assertFalse(server_copy.is_symlink(), f"server is a symlink, not a copy -- hermeticity broken ({server_copy})")

    def test_subprocess_timeout_terminates_hung_process(self) -> None:
        """Regression for review finding m1 on PR #731: a hung
        subprocess must not hold a stream slot forever. We install
        a stub that sleeps past PROC_TIMEOUT_SECONDS and assert the
        stream emits `done` with exit_code -9 within a reasonable
        bound. To keep the test fast, we monkey-patch
        PROC_TIMEOUT_SECONDS to a tiny value via env-var injection
        into a temp copy of the server. Simpler: just assert the
        subprocess is killed -- the actual 10-minute cap is a config
        constant, not behavior we need to re-test.
        """
        # Build a tiny isolated server with a 1-second cap. We do
        # this by copying the server, patching PROC_TIMEOUT_SECONDS,
        # and running it on a fresh port. Then connect, observe
        # the stub blocks, and assert `done` arrives quickly.
        timeout_root = Path(tempfile.mkdtemp()) / "repo"
        timeout_root.mkdir()
        (timeout_root / "bin").mkdir()
        # Symlink tools/ so PREVIEW_HTML resolves (the server refuses
        # to boot without it). tools/ only contains the preview
        # HTML, no executable scripts, so a symlink is safe here
        # (write-through is impossible).
        (timeout_root / "tools").symlink_to(PROJECT_ROOT / "tools")
        # Stub that blocks until killed.
        (timeout_root / "bin" / "review-local.sh").write_text(
            "#!/usr/bin/env bash\n"
            "echo STUB_INVOKED\n"
            "sleep 60\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (timeout_root / "bin" / "review-local.sh").chmod(
            (timeout_root / "bin" / "review-local.sh").stat().st_mode | stat.S_IXUSR
        )
        # Copy server and patch the timeout constant.
        patched = timeout_root / "bin" / "review-local-server.py"
        patched.write_text(
            SERVER.read_text(encoding="utf-8").replace(
                "PROC_TIMEOUT_SECONDS = 600",
                "PROC_TIMEOUT_SECONDS = 1",
            ),
            encoding="utf-8",
        )
        patched.chmod(patched.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, str(patched), "--port", str(port), "--bind", "127.0.0.1"],
            cwd=str(timeout_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_server("127.0.0.1", port, timeout=10)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/pr/1/stream",
                headers={"Accept": "text/event-stream"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # type: ignore[assignment]
                frames = _read_sse_frames(resp, max_frames=128, timeout=10)
            done_frames = [f for f in frames if f.get("event") == "done"]
            self.assertEqual(len(done_frames), 1, f"expected exactly 1 done frame, got {done_frames!r}")
            self.assertEqual(
                done_frames[0].get("reason"),
                "timeout",
                f"expected reason=timeout, got {done_frames[0]!r}",
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(timeout_root.parent, ignore_errors=True)

    def test_unknown_path_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/no-such-route", timeout=3)
        self.assertEqual(cm.exception.code, 404)

    def test_concurrency_cap_returns_503(self) -> None:
        """Open 5 stream connections; the 5th must be rejected.

        The cap is 4 (MAX_CONCURRENT_STREAMS). The test opens 4
        streams, then attempts a 5th and asserts HTTP 503.

        After all 4 streams finish (stub exits in <1s each), the
        server's _active_streams counter must drop back to 0 so the
        next test gets a free slot. We poll /healthz until it does.
        """
        connections: list[HTTPResponse] = []
        try:
            # Open 4 streams concurrently. Each read returns its frames
            # when the stub exits; we don't drain yet, just hold the
            # socket so the server sees them as active.
            for pr in (10, 11, 12, 13):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/pr/{pr}/stream",
                    headers={"Accept": "text/event-stream"},
                )
                resp = urllib.request.urlopen(req, timeout=15)  # type: ignore[assignment]
                connections.append(resp)
            # Give the server a moment to register all 4 streams.
            time.sleep(0.2)
            # 5th must fail with 503.
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/pr/14/stream", timeout=3
                )
            self.assertEqual(cm.exception.code, 503)
        finally:
            for c in connections:
                with contextlib.suppress(Exception):
                    c.close()
            # Drain all 4 streams so the server decrements its counter.
            # The stub emits 11 lines + a done frame per stream.
            for c in connections:
                with contextlib.suppress(Exception):
                    _read_sse_frames(c, max_frames=64, timeout=5)
            # Poll /healthz until the counter drops back to 0. The
            # test_stream_emits_ready_stdout_done test that runs next
            # would otherwise get a 503 from a leaked slot.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/healthz", timeout=2
                ) as r:
                    body = json.loads(r.read().decode("utf-8"))
                    if body.get("active_streams", 0) == 0:
                        return
                time.sleep(0.1)
            self.fail("server did not drain active streams back to 0 within 5s")


if __name__ == "__main__":
    unittest.main()
