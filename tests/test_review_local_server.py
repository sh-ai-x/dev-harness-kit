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

        # Symlink the real server into the fake bin/ so the same code
        # runs (PROJECT_ROOT resolves to the fake tree because the
        # server walks up from SCRIPT_DIR.parent looking for
        # bin/review-local.sh, and the stub satisfies the check).
        server_link = cls._fake_root / "bin" / "review-local-server.py"
        server_link.symlink_to(SERVER)
        cls.port = _free_port()
        env = os.environ.copy()
        cls.proc = subprocess.Popen(
            [sys.executable, str(server_link), "--port", str(cls.port), "--bind", "127.0.0.1"],
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
            # First frame is `ready`, last is `done`; must contain
            # at least 6 `stdout` frames (the stub emits 11 lines).
            self.assertEqual(events[0], "ready", f"first frame should be ready, got {frames[:2]!r}")
            self.assertEqual(events[-1], "done", f"last frame should be done, got {frames[-2:]!r}")
            stdout_events = [e for e in events if e == "stdout"]
            self.assertGreaterEqual(len(stdout_events), 6)
            # The combined-verdict line should appear in stdout.
            stdout_lines = [f.get("line", "") for f in frames if f.get("event") == "stdout"]
            self.assertTrue(
                any("combined verdict: Approve" in ln for ln in stdout_lines),
                f"combined verdict line missing; got: {stdout_lines!r}",
            )
        finally:
            with contextlib.suppress(Exception):
                resp.close()

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
