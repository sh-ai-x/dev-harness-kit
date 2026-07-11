#!/usr/bin/env python3
"""
test_write_project_md.py — RED-first tests for write_project_md.py.

Tests cover:
- IRON_LAWS list (5 items, contains L1-L5 keywords)
- render_stub_section_3 = lazy-loading index (canonical file refs, no inline tree)
- render_codebase_map_doc = full 4-section map (Tree, Manifest, Deps, Conventions)
- render_claude_md has §1 §2 §3 §4 §5
- write_project_md atomic (no .tmp leftover)
- write_project_md includes AUTO-GENERATED marker
- write_project_md also writes AGENTS.md
- write_project_md(full_map=True) writes docs/CODEBASE-MAP.md
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import write_project_md  # noqa: E402


class TestWriteProjectMd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_iron_laws_count_and_content(self):
        laws = write_project_md.IRON_LAWS
        self.assertEqual(len(laws), 5)
        self.assertIn("verification artifact", laws[0])
        self.assertIn("reproducing", laws[1])
        self.assertIn("completion claim", laws[2])
        self.assertIn("TODO", laws[3])
        self.assertIn("option", laws[4])

    def test_render_stub_is_lazy_loading_index(self):
        """§3 stub = canonical file refs + opt-in dump command. No inline tree."""
        stub = write_project_md.render_stub_section_3(self.root)
        # Canonical manifest refs
        self.assertIn("package.json", stub)
        self.assertIn("pyproject.toml", stub)
        # Canonical lockfile refs
        self.assertIn("pnpm-lock.yaml", stub)
        # Opt-in dump command
        self.assertIn("--full-claude-md", stub)
        self.assertIn("docs/CODEBASE-MAP.md", stub)
        # NO inline tree dump
        self.assertNotIn("### Tree (depth 4)", stub)
        self.assertNotIn("lib/{", stub)

    def test_render_codebase_map_doc_has_4_sections(self):
        full = write_project_md.render_codebase_map_doc(self.root)
        for tag in ("## Tree", "## Manifest", "## External deps", "## Conventions"):
            self.assertIn(tag, full)

    def test_render_full_section_3_is_alias(self):
        """render_full_section_3 kept for back-compat; aliases render_codebase_map_doc."""
        self.assertEqual(
            write_project_md.render_full_section_3(self.root),
            write_project_md.render_codebase_map_doc(self.root),
        )

    def test_render_agents_md(self):
        """AGENTS.md is a full duplicate of CLAUDE.md content (obra/superpowers pattern):
        Codex-family CLIs read AGENTS.md directly and won't reliably follow a pointer
        to another file, so the complete instructions must live in AGENTS.md itself."""
        agents = write_project_md.render_agents_md(self.root, stage="plan")
        claude = write_project_md.render_claude_md(self.root, stage="plan")
        self.assertEqual(agents, claude)

    def test_render_claude_md_has_all_5_sections(self):
        md = write_project_md.render_claude_md(self.root)
        for section in ("§1", "§2", "§3", "§4", "§5"):
            self.assertIn(section, md)

    def test_render_claude_md_includes_iron_laws(self):
        md = write_project_md.render_claude_md(self.root)
        for i in range(1, 6):
            self.assertIn(f"**L{i}**", md)

    def test_render_claude_md_section_3_is_always_lazy(self):
        """§3 is the lazy-loading index regardless of full_map flag."""
        md_slim = write_project_md.render_claude_md(self.root, full_map=False)
        md_full = write_project_md.render_claude_md(self.root, full_map=True)
        for md in (md_slim, md_full):
            self.assertIn("lazy-loading index", md)
            self.assertNotIn("### Tree (depth 4)", md)
            self.assertIn("--full-claude-md", md)

    def test_render_claude_md_with_stage_override(self):
        md = write_project_md.render_claude_md(self.root, stage="design")
        self.assertIn("current_stage: design", md)

    def test_write_atomic(self):
        p = write_project_md.write_project_md(self.root, full_map=False, stage="plan")
        self.assertTrue(p.exists())
        content = p.read_text()
        self.assertIn("AUTO-GENERATED", content)
        self.assertIn("current_stage: plan", content)
        # No tmp leftover
        leftover = list(self.root.glob(".CLAUDE.md.*.tmp"))
        self.assertEqual(leftover, [])

    def test_write_also_writes_agents_md(self):
        write_project_md.write_project_md(self.root, stage="plan")
        agents_path = self.root / "AGENTS.md"
        claude_path = self.root / "CLAUDE.md"
        self.assertTrue(agents_path.exists())
        # Byte-identical to CLAUDE.md, including the stage that was passed in --
        # not a stale/default re-render (regression guard for the __main__ block's
        # old redundant second write_agents_md(root) call, which used to clobber
        # the correct stage with the default).
        self.assertEqual(agents_path.read_text(), claude_path.read_text())
        self.assertIn("current_stage: plan", agents_path.read_text())

    def test_write_full_map_writes_codebase_map_doc(self):
        write_project_md.write_project_md(self.root, full_map=True, stage="plan")
        doc_path = self.root / "docs" / "CODEBASE-MAP.md"
        self.assertTrue(doc_path.exists())
        content = doc_path.read_text()
        self.assertIn("## Tree", content)
        self.assertIn("## Manifest", content)
        # CLAUDE.md §3 must still be the lazy-loading index
        claude = (self.root / "CLAUDE.md").read_text()
        self.assertIn("lazy-loading index", claude)

    def test_write_default_skips_codebase_map_doc(self):
        write_project_md.write_project_md(self.root, full_map=False, stage="plan")
        doc_path = self.root / "docs" / "CODEBASE-MAP.md"
        self.assertFalse(doc_path.exists())

    def test_write_overwrites_cleanly(self):
        write_project_md.write_project_md(self.root, stage="plan")
        write_project_md.write_project_md(self.root, stage="design")
        content = (self.root / "CLAUDE.md").read_text()
        self.assertIn("current_stage: design", content)
        self.assertNotIn("current_stage: plan", content.replace("current_stage: design", ""))

    def test_codebase_map_doc_filters_credentials_and_dotfiles(self):
        """CODEBASE-MAP.md must not leak `.git/` or `x-access-token:...@` credentials."""
        # Create fake "credential" directory + .git file at root of tmp
        cred_dir = self.root / "https:"
        cred_dir.mkdir()
        (cred_dir / "x-access-token:fake-pat@github.com").mkdir()
        # Create a fake .git worktree-pointer file
        (self.root / ".git").write_text("gitdir: /tmp/fake/.git/worktrees/x")
        write_project_md.write_project_md(self.root, full_map=True, stage="plan")
        content = (self.root / "docs" / "CODEBASE-MAP.md").read_text()
        self.assertNotIn("x-access-token", content)
        self.assertNotIn("fake-pat", content)
        # .git as a top-level file should be filtered (worktree pointer)
        self.assertNotIn("\n  .git\n", content)
        self.assertNotIn("\n  .git/", content)

    def test_safe_deps_redacts_credentialed_registry_urls(self):
        """_safe_deps must redact x-access-token:...@ URLs in lockfile lines."""
        # Fake requirements.txt with a credentialed index URL
        (self.root / "requirements.txt").write_text(
            "# Sample lockfile\n"
            "--index-url https://x-access-token:fake-pat@pypi.example.com/simple\n"
            "requests==2.31.0\n"
        )
        out = write_project_md._safe_deps(self.root)
        self.assertNotIn("fake-pat", out)
        self.assertNotIn("x-access-token", out)
        self.assertIn("requests==2.31.0", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)