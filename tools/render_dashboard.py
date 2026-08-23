#!/usr/bin/env python3
"""Render a token-dashboard HTML to a full-page PNG.

Used to regenerate ``docs/screenshots/token-dashboard-*.png`` from the HTML
emitted by ``tools/token_efficiency_analyzer.py`` so the README preview stays
in sync with the latest analyzer output.

Requires:
- Python 3.10+
- ``playwright`` (``pip install playwright && playwright install chromium``)
- Google Chrome installed at a path Playwright's ``channel="chrome"`` can find
  (e.g. ``/Applications/Google Chrome.app`` on macOS).

Usage::

    python3 tools/render_dashboard.py <html_path> <png_path> [width]

Example (regenerate the screenshot shown in README.md)::

    python3 tools/token_efficiency_analyzer.py \
        --repo "dev-harness-kit" --days 30 \
        --logs-dir fixtures/logs \
        --out docs/observability/dashboard-dev-harness-kit-30d.html
    python3 tools/render_dashboard.py \
        docs/observability/dashboard-dev-harness-kit-30d.html \
        docs/screenshots/token-dashboard-dev-harness-kit-30d.png
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def render(html_path: Path, png_path: Path, viewport_w: int = 1440) -> None:
    """Open *html_path* in headless Chrome and screenshot the full page."""
    url = "file://" + str(html_path.resolve())
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(
            viewport={"width": viewport_w, "height": 900},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(500)  # let any deferred layout settle
        png_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(png_path), full_page=True)
        browser.close()
    print(f"[ok] {png_path}  ({png_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    html = Path(sys.argv[1])
    png = Path(sys.argv[2])
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1440
    render(html, png, width)
