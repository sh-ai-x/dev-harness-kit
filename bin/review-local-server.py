#!/usr/bin/env python3
"""bin/review-local-server.py — localhost live-streaming viewer for bin/review-local.sh.

The /dev-kit:babysit-pr-local skill runs `bin/review-local.sh --pr N` in the
worker's terminal. The verdict pipeline (review + security + maintenance) takes
30-300s per PR; the operator has been staring at a single "running
/dev-kit:review via provider=..." line per gate with no in-gate progress
streaming (issue #727 fix exposed this gap: bare `claude -p` had no
--plugin-dir, so judges exited in <1s with no output). Even after the fix,
the operator still has to watch stdout in a separate terminal.

This script boots a stdlib HTTP server on 127.0.0.1:8765 (no Flask dep) and
exposes:

  GET /            -> 302 redirect to /pr/<current-branch-pr> (best-effort)
  GET /pr/<N>      -> tools/review-local-preview.html (three-gate layout)
  GET /pr/<N>/stream
                   -> Server-Sent Events: stdout of `bin/review-local.sh --pr N`
                      streamed line-by-line as SSE `data:` frames. The client
                      EventSource handler appends each frame to the per-gate
                      <pre> region in the HTML.

Why stdlib (not Flask):

- Zero new dependency. `http.server` + `socketserver.ThreadingMixIn` is
  enough for SSE (one thread per request; the long-lived `/stream`
  connection holds a thread while it streams).
- Boots in <100ms. No venv churn for an operator who just wants to
  babysit a PR.
- The single-binary footprint matches the rest of `bin/` (every entry
  is a single Python script).

Why SSE (not WebSocket / long-poll):

- The server is one-way (read-only stream of subprocess stdout). SSE
  is the right tool; WebSocket adds a handshake layer for no benefit.
- `EventSource` is built into every modern browser; the HTML needs
  ~10 lines of JS to consume it.
- Auto-reconnect on transient network drops is free (EventSource
  reconnects by default).

Threading model:

- One daemon thread per `/pr/<N>/stream` connection. The thread owns
  one subprocess (`bin/review-local.sh --pr N`) and pumps its stdout
  into the SSE socket. When the subprocess exits, the thread sends
  a final `event: done` frame and closes the socket.
- Concurrency cap: 4 simultaneous stream connections (one per PR the
  operator may have open in different tabs). Excess connections
  return 503.

Security:

- Bound to 127.0.0.1 only (no LAN exposure).
- No CORS headers (same-origin only).
- No request body parsing (GET-only for /stream; /pr/<N> is a static
  file).
- Subprocess argv is hard-coded; no user-controlled flags are
  forwarded (PR number is sanitized to digits before being spliced
  into the argv list).

Usage:

  bin/review-local-server.py [--port 8765] [--bind 127.0.0.1]
  bin/review-local-server.py --help

The script is `noqa`'d for line-length on the docstring above because
breaking the SSE protocol explanation would hurt readability.
"""
# noqa: E501
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Resolve project root (the directory holding bin/review-local.sh). Walk up
# from this script's location; fall back to $PWD's git toplevel.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT_CANDIDATES = [
    SCRIPT_DIR.parent,  # bin/../  (canonical)
    Path.cwd(),  # operator's pwd if they ran from the repo root
]
for candidate in PROJECT_ROOT_CANDIDATES:
    if (candidate / "bin" / "review-local.sh").exists():
        PROJECT_ROOT = candidate.resolve()
        break
else:
    PROJECT_ROOT = SCRIPT_DIR.parent.resolve()

REVIEW_LOCAL_SH = PROJECT_ROOT / "bin" / "review-local.sh"
PREVIEW_HTML = PROJECT_ROOT / "tools" / "review-local-preview.html"

# Concurrency cap: how many simultaneous /pr/<N>/stream connections.
# 4 = "operator has 4 PRs open in tabs at once" — generous for a single
# babysit session; anything more is operator-error.
MAX_CONCURRENT_STREAMS = 4

