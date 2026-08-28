"""tests/test_skill_frontmatter_audit.py — G5 regression for the A2 audit.

Per docs/proposals/cache-hit-rate/structural-fix.yaml §Validation gates:

  G5: every SKILL.md frontmatter must be free of non-deterministic
      fields (timestamps, git SHAs, build numbers). The audit is
      read-only — it never auto-edits the SKILL.md files; it just
      reports which skills need a maintainer review.

These tests synthesize two fake SKILL.md files (clean + dirty) in a
tmp layout and invoke ``scripts/audit_skill_frontmatter.py`` against
that layout. We don't run the audit against the real ``skills/`` dir
because (a) it could mask a real regression behind a test that says
"all green" and (b) tests must not depend on repo state.

Stdlib only. No third-party deps.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "audit_skill_frontmatter.py"


CLEAN_FM = textwrap.dedent("""\
    ---
    name: clean-skill
    description: A skill whose frontmatter is fully deterministic.
    ---
    Body text starts here.
""")

DIRTY_FM_DATE = textwrap.dedent("""\
    ---
    name: dirty-date-skill
    description: Has an ISO date that auto-regenerates.
    last-reviewed: 2026-08-28
    ---
    Body.
""")

DIRTY_FM_SHA = textwrap.dedent("""\
    ---
    name: dirty-sha-skill
    description: Embeds a git SHA.
    source-ref: a1b2c3d4e5f6
    ---
    Body.
""")

DIRTY_FM_BUILD = textwrap.dedent("""\
    ---
    name: dirty-build-skill
    description: Embeds a build counter.
    artifact: build-1234
    ---
    Body.
""")


def _make_skill(skills_root: Path, name: str, body: str) -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body)
    return path


class TestSkillFrontmatterAudit(unittest.TestCase):
    """G5 regression — see module docstring."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="skill-audit-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _invoke_audit(self) -> subprocess.CompletedProcess:
        """Invoke the audit script with a tmp skills/ dir.

        The script's REPO_ROOT is derived from its own __file__
        location, so we stage a fake-repo with a copy of the script
        and a fresh skills/ dir.
        """
        fake_repo = self.tmpdir / "fake-repo"
        fake_repo.mkdir()
        scripts_dir = fake_repo / "scripts"
        scripts_dir.mkdir()
        # Re-use the real audit script — its REPO_ROOT = Path(__file__).parent.parent
        # = <fake_repo>, and SKILLS_DIR = <fake_repo>/skills, which we set up below.
        target_script = scripts_dir / "audit_skill_frontmatter.py"
        shutil.copy(AUDIT_SCRIPT, target_script)
        target_skills = fake_repo / "skills"
        target_skills.mkdir()
        # Caller is responsible for filling target_skills with tests' inputs.
        return subprocess.run(
            [sys.executable, str(target_script)],
            capture_output=True, text=True, cwd=str(fake_repo),
        )

    def test_clean_frontmatter_passes(self):
        """A skill whose frontmatter is free of forbidden fields passes
        with exit 0 and is not named in the report."""
        fake_repo = self.tmpdir / "fake-repo"
        (fake_repo / "scripts").mkdir(parents=True)
        shutil.copy(AUDIT_SCRIPT, fake_repo / "scripts" / "audit_skill_frontmatter.py")
        _make_skill(fake_repo / "skills", "clean-skill", CLEAN_FM)
        proc = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "audit_skill_frontmatter.py")],
            capture_output=True, text=True, cwd=str(fake_repo),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"expected exit 0, got {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
        self.assertIn("deterministic frontmatter", proc.stdout)

    def test_dirty_date_is_flagged(self):
        """A skill with an ISO date in frontmatter must be flagged."""
        fake_repo = self.tmpdir / "fake-repo"
        (fake_repo / "scripts").mkdir(parents=True)
        shutil.copy(AUDIT_SCRIPT, fake_repo / "scripts" / "audit_skill_frontmatter.py")
        _make_skill(fake_repo / "skills", "clean-skill", CLEAN_FM)
        _make_skill(fake_repo / "skills", "dirty-date-skill", DIRTY_FM_DATE)
        proc = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "audit_skill_frontmatter.py")],
            capture_output=True, text=True, cwd=str(fake_repo),
        )
        self.assertEqual(
            proc.returncode, 1,
            f"expected exit 1 (one dirty skill), got {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
        self.assertIn("dirty-date-skill", proc.stdout)
        self.assertNotIn("clean-skill", proc.stdout)

    def test_dirty_sha_and_build_are_flagged(self):
        """SHA + build-number patterns must each be flagged."""
        fake_repo = self.tmpdir / "fake-repo"
        (fake_repo / "scripts").mkdir(parents=True)
        shutil.copy(AUDIT_SCRIPT, fake_repo / "scripts" / "audit_skill_frontmatter.py")
        _make_skill(fake_repo / "skills", "clean-skill", CLEAN_FM)
        _make_skill(fake_repo / "skills", "dirty-sha-skill", DIRTY_FM_SHA)
        _make_skill(fake_repo / "skills", "dirty-build-skill", DIRTY_FM_BUILD)
        proc = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "audit_skill_frontmatter.py")],
            capture_output=True, text=True, cwd=str(fake_repo),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("dirty-sha-skill", proc.stdout)
        self.assertIn("dirty-build-skill", proc.stdout)

    def test_json_mode_emits_structured_report(self):
        """--json mode emits parseable JSON for CI consumption."""
        fake_repo = self.tmpdir / "fake-repo"
        (fake_repo / "scripts").mkdir(parents=True)
        shutil.copy(AUDIT_SCRIPT, fake_repo / "scripts" / "audit_skill_frontmatter.py")
        _make_skill(fake_repo / "skills", "clean-skill", CLEAN_FM)
        _make_skill(fake_repo / "skills", "dirty-date-skill", DIRTY_FM_DATE)
        proc = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "audit_skill_frontmatter.py"),
             "--json"],
            capture_output=True, text=True, cwd=str(fake_repo),
        )
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertEqual(report["checked"], 2)
        self.assertIn("dirty-date-skill", report["bad"])
        self.assertNotIn("clean-skill", report["bad"])

    def test_skill_without_frontmatter_passes(self):
        """A SKILL.md with no frontmatter at all is vacuously clean."""
        no_fm = textwrap.dedent("""\
            # Just a heading, no frontmatter.
            Body.
        """)
        fake_repo = self.tmpdir / "fake-repo"
        (fake_repo / "scripts").mkdir(parents=True)
        shutil.copy(AUDIT_SCRIPT, fake_repo / "scripts" / "audit_skill_frontmatter.py")
        _make_skill(fake_repo / "skills", "no-fm-skill", no_fm)
        proc = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "audit_skill_frontmatter.py")],
            capture_output=True, text=True, cwd=str(fake_repo),
        )
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
