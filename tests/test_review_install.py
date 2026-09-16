#!/usr/bin/env python3
"""test_review_install.py — regression tests for templates/ci/.github/workflows/review.yml
(and the dev-harness-kit repo's own .github/workflows/review.yml).

Phase 4 gates-distribution migration (issue #12, sh-ai-x/dev-harness-kit-gates):
the "Install dev-kit plugin" self-aware install step (symlink for
self-install, `git clone` for consumer-install) that used to live inline
in the TEMPLATE has moved into the reusable workflow at
sh-ai-x/dev-harness-kit-gates/.github/workflows/review.yml. The template
is now a thin wrapper that `uses:` the reusable and forwards GitHub
context + this repo's own vars/secrets — see the gates repo's own test
suite (tests/test_workflow_call_inputs.py) for coverage of the install
step's self/consumer-install branching, since that logic now lives there.

This file's coverage after the migration:
  1. The TEMPLATE `uses:` the gates repo's review.yml reusable, pinned
     to an explicit tag (never a floating/unpinned ref).
  2. The TEMPLATE forwards `install_token` (mapped from
     `secrets.DEV_KIT_GITHUB_TOKEN`) so the reusable's own consumer-install
     branch can clone this repo when the caller isn't the plugin itself.
  3. The dev-harness-kit repo's OWN `.github/workflows/review.yml` is
     UNTOUCHED by this migration (self-install only, still symlinks
     inline) — deliberately out of scope for issue #12, which only
     replaces the CONSUMER TEMPLATE other repos install via ci-setup.
  4. `.claude-plugin/marketplace.json` still points at a valid,
     public dev-harness-kit source (unrelated to the install-step
     migration, kept in this file for historical co-location).

Run as:
  pytest tests/test_review_install.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE_REVIEW_YML = REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "review.yml"
OWN_REVIEW_YML = REPO_ROOT / ".github" / "workflows" / "review.yml"


class TestTemplateUsesGatesReusable(unittest.TestCase):
    """The TEMPLATE is a thin wrapper around the gates repo's reusable
    review.yml (Phase 4 migration, issue #12) — it no longer contains
    install logic of its own."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = TEMPLATE_REVIEW_YML.read_text(encoding="utf-8")

    def test_no_inline_install_step(self) -> None:
        """The self-aware 'Install dev-kit plugin' step must NOT exist in
        the template anymore — that logic lives in the gates repo now."""
        self.assertNotIn(
            "Install dev-kit plugin", self.body,
            "template still has an inline install step — the Phase 4 "
            "migration should have removed it in favor of `uses:` the "
            "gates repo's reusable review.yml",
        )

    def test_uses_gates_repo_reusable_pinned_to_a_tag(self) -> None:
        """Must `uses:` sh-ai-x/dev-harness-kit-gates/.github/workflows/review.yml
        pinned to an explicit tag (`@vX.Y.Z` or `@vX`), never a bare branch
        name or unpinned ref."""
        m = re.search(
            r"uses:\s*sh-ai-x/dev-harness-kit-gates/\.github/workflows/review\.yml@(\S+)",
            self.body,
        )
        self.assertIsNotNone(
            m, "template must `uses:` sh-ai-x/dev-harness-kit-gates's reusable review.yml"
        )
        ref = m.group(1)
        self.assertRegex(
            ref, r"^v\d+(\.\d+){0,2}$",
            f"gates repo ref must be a version tag (e.g. v1 or v1.2.3), got {ref!r}",
        )

    def test_forwards_install_token_from_dev_kit_github_token(self) -> None:
        """The reusable's consumer-install branch (when the caller isn't
        the plugin itself) needs a PAT with contents:read on
        sh-ai-x/dev-harness-kit — forwarded from this repo's own
        DEV_KIT_GITHUB_TOKEN secret, same name as before the migration."""
        self.assertIn(
            "install_token: ${{ secrets.DEV_KIT_GITHUB_TOKEN }}", self.body,
            "template must forward secrets.DEV_KIT_GITHUB_TOKEN as install_token",
        )

    def test_preserves_minimax_api_key_secret_name(self) -> None:
        """MINIMAX_API_KEY is preserved from the prior workflow — renaming
        it would break every existing consumer repo's secret config."""
        self.assertIn(
            "secrets.MINIMAX_API_KEY", self.body,
            "template must still reference secrets.MINIMAX_API_KEY by that exact name",
        )


class TestReviewYmlStructure(unittest.TestCase):
    """The TEMPLATE is what consumer repos get via ci-setup. The
    dev-harness-kit repo's OWN review.yml is a SEPARATE, untouched file —
    issue #12 only replaces the consumer template, not this repo's own
    live CI (a deliberately narrower, lower-risk first step)."""

    def test_both_files_exist(self) -> None:
        self.assertTrue(TEMPLATE_REVIEW_YML.exists(), f"missing: {TEMPLATE_REVIEW_YML}")
        self.assertTrue(OWN_REVIEW_YML.exists(), f"missing: {OWN_REVIEW_YML}")

    def test_own_workflow_can_stay_self_install_only(self) -> None:
        """The dev-harness-kit repo's own workflow is untouched by the
        Phase 4 migration — it still symlinks the local checkout inline
        (workspace IS the dev-kit plugin, so self-install always works;
        no gates-repo dependency needed for this repo's own CI)."""
        if not OWN_REVIEW_YML.exists():
            self.skipTest("own review.yml not present")
        body = OWN_REVIEW_YML.read_text(encoding="utf-8")
        self.assertIn(
            "ln -sfn", body,
            "own workflow should at minimum symlink the local checkout",
        )

    def test_own_workflow_unchanged_by_this_migration(self) -> None:
        """Sanity: the own workflow must NOT `uses:` the gates repo --
        that would mean issue #12's scope crept into dev-harness-kit's
        own live CI, which is explicitly out of scope (separate, higher-
        risk decision left for later)."""
        if not OWN_REVIEW_YML.exists():
            self.skipTest("own review.yml not present")
        body = OWN_REVIEW_YML.read_text(encoding="utf-8")
        self.assertNotIn(
            "sh-ai-x/dev-harness-kit-gates", body,
            "own workflow must not reference the gates repo — issue #12 "
            "only replaces the CONSUMER TEMPLATE, not this repo's own CI",
        )


class TestMarketplaceJsonSource(unittest.TestCase):
    """.claude-plugin/marketplace.json source must be a valid schema
    form pointing at the public dev-harness-kit source. The schema
    allows 3 forms:
      1. "./path"           (relative — works for local install)
      2. {"source":"npm",...} (NPM)
      3. {"source":"url","url":...,"ref":...} (git URL — for distribution)
    A bare string like "https://github.com/..." is INVALID and causes
    'source type your Claude Code version does not support' on install.
    """

    def test_marketplace_source_is_valid_schema_form(self) -> None:
        import json
        m = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        src = m["plugins"][0]["source"]
        if isinstance(src, str):
            self.assertRegex(
                src, r"^\./.*",
                f"bare string source must start with './' (got: {src!r})",
            )
        elif isinstance(src, dict):
            self.assertIn(
                src.get("source"), ("npm", "url"),
                f"object source must have 'source': 'npm' or 'url' (got: {src!r})",
            )
        else:
            self.fail(f"marketplace.json source has unrecognized form: {src!r}")

    def test_marketplace_source_points_at_dev_harness_kit(self) -> None:
        import json
        m = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        src = m["plugins"][0]["source"]
        if isinstance(src, str):
            url = src
        else:
            url = src.get("url", "")
        self.assertIn(
            "dev-harness-kit", url,
            f"marketplace.json source must point at the dev-harness-kit repo, got: {url!r}",
        )

    def test_marketplace_plugin_version_present(self) -> None:
        """feat/skill-versions: `plugin.json` MUST declare `version:` (single
        source of truth restored from PR #31's removal). Per-skill version
        bookkeeping was dropped (DRY) so this is the ONLY version field
        the project maintains for an installed plugin's release tag.
        """
        import json
        p = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
        v = p.get("version")
        self.assertIsNotNone(v, "plugin.json must declare `version:` (the canonical plugin-level version)")
        self.assertRegex(v or "", r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$",
                         f"plugin.json:version={v!r} is not valid semver")


if __name__ == "__main__":
    unittest.main(verbosity=2)
