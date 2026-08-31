"""render_review_local_screenshot.py -- capture tools/review-local-preview.png.

Renders the no-buttons HTML viewer against a sample babysit session
and saves a PNG screenshot for embedding in
docs/tools/review-local-html-viewer.md. Used as a manual
documentation aid, not a CI step (the underlying HTML is snapshot-
tested by `tests/test_review_local_server.py`).

Run from the project root:

    python3 tools/render_review_local_screenshot.py

Output: tools/review-local-preview.png
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREVIEW_HTML = PROJECT_ROOT / "tools" / "review-local-preview.html"
OUTPUT_PNG = PROJECT_ROOT / "tools" / "review-local-preview.png"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server did not start on {host}:{port} within {timeout}s")


def main() -> int:
    if not PREVIEW_HTML.exists():
        print(f"error: {PREVIEW_HTML} missing", file=sys.stderr)
        return 1

    # Build a hermetic fake project tree in a tmpdir: a `bin/`
    # with the server + a tiny `review-local.sh`, symlinked
    # `tools/` (so PREVIEW_HTML resolves), and a pre-populated
    # live log that the `/tail` SSE route will stream. The page
    # renders against `/tail` which is read-only -- no subprocess
    # is spawned by this script.
    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp) / "repo"
        fake_root.mkdir()
        (fake_root / "bin").mkdir()
        # The server's `main()` refuses to boot without a real
        # bin/review-local.sh (it just checks for existence -- it is
        # not actually invoked because the /tail route is read-only).
        # Provide a no-op so the boot check passes.
        (fake_root / "bin" / "review-local.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        (fake_root / "tools").symlink_to(PROJECT_ROOT / "tools")
        # Pre-populate the live log with three "gate markers" + a
        # verdict so the screenshot shows meaningful content (gray
        # dots flip to approved/changes on the rendered page).
        log_dir = fake_root / ".dev-kit"
        log_dir.mkdir(parents=True)
        live_log = log_dir / "babysit-pr-local-live.log"
        live_log.write_text(
            "  running /dev-kit:review via provider=minimax (dry_run=0)\n"
            "**Verdict:** Approve\n"
            "  running /dev-kit:security via provider=minimax (dry_run=0)\n"
            "**Verdict:** Approve\n"
            "  running /dev-kit:maintenance via provider=minimax (dry_run=0)\n"
            "**Verdict:** Changes Requested\n"
            "  verdicts: review='Approve' security='Approve' maintenance='Changes Requested'\n"
            "  combined verdict: Changes Requested\n"
            "  L3 evidence: pytest tail line found in PR body\n"
            "##BABYSIT-DONE exit_code=1##\n",
            encoding="utf-8",
        )

        # Copy the server into the fake bin/. A COPY (not symlink)
        # is required so `Path(__file__).resolve()` walks up into
        # the fake tree -- see the same constraint in
        # tests/test_review_local_server.py.
        shutil.copy(PROJECT_ROOT / "bin" / "review-local-server.py",
                    fake_root / "bin" / "review-local-server.py")

        port = _free_port()
        server_proc = subprocess.Popen(
            [sys.executable, str(fake_root / "bin" / "review-local-server.py"),
             "--port", str(port), "--bind", "127.0.0.1"],
            cwd=str(fake_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            try:
                _wait_for_server("127.0.0.1", port, timeout=5)
            except RuntimeError:
                out = (server_proc.stdout.read(1000).decode("utf-8", errors="replace")
                       if server_proc.stdout else "")
                raise RuntimeError(f"server boot failed. output:\n{out}")
            # Wait a moment for the page's SSE connection to receive
            # the pre-populated log lines + the iteration_done frame.
            time.sleep(0.6)

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{port}/pr/725?autostart=1",
                          wait_until="domcontentloaded", timeout=10_000)
                # Give the SSE frames a beat to render the verdict
                # colors. networkidle should cover it but the page
                # never closes the EventSource, so we wait for the
                # "Changes Requested" header text instead.
                page.wait_for_selector("text=Changes Requested", timeout=5_000)
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUTPUT_PNG), full_page=False)
                browser.close()
            print(f"wrote {OUTPUT_PNG}")
            return 0
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