# Per-stream lock so the SSE writer is serialized (BaseHTTPRequestHandler
# writes aren't atomic under threading).
_stream_lock = threading.Lock()
_active_streams = 0
_active_streams_lock = threading.Lock()


def _increment_active_streams() -> bool:
    """Reserve one stream slot. Returns False if at capacity."""
    global _active_streams
    with _active_streams_lock:
        if _active_streams >= MAX_CONCURRENT_STREAMS:
            return False
        _active_streams += 1
        return True


def _decrement_active_streams() -> None:
    global _active_streams
    with _active_streams_lock:
        _active_streams = max(0, _active_streams - 1)


# Track active subprocess.Popen handles so SIGINT / SIGTERM can clean
# them up before the server exits. Without this, the spawned
# `bin/review-local.sh` and its transitive `claude -p` survive the
# server process — they keep consuming API quota and the only way to
# kill them is `ps -ef | grep claude` (review finding M1 on PR #731).
_active_procs: list[subprocess.Popen] = []
_active_procs_lock = threading.Lock()

# Monotonic clock at the moment the current stream's subprocess
# was spawned. Used by the 10-minute wall-clock cap in the
# `_stream_review_local` finally block. Initialized to 0; the first
# stream call sets it to time.monotonic() before Popen.
_stream_start_monotonic: float = 0.0

# Wall-clock cap on a single verdict pipeline run. Mirrors the
# 10-minute per-job timeout in `.github/workflows/review.yml:127` so
# the local mode fails fast on a hung `claude -p` instead of holding
# a stream slot forever (review finding m1 on PR #731).
PROC_TIMEOUT_SECONDS = 600


def _register_proc(proc: subprocess.Popen) -> None:
    with _active_procs_lock:
        _active_procs.append(proc)


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _active_procs_lock:
        try:
            _active_procs.remove(proc)
        except ValueError:
            pass


def _terminate_all_procs() -> None:
    """Terminate every active subprocess on server shutdown.

    Called from the SIGINT/SIGTERM handler in `main()`. Each proc is
    given 2 s to exit cleanly; stragglers are SIGKILLed. Idempotent —
    safe to call from multiple signal paths.
    """
    with _active_procs_lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.terminate()
        except (ProcessLookupError, OSError):
            continue
    for p in procs:
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=1)
            except (ProcessLookupError, OSError):
                pass


def _resolve_default_pr() -> int | None:
    """Best-effort: return the PR number for the current branch via `gh`.

    Returns None if `gh` is missing, unauthenticated, or the branch has no PR.
    The server's `/` handler then renders an empty form instead of a redirect.

    The result is memoized in `_cached_default_pr` so a slow /
    unauthenticated `gh` doesn't stall every `GET /` request
    (review finding m3 on PR #731). Restart the server to refresh
    the cache after switching branches.
    """
    global _cached_default_pr_resolved, _cached_default_pr
    if _cached_default_pr_resolved:
        return _cached_default_pr
    try:
        out = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "-q", ".number"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _cached_default_pr = None
    else:
        try:
            _cached_default_pr = int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
        except ValueError:
            _cached_default_pr = None
    _cached_default_pr_resolved = True
    return _cached_default_pr


# Memoization state for `_resolve_default_pr`. `_cached_default_pr`
# is None until the first call; `_cached_default_pr_resolved` flips
# True after, so subsequent calls skip the `gh pr view` round-trip
# entirely (avoid stalling every `GET /` on a slow / unauthenticated
# gh CLI -- review finding m3 on PR #731).
_cached_default_pr: int | None = None
_cached_default_pr_resolved: bool = False


