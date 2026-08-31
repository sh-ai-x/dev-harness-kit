"""test_review_local_preview_gate_state.py -- browser-driven test for the
HTML viewer's gate-state transitions.

The viewer at tools/review-local-preview.html mirrors a babysit
session's stdout via the server's `/pr/<N>/tail` SSE route. The left
panel renders three gate dots (`/dev-kit:review`, `/dev-kit:security`,
`/dev-kit:maintenance`) that MUST transition from `running` to
`approved` / `changes` / `blocked` as the corresponding
`**Verdict:** <Word>` line appears in the live log — the user-facing
state machine is the whole point of the dot column.

The previous implementation (PR #772) set the CSS class on the
`**Verdict:**` line (so it renders green/yellow/red) but did NOT
transition the per-gate dot. Result: every gate stayed "running"
forever even after the verdict was clearly visible in the right
pane — a confusing UX. This test pins the bug + the fix.

The test spins up a real `bin/review-local-server.py` against a
hermetic fake tree, pre-populates `.dev-kit/babysit-pr-local-live.log`
with the canonical `running` → `**Verdict:**` → `running` → ...
sequence, opens the page in headless Chromium, waits for the SSE
frames to flush through `applyLineEvents`, and asserts the gate
classes via `page.evaluate()`.

Hermeticity contract:
- `bin/review-local.sh` is a no-op script (the server's `main()`
  refuses to boot without one, but `/tail` is read-only so it is
  never invoked).
- `tools/` is a symlink to the project `tools/` (lets the server
  resolve `tools/review-local-preview.html`).
- `.dev-kit/babysit-pr-local-live.log` is pre-populated with the
  test fixture; the server reads from it.
"""
from __future__ import annotations

import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SERVER = PROJECT_ROOT / "bin" / "review-local-server.py"
HTML = PROJECT_ROOT / "tools" / "review-local-preview.html"


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


# Per-judge sequence ONLY (no bulk `verdicts:` line). This isolates
# the per-judge `**Verdict:**` regex path: the prior implementation
# (PR #772) had a `verdicts:` bulk-update path that worked correctly,
# but the per-judge path silently no-op'd. Without the bulk line, the
# only way for the gate dots to reach `approved` is via the per-judge
# **Verdict:** transition we're fixing in this PR.
LOG_FIXTURE = (
    "  running /dev-kit:review via provider=minimax (dry_run=0)\n"
    "**Verdict:** Approve\n"
    "  running /dev-kit:security via provider=minimax (dry_run=0)\n"
    "**Verdict:** Approve\n"
    "  running /dev-kit:maintenance via provider=minimax (dry_run=0)\n"
    "**Verdict:** Approve\n"
    "  combined verdict: Approve\n"
    "##BABYSIT-DONE exit_code=0##\n"
)


class TestHtmlViewerGateStateTransitions(unittest.TestCase):
    """Pin the per-gate state machine in tools/review-local-preview.html.

    Reproduces the bug fixed in the sibling PR: prior to the fix, every
    gate dot stayed in the `running` class even after its corresponding
    `**Verdict:** Approve` line streamed through. The test asserts the
    dot transitions to `approved` instead.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        # Unittest's addCleanup is per-test; for class-level teardown
        # we drive it ourselves via tearDownClass + a guard.
        cls._tmp_cleaned = False
        fake_root = Path(cls._tmp.name) / "repo"
        fake_root.mkdir()
        (fake_root / "bin").mkdir()
        # Symlink tools/ so PREVIEW_HTML resolves into the real project.
        # Per the hermeticity contract in render_review_local_screenshot.py
        # (which this test mirrors), bin/ is a fresh dir, tools/ is a
        # symlink to the real project tools/. Writes to bin/ are
        # contained in the tmpdir.
        (fake_root / "tools").symlink_to(PROJECT_ROOT / "tools")
        # Stub bin/review-local.sh -- the server's main() refuses to
        # boot without it, but /tail is read-only so the no-op script is never
        # invoked.
        (fake_root / "bin" / "review-local.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        (fake_root / "bin" / "review-local.sh").chmod(
            (fake_root / "bin" / "review-local.sh").stat().st_mode | stat.S_IXUSR
        )
        # COPY (not symlink) the server so __file__ resolves into the
        # fake tree (per render_review_local_screenshot.py).
        shutil.copy(SERVER, fake_root / "bin" / "review-local-server.py")
        # Pre-populate the live log so the server's /tail has content
        # to stream from the moment the page connects.
        (fake_root / ".dev-kit").mkdir()
        (fake_root / ".dev-kit" / "babysit-pr-local-live.log").write_text(
            LOG_FIXTURE, encoding="utf-8"
        )

        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, str(fake_root / "bin" / "review-local-server.py"),
             "--port", str(cls.port), "--bind", "127.0.0.1"],
            cwd=str(fake_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_server("127.0.0.1", cls.port, timeout=5)
        except RuntimeError:
            out = (cls.proc.stdout.read(700).decode("utf-8", errors="replace")
                   if cls.proc.stdout else "")
            raise RuntimeError(f"server boot failed. logs:\n{out}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    def _gate_classes(self) -> dict[str, str]:
        """Open the page, wait for SSE frames to flush, return the class
        attribute of each gate row.
        """
        # Playwright is an optional dependency for this test -- the
        # test's sole purpose is to pin a browser-driven state-machine
        # contract that is otherwise only exercisable via the manual
        # `tools/render_review_local_screenshot.py` capture. CI runs
        # without playwright installed (it's a dev-time tool for the
        # screenshot generator, not a CI runtime dep) so we skip the
        # test body if the import fails. Local runs that have
        # playwright installed (e.g. `pip install playwright &&
        # playwright install chromium`) exercise the real assertions.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed; install via `pip install playwright && playwright install chromium`")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                page = context.new_page()
                page.goto(
                    f"http://127.0.0.1:{self.port}/pr/776?autostart=1",
                    wait_until="domcontentloaded", timeout=10_000,
                )
                # Wait for the iteration_done frame to arrive (signals
                # the server has streamed every fixture line).
                page.wait_for_function(
                    "() => document.getElementById('status').textContent.includes('Approve') "
                    "|| document.getElementById('status').textContent.includes('done')",
                    timeout=5_000,
                )
                # Give the post-status DOM updates a beat to flush.
                page.wait_for_timeout(150)
                return page.evaluate("""() => {
                    const out = {};
                    document.querySelectorAll('[data-gate]').forEach(el => {
                        out[el.getAttribute('data-gate')] = el.className;
                    });
                    return out;
                }""")
            finally:
                browser.close()

    def test_each_gate_dot_transitions_to_approved_after_per_judge_verdict(self) -> None:
        """The fix: each `**Verdict:** Approve` line must transition its
        gate's dot from `running` to `approved`. Prior to the fix, all
        three dots stayed in the `running` class forever.
        """
        classes = self._gate_classes()
        for gate in ("review", "security", "maintenance"):
            with self.subTest(gate=gate):
                self.assertIn(
                    "approved", classes.get(gate, ""),
                    f"gate {gate!r} should be in `approved` class after the "
                    f"`**Verdict:** Approve` line for its judge streamed; "
                    f"got class={classes.get(gate, '<missing>')!r}. "
                    f"all={classes!r}",
                )
                self.assertNotIn(
                    "running", classes.get(gate, ""),
                    f"gate {gate!r} should NOT still be `running` once its "
                    f"judge's verdict line has streamed; "
                    f"got class={classes.get(gate, '<missing>')!r}",
                )


if __name__ == "__main__":
    unittest.main()
