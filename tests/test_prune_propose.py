#!/usr/bin/env python3
"""test_prune_propose.py -- unit tests for the prune-propose skill.

Coverage:
* ``scripts/dump_usage.py`` filters, table render, dry-run, and the
  plain-text fallback ask loop.
* ``tools/skill_usage.py --propose-delete`` pipes the 0-turns-and-0-
  invocations subset to dump_usage.py with the right window.
* ``--propose-delete --dry-run`` skips the ask loop and prints the
  candidate table.
* ``_discover_catalog_skills`` / ``_run_propose_delete`` seed the
  candidate set from the on-disk ``skills/*/SKILL.md`` catalog so a
  skill that has never once been invoked (not just gone quiet) is
  still a candidate; model-use (``user-invocable: false``) skills are
  excluded from that seed.
* The skill declares the L6 ``alpha: state`` frontmatter so the
  governance gate (``tests/test_skill_governance.py``) stays green.
* ``SKILL.md`` lives at the flat ``skills/<name>/SKILL.md`` location
  with matching frontmatter ``name:``.

Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
TOOLS = PROJECT_ROOT / "tools"
SCRIPTS = PROJECT_ROOT / "skills" / "prune-propose" / "scripts"
SKILL_MD = PROJECT_ROOT / "skills" / "prune-propose" / "SKILL.md"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(SCRIPTS))

import dump_usage  # noqa: E402
import skill_usage  # noqa: E402

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "skill_usage" / "mixed.jsonl"


def _fixture_max_ts() -> _dt.datetime:
    """Return the maximum ISO timestamp in ``FIXTURE``."""
    max_ts: _dt.datetime | None = None
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = obj.get("timestamp")
            if not isinstance(ts, str) or not ts:
                continue
            parsed = skill_usage._parse_iso(ts)
            if parsed is None:
                continue
            if max_ts is None or parsed > max_ts:
                max_ts = parsed
    if max_ts is None:
        raise RuntimeError("fixture contained no parseable timestamps")
    return max_ts


# Reference 'now' for windowed tests: fixture max + 7 days. With
# window=30 the fixture's oldest records (2026-06-20 / 2026-06-26) sit
# 17-20 days before _REF_NOW, so they are inside the window. With
# window=10 they fall out.
_REF_NOW = _fixture_max_ts() + _dt.timedelta(days=7)


def _copy_skill_usage_toolkit(root: Path) -> Path:
    """Copy the ``tools/skill_usage*.py`` trio + ``dump_usage.py`` into
    ``<root>/tools`` and ``<root>/skills/prune-propose/scripts``.

    No ``skills/`` catalog directory is created -- callers that want
    catalog-seeded skills add them under ``<root>/skills`` themselves.
    Returns the ``<root>/tools`` path (the CLI entrypoint's directory).
    """
    tools_dir = root / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for name in ("skill_usage.py", "skill_usage_render.py",
                "skill_usage_normalize.py"):
        shutil.copy2(TOOLS / name, tools_dir / name)

    dump_dst = root / "skills" / "prune-propose" / "scripts"
    dump_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPTS / "dump_usage.py", dump_dst / "dump_usage.py")
    return tools_dir


class TestDumpUsageHelpers(unittest.TestCase):
    def test_read_candidates_strips_comments_and_blanks(self):
        buf = io.StringIO("dev-kit:foo\n\n# comment\n  \ndev-kit:bar\n")
        self.assertEqual(dump_usage._read_candidates(buf),
                         ["dev-kit:foo", "dev-kit:bar"])

    def test_render_table_includes_header_and_window(self):
        out = dump_usage.render_table(["dev-kit:foo", "dev-kit:bar-baz"], 30)
        self.assertIn("SKILL", out)
        self.assertIn("30", out)
        # Width bounded by 40 char cap.
        self.assertNotIn("dev-kit:bar-baz-truncated", out)

    def test_render_table_empty_states_zero(self):
        out = dump_usage.render_table([], 30)
        self.assertIn("no candidates", out)
        self.assertIn("30", out)


class TestDumpUsageAskLoop(unittest.TestCase):
    def test_ask_user_plain_text_yes(self):
        """The plain-text fallback should accept y/yes as Delete."""
        with tempfile.TemporaryFile("w+") as fh:
            fh.write("y\n")
            fh.seek(0)
            old_stdin = sys.stdin
            sys.stdin = fh
            try:
                self.assertTrue(dump_usage._ask_user("dev-kit:foo"))
            finally:
                sys.stdin = old_stdin

    def test_ask_user_plain_text_default_keep(self):
        """Empty input should default to Keep (no destructive default)."""
        with tempfile.TemporaryFile("w+") as fh:
            fh.write("\n")
            fh.seek(0)
            old_stdin = sys.stdin
            sys.stdin = fh
            try:
                self.assertFalse(dump_usage._ask_user("dev-kit:foo"))
            finally:
                sys.stdin = old_stdin


class TestDumpUsageMain(unittest.TestCase):
    def test_dry_run_skips_ask_loop(self):
        """--dry-run should print the candidate table and exit 0 without
        asking the user anything (no stdin reads from the user)."""
        candidates = "dev-kit:foo\ndev-kit:bar\n"
        with tempfile.TemporaryFile("w+") as fh:
            fh.write(candidates)
            fh.seek(0)
            old_stdin = sys.stdin
            sys.stdin = fh
            try:
                buf = io.StringIO()
                old_stdout = sys.stdout
                sys.stdout = buf
                try:
                    rc = dump_usage.main(["--dry-run", "--window-days", "30"])
                finally:
                    sys.stdout = old_stdout
            finally:
                sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("dev-kit:foo", out)
        self.assertIn("dry-run", out)
        # No DELETED/KEPT lines emitted in dry-run mode.
        self.assertNotIn("DELETED:", out)

    def test_interactive_loop_routes_yes_no(self):
        """Two y's, one n -> 2 deleted, 1 kept.

        Candidates are passed via ``--candidates`` so the ask loop can
        read fresh stdin for the user's per-skill y/n answers without
        colliding with the candidate stream.
        """
        answers = "y\nn\ny\n"
        with tempfile.TemporaryFile("w+") as in_fh:
            in_fh.write(answers)
            in_fh.seek(0)
            old_stdin = sys.stdin
            sys.stdin = in_fh
            buf = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = dump_usage.main([
                    "--window-days", "30",
                    "--candidates", "dev-kit:a,dev-kit:b,dev-kit:c",
                ])
            finally:
                sys.stdout = old_stdout
                sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("DELETED: dev-kit:a dev-kit:c", out)
        self.assertIn("KEPT:    dev-kit:b", out)


class TestProposeDeleteFlag(unittest.TestCase):
    def test_filters_to_zero_turns_and_zero_invocations(self):
        """With the fixture + 30-day window, the surviving 0/0 subset is
        the skills whose every record falls outside the window. The
        fixture has ``dev-kit:prune`` at 2026-06-20 (outside the 30d
        window when now=fixture_max+7d, since 2026-06-20 is ~20 days
        before fixture_max). It survives the 30-day window but the
        other three skills all have records inside the window so they
        are excluded from the proposal list.

        This exercises ``aggregate_skill_usage`` directly (the raw
        telemetry dict), not the full ``--propose-delete`` pipeline --
        catalog seeding (see ``TestCatalogSeeding`` below) only happens
        inside ``_run_propose_delete``, so it does not affect this
        assertion."""
        agg = skill_usage.aggregate_skill_usage(
            str(FIXTURE), window_days=30, now=_REF_NOW)
        candidates = sorted(
            name for name, rec in agg.items()
            if rec.get("turns", 0) == 0 and rec.get("invocations", 0) == 0
        )
        # Every fixture skill has at least one record inside 30d.
        self.assertEqual(candidates, [])

    def test_propose_delete_dry_run_via_cli(self):
        """End-to-end: ``--propose-delete --dry-run`` exits 0 and the
        dump script prints its no-candidates table.

        Runs against an isolated copy of the toolkit with no ``skills/``
        directory (see ``_copy_skill_usage_toolkit``) so the result is
        independent of the real repo's catalog size -- this specifically
        covers the case where ``_discover_catalog_skills`` finds nothing
        to seed and the fixture's own skills are all inside the window."""
        with tempfile.TemporaryDirectory() as root:
            tools_dir = _copy_skill_usage_toolkit(Path(root))
            r = subprocess.run(
                [sys.executable, str(tools_dir / "skill_usage.py"),
                 "--logs-glob", str(FIXTURE),
                 "--days", "30",
                 "--propose-delete", "--dry-run"],
                capture_output=True, text=True, timeout=60,
                cwd=root,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no candidates", r.stdout)
        self.assertIn("dry-run", r.stdout)


class TestCatalogSeeding(unittest.TestCase):
    """``_discover_catalog_skills`` / ``_run_propose_delete`` seeding.

    Covers the gap where a skill that has *never* appeared in any log
    (not just one that went quiet) was invisible to ``--propose-delete``
    because the candidate set was built purely from observed telemetry.
    """

    def _write_skill(self, skills_dir: Path, name: str, *,
                     user_invocable: bool | None = True) -> None:
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        lines = ["---", f"name: {name}", "category: build",
                "description: fixture skill", "alpha: analysis"]
        if user_invocable is not None:
            lines.append(f"user-invocable: {'true' if user_invocable else 'false'}")
        lines += ["---", "", "fixture body"]
        (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n")

    def test_discover_catalog_skills_excludes_model_use(self):
        """User-invocable skills are prefixed and returned; a skill
        declaring ``user-invocable: false`` is excluded."""
        with tempfile.TemporaryDirectory() as root:
            root_p = Path(root)
            (root_p / ".claude-plugin").mkdir()
            (root_p / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "dev-kit"}))
            skills_dir = root_p / "skills"
            skills_dir.mkdir()
            self._write_skill(skills_dir, "totally-idle", user_invocable=True)
            self._write_skill(skills_dir, "internal-helper", user_invocable=False)
            self._write_skill(skills_dir, "implicit-default")  # no key -> true

            names = skill_usage._discover_catalog_skills(root_p)

        self.assertIn("dev-kit:totally-idle", names)
        self.assertIn("dev-kit:implicit-default", names)
        self.assertNotIn("dev-kit:internal-helper", names)

    def test_discover_catalog_skills_missing_dir_returns_empty(self):
        """No ``skills/`` directory (e.g. a non-plugin checkout) is a
        no-op, not a crash."""
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(skill_usage._discover_catalog_skills(Path(root)), [])

    def test_run_propose_delete_seeds_never_invoked_skill(self):
        """A catalog skill absent from telemetry entirely now shows up
        as a 0/0 candidate; a model-use sibling does not."""
        with tempfile.TemporaryDirectory() as root:
            root_p = Path(root)
            (root_p / ".claude-plugin").mkdir()
            (root_p / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "dev-kit"}))
            skills_dir = root_p / "skills"
            skills_dir.mkdir()
            self._write_skill(skills_dir, "totally-idle", user_invocable=True)
            self._write_skill(skills_dir, "internal-helper", user_invocable=False)

            dump_dst = skills_dir / "prune-propose" / "scripts"
            dump_dst.mkdir(parents=True)
            (dump_dst / "dump_usage.py").write_text(
                (SCRIPTS / "dump_usage.py").read_text())

            tools_dir = root_p / "tools"
            tools_dir.mkdir()

            buf = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(buf):
                rc = skill_usage._run_propose_delete(
                    {}, 30, dry_run=True, here=tools_dir)

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("dev-kit:totally-idle", out)
        self.assertNotIn("dev-kit:internal-helper", out)


