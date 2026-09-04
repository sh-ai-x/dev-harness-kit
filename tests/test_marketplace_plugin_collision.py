#!/usr/bin/env python3
"""test_marketplace_plugin_collision.py — regression test for ADR-0023.

Issue #783 (2026-09-01) saw commit `e296ce6a` force-push a sibling plugin
(`dev-kit-lite`) into `.claude-plugin/marketplace.json` without PR review.
This test makes that class of failure detectable at CI time:

1. Every entry's `name` is unique within a single `marketplace.json`.
2. Every entry's skill-prefix namespace is disjoint from every other entry's.
   Two entries with names `foo` and `foo-bar` are flagged because the
   `foo:` prefix namespace silently overlaps with `foo-bar:` from the
   user's perspective (Claude Code installs both → both expose skills
   under `foo:*` and `foo-bar:*`, and consumers can't tell which they
   actually invoked).
3. The first plugin entry's `name` equals the marketplace's `name` field
   (the marketplace ships its own canonical plugin first; side-loaded
   siblings go after, and any first-slot mismatch is a smell).
4. No plugin entry in any marketplace.json under the dev-harness-kit
   tree may point at a repo outside the dev-harness-kit org
   (`sh-ai-x/dev-harness-kit*` family) unless an ADR reference is
   documented in the test fixtures.

Run as:
    pytest tests/test_marketplace_plugin_collision.py
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _load_marketplace(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_marketplaces(root: Path):
    """Yield every marketplace.json under the repo root."""
    yield root / ".claude-plugin" / "marketplace.json"
    # Templates are not shipped at runtime, but they should also follow
    # the rule (a stale template would re-introduce the bug on next
    # /dev-kit:ci-setup).
    for p in root.rglob("marketplace.json"):
        if p == root / ".claude-plugin" / "marketplace.json":
            continue
        if any(part.startswith(".") and part not in (".claude-plugin",) for part in p.parts):
            continue
        if "node_modules" in p.parts:
            continue
        yield p


class TestMarketplacePluginCollision(unittest.TestCase):
    """ADR-0023: marketplace scope policy."""

    def test_first_plugin_name_matches_marketplace_name(self):
        """Marketplace `name` must equal first plugin entry's `name`.

        Otherwise the marketplace ships a canonical-name discrepancy:
        users browse `dev-kit` but install whatever the first entry
        happens to be. The dev-kit marketplace currently ships dev-kit
        as entry 0; a regression here means the slot order was
        inverted.
        """
        for mp_path in _iter_marketplaces(PROJECT_ROOT):
            with self.subTest(marketplace=str(mp_path)):
                if not mp_path.exists():
                    continue
                mp = _load_marketplace(mp_path)
                plugins = mp.get("plugins", [])
                self.assertGreaterEqual(len(plugins), 1, f"{mp_path} has no plugin entries")
                self.assertEqual(
                    mp["name"],
                    plugins[0]["name"],
                    f"{mp_path} marketplace name='{mp['name']}' != "
                    f"first plugin name='{plugins[0]['name']}'",
                )

    def test_plugin_names_unique_within_marketplace(self):
        """No two plugin entries may share a `name`."""
        for mp_path in _iter_marketplaces(PROJECT_ROOT):
            with self.subTest(marketplace=str(mp_path)):
                if not mp_path.exists():
                    continue
                mp = _load_marketplace(mp_path)
                names = [p["name"] for p in mp.get("plugins", [])]
                self.assertEqual(
                    len(names),
                    len(set(names)),
                    f"{mp_path} has duplicate plugin names: {names}",
                )

    def test_plugin_prefix_namespace_disjoint(self):
        """No two plugin entries may have overlapping prefix namespaces.

        Names like `dev-kit` and `dev-kit-lite` overlap because the
        `dev-kit:` prefix is a strict prefix of `dev-kit-lite:` from
        the user's mental-model standpoint (both feel like 'the dev-kit
        plugin' once installed). Two plugins claiming overlapping
        prefixes cause skill-resolution ambiguity at install time.
        """
        for mp_path in _iter_marketplaces(PROJECT_ROOT):
            with self.subTest(marketplace=str(mp_path)):
                if not mp_path.exists():
                    continue
                mp = _load_marketplace(mp_path)
                plugins = mp.get("plugins", [])
                # Treat `dev-kit` and `dev-kit-lite` as overlapping if
                # one is a `-`-separated suffix of the other. We allow
                # unrelated short names (a, b) to coexist.
                collisions = []
                for i, a in enumerate(plugins):
                    for b in plugins[i + 1 :]:
                        if _prefixes_overlap(a["name"], b["name"]):
                            collisions.append((a["name"], b["name"]))
                self.assertEqual(
                    collisions,
                    [],
                    f"{mp_path} has overlapping plugin prefixes: {collisions}",
                )

    def test_canonical_plugin_first_and_others_are_dev_harness_kit_family(self):
        """First plugin is dev-harness-kit; any sibling must point at the
        dev-harness-kit repo (or carry an ADR-doc exception).

        ADR-0023 §1 says the dev-harness-kit marketplace exposes only
        the dev-harness-kit plugin. A regression here means a sibling
        was registered without an ADR update.
        """
        for mp_path in _iter_marketplaces(PROJECT_ROOT):
            with self.subTest(marketplace=str(mp_path)):
                if not mp_path.exists():
                    continue
                mp = _load_marketplace(mp_path)
                if mp["name"] != "dev-kit":
                    # Other marketplaces are out of scope for this test.
                    continue
                plugins = mp.get("plugins", [])
                for entry in plugins:
                    src = entry.get("source", {})
                    url = src.get("url", "") if isinstance(src, dict) else ""
                    self.assertRegex(
                        url,
                        r"^https://github\.com/sh-ai-x/dev-harness-kit(/|$)",
                        f"{mp_path} plugin '{entry['name']}' points outside "
                        f"the dev-harness-kit family: {url!r}. Per ADR-0023 §1 "
                        f"this requires an explicit ADR (see docs/adr/); "
                        f"update this test's regex only after the ADR is merged.",
                    )


_PREFIX_SEP = re.compile(r"[-_]")


def _prefixes_overlap(a: str, b: str) -> bool:
    """Return True if `a` and `b` share an overlapping prefix namespace.

    Conservative rule — when in doubt, flag. The user reports
    ambiguity when two plugin names are hyphen/underscore-separated
    refinements of the same root token.
    """
    if a == b:
        return True
    tokens_a = _PREFIX_SEP.split(a)
    tokens_b = _PREFIX_SEP.split(b)
    # Shared root token AND one is a strict prefix of the other.
    if tokens_a[0] == tokens_b[0] and len(tokens_a) != len(tokens_b):
        return True
    return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
