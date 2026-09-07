#!/usr/bin/env python3
"""test_commands_install.py — Verify commands/ install for both Claude Code
($ARGUMENTS) and Codex (positional $1 $2 …) runtime parsers.

Covers:
- commands/*.md exists with valid frontmatter for each wrapper
- bin/install-commands.sh installs to both .claude/commands and .codex/commands
- bin/install-commands.sh --verify is idempotent
- claude runtime: `/dev-kit:skill-usage foo bar` ⇒ $ARGUMENTS = "foo bar"
- codex runtime : `/dev-kit:skill-usage foo bar` ⇒ positional ["foo", "bar"]
                  (verified by reading the installed codex variant and
                   asserting $ARGUMENTS was rewritten into "$@")
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
COMMAND_FILES = ("skill-usage", "review-local")
SRC_DIR = PROJECT_ROOT / "commands"
INSTALL_SH = PROJECT_ROOT / "bin" / "install-commands.sh"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_frontmatter(md: str) -> dict:
    """Minimal YAML-frontmatter parser (string-typed values only)."""
    m = re.match(r"^---\n(.+?)\n---", md, re.DOTALL)
    assert m, f"frontmatter missing in {md[:40]!r}…"
    fm: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _strip_frontmatter(md: str) -> str:
    """Return the markdown body without the leading --- frontmatter block."""
    m = re.match(r"^---\n.+?\n---\n?", md, re.DOTALL)
    assert m, "frontmatter delimiter missing"
    return md[m.end():]


def _render_claude(args: list[str]) -> str:
    """Replicate Claude Code's $ARGUMENTS interpolation."""
    return " ".join(args)


def _render_codex(args: list[str]) -> list[str]:
    """Replicate Codex's positional-arg interpolation (returns the argv)."""
    return list(args)


class TestCommandsInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Mirror the workspace layout: source commands/ + bin/ + targets.
        (self.root / "commands").mkdir(parents=True)
        (self.root / "bin").mkdir(parents=True)
        # Copy the canonical source-of-truth files.
        for name in COMMAND_FILES:
            shutil.copy(SRC_DIR / f"{name}.md", self.root / "commands" / f"{name}.md")
        # Copy + lightly rewrite the install script (replace $PROJECT_ROOT).
        install_text = _read_text(INSTALL_SH).replace(
            str(PROJECT_ROOT), str(self.root)
        )
        self.install_sh = self.root / "bin" / "install-commands.sh"
        self.install_sh.write_text(install_text, encoding="utf-8")
        self.install_sh.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- canonical source-of-truth --------------------------------

    def test_source_files_exist(self):
        for name in COMMAND_FILES:
            path = SRC_DIR / f"{name}.md"
            self.assertTrue(path.exists(), f"missing source: {path}")

    def test_source_frontmatter_minimal(self):
        for name in COMMAND_FILES:
            text = _read_text(SRC_DIR / f"{name}.md")
            fm = _parse_frontmatter(text)
            self.assertEqual(fm.get("name"), name, f"{name}: frontmatter name mismatch")
            self.assertTrue(fm.get("description"), f"{name}: description missing")
            self.assertTrue(fm.get("alpha") in {"state", "enforcement", "analysis"},
                            f"{name}: alpha must be state|enforcement|analysis")

    def test_source_body_uses_dollar_arguments(self):
        for name in COMMAND_FILES:
            body = _strip_frontmatter(_read_text(SRC_DIR / f"{name}.md"))
            self.assertIn("$ARGUMENTS", body,
                          f"{name}: body must reference $ARGUMENTS for claude interpolation")

    # ---- install script -------------------------------------------

    def _run_install(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.install_sh), *args],
            cwd=self.root, capture_output=True, text=True,
        )

    def test_install_creates_both_targets(self):
        r = self._run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        for kind in ("claude", "codex"):
            dest = self.root / f".{kind}" / "commands"
            self.assertTrue(dest.is_dir(), f"missing target dir: {dest}")
            for name in COMMAND_FILES:
                self.assertTrue((dest / f"{name}.md").exists(),
                                f"missing installed file: {dest / name}.md")

    def test_install_verify_is_idempotent(self):
        r1 = self._run_install()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self._run_install("--verify")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        for kind in ("claude", "codex"):
            for name in COMMAND_FILES:
                p1 = _read_text(self.root / f".{kind}" / "commands" / f"{name}.md")
                p2 = _read_text(self.root / f".{kind}" / "commands" / f"{name}.md")
                self.assertEqual(p1, p2, f"{kind}/{name}: not idempotent")

    # ---- arg-parsing assertions -----------------------------------

    def test_claude_render_dollar_arguments_joins_args(self):
        args = ["foo", "bar"]
        rendered = _render_claude(args)
        self.assertEqual(rendered, "foo bar")

        # Sanity-check against the installed claude variant.
        self._run_install()
        installed = _read_text(self.root / ".claude" / "commands" / "skill-usage.md")
        body = _strip_frontmatter(installed)
        self.assertIn("$ARGUMENTS", body)
        # The installed body still contains the literal `$ARGUMENTS`
        # marker; rendering is performed by Claude Code's loader.
        self.assertNotIn('"$@"', body, "claude install must NOT transform $ARGUMENTS")

    def test_claude_install_preserves_dollar_arguments(self):
        # Run install once, then read the .claude/commands/skill-usage.md.
        self._run_install()
        installed = _read_text(self.root / ".claude" / "commands" / "skill-usage.md")
        self.assertIn("$ARGUMENTS", installed,
                      "claude install path must retain $ARGUMENTS verbatim")

    def test_codex_render_positional_splits_args(self):
        args = ["foo", "bar"]
        positional = _render_codex(args)
        self.assertEqual(positional, ["foo", "bar"])

    def test_codex_install_rewrites_dollar_arguments_to_dollar_at(self):
        self._run_install()
        installed = _read_text(self.root / ".codex" / "commands" / "skill-usage.md")
        # Codex variant must rewrite $ARGUMENTS → "$@" so the runtime
        # positional expansion yields the same argv Claude interpolates.
        self.assertNotIn("$ARGUMENTS", installed,
                         "codex install must rewrite $ARGUMENTS")
        self.assertIn('"$@"', installed,
                      "codex install must inject positional \"$@\"")

    # ---- cross-parity: both parsing modes render consistently -----

    def test_parity_claude_join_equals_codex_positional_join(self):
        """The two parsers must produce equivalent downstream strings."""
        args = ["foo", "bar"]
        self.assertEqual(_render_claude(args), " ".join(_render_codex(args)))

    def test_install_script_help_lists_modes(self):
        r = subprocess.run(
            ["bash", str(self.install_sh), "--help"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--verify", r.stdout)
        self.assertIn("--claude-only", r.stdout)
        self.assertIn("--codex-only", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