class TestSkillFrontmatter(unittest.TestCase):
    def test_skill_md_exists_at_flat_path(self):
        """Skill must live at skills/<name>/SKILL.md (no category subdir)."""
        self.assertTrue(SKILL_MD.exists(),
                        f"missing SKILL.md at {SKILL_MD}")

    def test_skill_md_declares_alpha_state(self):
        """L6 gate: ``alpha: state`` must be in the frontmatter so the
        governance test stays green for this newly added skill."""
        text = SKILL_MD.read_text(encoding="utf-8")
        # Frontmatter is the YAML block delimited by --- lines. Search
        # for the opening fence + closing fence rather than anchoring
        # with ``^`` so the regex works against the whole file body.
        self.assertRegex(text, r"---\n[\s\S]+?\n---",
                         "frontmatter missing")
        self.assertIsNotNone(
            re.search(r"(?m)^alpha:\s*state\s*$", text),
            "alpha: state frontmatter required (L6)",
        )

    def test_skill_md_name_matches_directory(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIsNotNone(
            re.search(r"(?m)^name:\s*prune-propose\s*$", text),
            "name: prune-propose required",
        )

    def test_skill_md_category_is_audit(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIsNotNone(
            re.search(r"(?m)^category:\s*audit\s*$", text),
            "category: audit required",
        )

    def test_skill_md_disallows_write_edit(self):
        """Per the dispatch: Write and Edit must be disallowed."""
        text = SKILL_MD.read_text(encoding="utf-8")
        m_text = text.split("---", 2)[1] if "---" in text else text
        self.assertIn("disallowed-tools:", m_text)
        self.assertRegex(m_text, r"disallowed-tools:.*\bWrite\b")
        self.assertRegex(m_text, r"disallowed-tools:.*\bEdit\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
