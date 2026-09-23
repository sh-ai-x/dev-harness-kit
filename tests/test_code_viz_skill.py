#!/usr/bin/env python3
"""test_code_viz_skill.py — regression test for tools/code_viz.py.

Validates the skill workflow end-to-end against a synthetic sandbox:

1. The executable tool compiles standalone and remains the visualizer SSOT.
2. The skill runs to completion: exits 0, prints all 5 validator labels
   on stdout (catches silent validator failures and NameError-on-`svgs` in
   the parent scope after my fix to the cosmetic glitch).
3. The HTML is emitted with at least 2 Mermaid blocks and the lightbox
   modal markup (catches HTML regression).
4. Two runs on the same target produce byte-identical HTML (modulo the
   embedded timestamp), confirming the skill is deterministic on real input.
5. The "Syntax error in text" Playwright signal fires on a known-broken
   stateDiagram-v2 (catches if the validator's check ever becomes
   decorative vs. real).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

# Playwright-driven tests (skill validator + meta-test) require the
# playwright Python package AND the headless chromium binary. Skip the
# whole module when either is missing so CI without browser tooling still
# runs the tool-compile + structural assertions; the validator
# subprocess and the modal-click meta-test are local-environment concerns.
pytest.importorskip("playwright", reason="playwright not installed; skipping Playwright-dependent tests")


def _chromium_headless_shell_available() -> bool:
    """True iff the playwright chromium-headless-shell binary is on disk.

    The Python wheel may be installed while the matching browser build
    (downloaded separately via `playwright install chromium`) is not —
    common in CI and in minimal dev containers. Walks the platform
    cache (`~/Library/Caches/ms-playwright/` on macOS,
    `~/.cache/ms-playwright/` on Linux) and looks for any
    `chromium_headless_shell-*/chrome-{mac,linux}/headless_shell`
    binary, version-agnostic so a Playwright upgrade does not break
    the gate.
    """
    home = Path.home()
    for cache_root in (home / "Library/Caches/ms-playwright",
                       home / ".cache" / "ms-playwright"):
        if not cache_root.exists():
            continue
        for d in cache_root.iterdir():
            if not d.name.startswith("chromium_headless_shell-"):
                continue
            for binary in (d / "chrome-mac" / "headless_shell",
                           d / "chrome-linux" / "headless_shell"):
                if binary.exists():
                    return True
    return False


if not _chromium_headless_shell_available():
    pytest.skip(
        "playwright chromium-headless-shell binary missing; "
        "run `playwright install chromium` to enable the "
        "code-viz validator tests (env gap, not a code defect)",
        allow_module_level=True,
    )

PROJECT_ROOT = Path(__file__).parent.parent
TOOL_FILE = PROJECT_ROOT / "tools" / "code_viz.py"


def _extract_tool() -> str:
    """Read the executable visualizer source of truth."""
    return TOOL_FILE.read_text(encoding="utf-8")


def _build_sandbox(tmpdir: Path) -> Path:
    """Create a small synthetic target directory the skill can analyze."""
    (tmpdir / "src").mkdir()
    (tmpdir / "src" / "main.py").write_text("print('hello')\n")
    (tmpdir / "src" / "utils.py").write_text("def add(a, b): return a + b\n")
    (tmpdir / "tests").mkdir()
    (tmpdir / "tests" / "test_main.py").write_text("# tests here\n")
    (tmpdir / "README.md").write_text("# Test Repo\n")
    (tmpdir / "plugin.json").write_text('{"name": "demo"}')
    return tmpdir


def _run_skill(sandbox: Path, out_html: Path) -> subprocess.CompletedProcess:
    """Run the visualizer tool in a fresh python subprocess, with
    --target and --out wired to the test fixtures."""
    return subprocess.run(
        [sys.executable, str(TOOL_FILE), f"--target={sandbox}", f"--out={out_html}"],
        capture_output=True, text=True, timeout=120,
    )


def _strip_timestamp_meta(text: str) -> str:
    """Normalize the embedded <p class="meta"> timestamp so two same-day
    runs compare equal under the determinism test."""
    return re.sub(
        r"<p class=\"meta\">.*?</p>",
        '<p class="meta">[TIMESTAMP_STRIPPED]</p>',
        text,
        flags=re.S,
    )


class CodeVizSkillTests(unittest.TestCase):
    """Regression tests for the code-viz tool contract."""

    def setUp(self):
        self.sandbox = _build_sandbox(Path(tempfile.mkdtemp(prefix="code_viz_test_")))
        self.tmp = Path(tempfile.mkdtemp(prefix="code_viz_out_"))

    def tearDown(self):
        shutil.rmtree(self.sandbox, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tool_compiles(self):
        """The executable tool must parse cleanly before it is invoked."""
        body = _extract_tool()
        try:
            compile(body, "<code-viz-tool>", "exec")
        except SyntaxError as e:
            self.fail(f"code_viz.py has a SyntaxError: {e}\n\nbody:\n{body}")

    def test_skill_runs_to_completion(self):
        """The skill exits 0 with all 5 validator labels on stdout and emits
        HTML with ≥2 mermaid blocks + modal markup. Regression for: silent
        validator failures (subprocess rc=1, empty stdout) and the
        NameError-on-`svgs` cosmetic glitch in the post-validation summary."""
        out = self.tmp / "viz.html"
        r = _run_skill(self.sandbox, out)
        # Validator subprocess actually ran (not silently crashed):
        self.assertIn("body_syntax_error=False", r.stdout,
                      f"validator did not run cleanly:\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")
        self.assertIn("svgs=", r.stdout)
        self.assertIn("modal_open=True", r.stdout)
        # Parent script printed the final report (catches NameError on `svgs`):
        self.assertIn("[code-viz] validation:", r.stdout,
                      "parent script crashed before printing the final report line — "
                      "likely a NameError on `svgs` (subprocess scope leak)")
        self.assertIn("[code-viz] wrote", r.stdout)
        # HTML emitted with required structure:
        self.assertTrue(out.exists(), "HTML output file not written")
        text = out.read_text()
        mermaid_count = text.count('class="mermaid"')
        self.assertGreaterEqual(
            mermaid_count, 2,
            f"expected ≥2 mermaid blocks in HTML; got {mermaid_count}")
        self.assertIn('class="mermaid-modal"', text,
                      "lightbox modal markup missing — click-to-expand broken")

    def test_validator_signal_catches_broken_mermaid(self):
        """Hard-coded regression: a `:` inside a stateDiagram-v2 transition label
        causes 'Syntax error in text' to appear in body.innerText. This is the
        single signal the code-viz validator checks; if it ever stops firing, the
        skill's validation is decorative."""
        html = (
            "<!doctype html><html><body>"
            '<pre class="mermaid">stateDiagram-v2\n'
            "  [*] --> A\n"
            "  A --> B: lib/foo.py:130 broken\n"
            "</pre>"
            '<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>'
            "</body></html>"
        )
        broken = self.tmp / "broken.html"
        broken.write_text(html)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page()
            page.goto(f"file://{broken}", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(1500)
            body = page.evaluate("() => document.body.innerText")
            b.close()
        self.assertIn(
            "Syntax error in text", body,
            "validator signal regressed: a known-broken mermaid did NOT produce "
            "'Syntax error in text' in body.innerText; the code-viz skill's "
            "validator would silently pass broken HTML")

    def test_two_runs_deterministic(self):
        """Same target + same --out → byte-identical HTML across runs, modulo
        the embedded <p class='meta'> timestamp (which falls in the same
        minute for sub-second test runs)."""
        out1 = self.tmp / "viz1.html"
        out2 = self.tmp / "viz2.html"
        r1 = _run_skill(self.sandbox, out1)
        r2 = _run_skill(self.sandbox, out2)
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        a = _strip_timestamp_meta(out1.read_text())
        b = _strip_timestamp_meta(out2.read_text())
        self.assertEqual(a, b, "HTML output is non-deterministic across runs on "
                              "the same input (modulo timestamp)")

    def test_target_path_with_special_chars(self):
        """Sandbox dir whose name contains hyphen + digits + dot still parses
        through the skill. Catches cwd-stringification bugs."""
        weird = Path(tempfile.mkdtemp(prefix="my.codex-special_name-42-"))
        try:
            (weird / "x.md").write_text("hi\n")
            out = self.tmp / "viz.html"
            r = _run_skill(weird, out)
            self.assertEqual(
                r.returncode, 0,
                f"skill failed on a sandbox path with special chars:\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")
            self.assertTrue(out.exists())
        finally:
            shutil.rmtree(weird, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