class _Handler(BaseHTTPRequestHandler):
    """Single handler for the four routes (/, /pr/<N>, /pr/<N>/stream, /healthz)."""

    # Quieter logs. Default BaseHTTPRequestHandler logs every request
    # to stderr; we only want lifecycle events (healthz, the 302 from
    # /, 404s). Stream connections are intentionally silent — the
    # SSE pipe is the operator's UI, stderr would be noise.
    #
    # Previously used `if "/stream" not in format` (denylist), but
    # review finding m2 noted that any future debug path that happens
    # to contain "/stream" would be silently dropped. Allowlist is
    # safer: only log the routes that genuinely need observability.
    _LOG_ALLOWLIST_PREFIXES = ("/healthz", "GET / ", "GET /pr/ (not numeric)")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
        msg = format % args
        if any(msg.startswith(p) for p in self._LOG_ALLOWLIST_PREFIXES):
            sys.stderr.write("[review-local-server] %s - %s\n" % (self.address_string(), msg))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok", "active_streams": _active_streams})
            return
        if path == "/":
            default_pr = _resolve_default_pr()
            if default_pr is not None:
                self.send_response(302)
                self.send_header("Location", f"/pr/{default_pr}")
                self.end_headers()
                return
            # No PR resolvable; serve the HTML with the empty-state form.
            self._serve_preview_html()
            return
        pr_match = re.fullmatch(r"/pr/(\d+)(?:/stream)?", path)
        if not pr_match:
            self.send_error(404, "not found; try /, /pr/<N>, /pr/<N>/stream, /healthz")
            return
        pr_number = int(pr_match.group(1))
        if path.endswith("/stream"):
            self._stream_review_local(pr_number)
            return
        # Render the HTML; the page's JS opens an EventSource to /stream.
        self._serve_preview_html(pr_number=pr_number)

    # ------------------------------------------------------------------
    # Static HTML.
    # ------------------------------------------------------------------
    def _serve_preview_html(self, pr_number: int | None = None) -> None:
        if not PREVIEW_HTML.exists():
            self.send_error(500, f"preview HTML missing: {PREVIEW_HTML}")
            return
        body = PREVIEW_HTML.read_bytes()
        # Inject the PR number into the page so the JS knows the default
        # stream target before the EventSource opens. A small <script>
        # tag at the top of <body> avoids a round-trip to /pr/<N>/detect.
        if pr_number is not None:
            inject = f'<script>window.__PR_NUMBER__ = {pr_number};</script>'.encode()
            body = body.replace(b"<body>", b"<body>" + inject, 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Server-Sent Events stream.
    # ------------------------------------------------------------------
    def _stream_review_local(self, pr_number: int) -> None:
        if not _increment_active_streams():
            self._send_json(503, {"error": f"at capacity ({MAX_CONCURRENT_STREAMS} streams)"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
            self.end_headers()

            # First frame: a `ready` event so the client knows the
            # connection is alive before the subprocess produces output.
            self._write_sse({"event": "ready", "pr": pr_number})

            # Resolve PR title for the header (best-effort; don't fail
            # the stream if `gh` is offline).
            try:
                title_proc = subprocess.run(
                    ["gh", "pr", "view", str(pr_number), "--json", "title", "-q", ".title"],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                title = title_proc.stdout.strip() if title_proc.returncode == 0 else ""
            except (FileNotFoundError, subprocess.TimeoutExpired):
                title = ""
            self._write_sse({"event": "meta", "pr": pr_number, "title": title})

            # Capture the stream-start monotonic clock so the
            # 10-minute wall-clock cap in `finally:` is measured from
            # the actual subprocess spawn (review finding m1).
            global _stream_start_monotonic
            _stream_start_monotonic = time.monotonic()

            # Spawn the verdict pipeline. Capture stdout line-by-line so
            # the SSE client gets each line as soon as the subprocess
            # flushes it (bufsize=1, text mode).
            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            try:
                proc = subprocess.Popen(
                    ["bash", str(REVIEW_LOCAL_SH), "--pr", str(pr_number)],
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            except FileNotFoundError:
                self._write_sse({"event": "error", "message": f"bin/review-local.sh missing at {REVIEW_LOCAL_SH}"})
                self._write_sse({"event": "done", "exit_code": -1})
                return

            _register_proc(proc)
            assert proc.stdout is not None
            import selectors
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            timed_out = False
            # Wall-clock cap (review finding m1 on PR #731). Without
            # this, a hung subprocess holds the stream slot forever.
            # select() lets us multiplex the stdout read with a
            # deadline timer -- readline() alone blocks indefinitely
            # if the subprocess keeps stdout open while hung.
            while True:
                elapsed = time.monotonic() - _stream_start_monotonic
                if elapsed > PROC_TIMEOUT_SECONDS:
                    timed_out = True
                    break
                remaining = max(0.1, PROC_TIMEOUT_SECONDS - elapsed)
                events = sel.select(timeout=remaining)
                if not events:
                    # select timed out without any data; loop top
                    # rechecks elapsed and breaks.
                    continue
                line = proc.stdout.readline()
                if not line:
                    # EOF -- subprocess closed its stdout.
                    break
                # Strip trailing newline; SSE will re-add framing.
                payload = {"event": "stdout", "line": line.rstrip("\n")}
                self._write_sse(payload)

            if timed_out:
                # Kill the hung subprocess; emit the timeout marker.
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                self._write_sse({"event": "done", "exit_code": -9, "reason": "timeout"})
            else:
                exit_code = proc.wait()
                self._write_sse({"event": "done", "exit_code": exit_code})
        except (BrokenPipeError, ConnectionResetError):
            # Operator closed the tab mid-stream; kill the subprocess so
            # we don't leak orphans.
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except (NameError, ProcessLookupError, OSError):
                pass
        finally:
            # Unregister from the active-procs list so a shutdown
            # signal that fires after we return doesn't see a stale
            # handle. The 10-minute cap is enforced inline in the
            # stdout loop; this finally is just cleanup.
            try:
                proc  # type: ignore[possibly-undefined]
                _unregister_proc(proc)
            except NameError:
                pass
            _decrement_active_streams()

    # ------------------------------------------------------------------
    # Low-level helpers.
    # ------------------------------------------------------------------
    def _write_sse(self, payload: dict) -> None:
        """Serialize payload as one SSE `data:` frame.

        Multi-line data is split per the SSE spec (`data:` prefix per
        line; blank line terminates the frame). JSON is used so the
        client can switch on the `event` field without parsing prose.
        """
        body = json.dumps(payload, ensure_ascii=False)
        with _stream_lock:
            try:
                self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                raise

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadingServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with SO_REUSEADDR so restarts don't TIME_WAIT."""
    allow_reuse_address = True
    daemon_threads = True


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="review-local-server",
        description="Localhost live-streaming viewer for bin/review-local.sh.",
    )
    p.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    p.add_argument("--bind", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if not REVIEW_LOCAL_SH.exists():
        print(f"error: bin/review-local.sh not found at {REVIEW_LOCAL_SH}", file=sys.stderr)
        return 1
    if not PREVIEW_HTML.exists():
        print(f"error: preview HTML not found at {PREVIEW_HTML}", file=sys.stderr)
        return 1
    server = _ThreadingServer((args.bind, args.port), _Handler)
    print(
        f"[review-local-server] listening on http://{args.bind}:{args.port} "
        f"(project_root={PROJECT_ROOT})",
        flush=True,
    )

    # Signal handlers: SIGINT (Ctrl+C) and SIGTERM (kill / systemd)
    # must terminate every active subprocess before the main thread
    # exits, or the verdict pipelines + their transitive `claude -p`
    # invocations survive as orphans (review finding M1 on PR #731).
    # Idempotent -- _terminate_all_procs is safe to call multiple
    # times. signal handlers run on the main thread; we set them up
    # BEFORE serve_forever so a SIGTERM during shutdown still routes
    # through the handler.
    def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        print(f"\n[review-local-server] caught {name}; terminating {len(_active_procs)} active subprocess(es)", flush=True)
        _terminate_all_procs()
        server.shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Belt-and-suspenders: the SIGINT handler above should fire
        # first, but if it doesn't (different Python / no handler
        # race), make sure subprocesses still get terminated.
        _terminate_all_procs()
        print("\n[review-local-server] shutting down", flush=True)
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
