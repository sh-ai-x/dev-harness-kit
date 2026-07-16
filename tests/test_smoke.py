#!/usr/bin/env python3
"""test_smoke.py — End-to-end smoke test for dev-kit.

Validates the plugin-only structure:
- All skills present in the flat layout: skills/<name>/SKILL.md
- All 5 hook bash scripts executable + syntactically valid (hooks/)
- 7 stage→hook mapping in active_hooks_codec
- Iron Laws are SSOT in CLAUDE.md
- Naming convention enforced (subset of test_naming.py)
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
SKILL_COUNT = 38
HOOK_SCRIPTS = {
    "tdd-guard.sh",
    "bash-guard.sh",
    "secret-scan.sh",
    "slop-detector.sh",
    "stop-verify.sh",
}


class TestSmoke(unittest.TestCase):
    def test_skills_count(self):
        skills_dir = PROJECT_ROOT / "skills"
        found = list(skills_dir.rglob("SKILL.md"))
        self.assertEqual(len(found), SKILL_COUNT, f"Expected {SKILL_COUNT} skills, got {len(found)}")

    def test_skill_bump_exists(self):
        """`/dev-kit:bump` must exist and its SKILL.md must parse + declare a valid frontmatter.

        Catches directory/name mismatch (skills/bump/ vs `name: bump` vs `category: ship`)
        which the SKILL_COUNT check alone misses when a skill is renamed in place.
        """
        bump = PROJECT_ROOT / "skills" / "bump" / "SKILL.md"
        self.assertTrue(bump.exists(), f"missing skill file: {bump}")
        text = bump.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
        self.assertIsNotNone(m, f"{bump} frontmatter missing or malformed")
        fm = yaml.safe_load(m.group(1))
        self.assertEqual(fm.get("name"), "bump")
        self.assertEqual(fm.get("category"), "ship")
        self.assertTrue(fm.get("user-invocable"))

    def test_plugin_manifest_has_version(self):
        """feat/skill-versions: `.claude-plugin/plugin.json` MUST declare `version:`."""
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location(
            "ci_setup_smoke", PROJECT_ROOT / "lib" / "ci_setup.py"
        )
        mod = module_from_spec(spec)
        sys.modules["ci_setup_smoke"] = mod
        spec.loader.exec_module(mod)
        v = mod.plugin_version(PROJECT_ROOT)
        self.assertRegex(v, mod.SEMVER_RE, f"plugin.json:version={v!r} is not valid semver")

    def test_semver_re_accepts_and_rejects(self):
        """`lib/ci_setup.py:SEMVER_RE` matches semver 2.0.0 shape (X.Y.Z + optional -pre/+build)."""
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location(
            "ci_setup_semver_smoke", PROJECT_ROOT / "lib" / "ci_setup.py"
        )
        mod = module_from_spec(spec)
        sys.modules["ci_setup_semver_smoke"] = mod
        spec.loader.exec_module(mod)
        for v in ("0.1.0", "1.10.0", "0.1.0-rc.1", "0.1.0+build.7", "1.0.0-alpha.1"):
            self.assertRegex(v, mod.SEMVER_RE, f"{v} should match SEMVER_RE")
        for v in ("1.0", "v1.0.0", "1.0.0.0", "", "1.0.0 ", " 1.0.0"):
            self.assertNotRegex(v, mod.SEMVER_RE, f"{v!r} should NOT match SEMVER_RE")

    def test_hook_scripts_exist_and_executable(self):
        hooks_dir = PROJECT_ROOT / "hooks"
        for h in HOOK_SCRIPTS:
            path = hooks_dir / h
            self.assertTrue(path.exists(), f"hook missing: {path}")
            self.assertTrue(os.access(path, os.X_OK), f"hook not executable: {path}")
            # bash syntax check (run as bash -n)
            import subprocess
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True)
            self.assertEqual(r.returncode, 0, f"bash syntax error in {path}: {r.stderr.decode()}")

    def test_iron_laws_in_claude_md(self):
        claude_md = (PROJECT_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for i in range(1, 6):
            # Iron law may be "**L1 Title**" or "**L1** Title:" — accept both forms
            self.assertTrue(
                f"**L{i}" in claude_md or f"L{i}**" in claude_md,
                f"L{i} missing from CLAUDE.md §1",
            )

    def test_marketplace_plugin_name(self):
        mp = json.loads((PROJECT_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(mp["name"], "dev-kit")

    def test_active_hooks_matrix_7_stages(self):
        from importlib.util import spec_from_file_location, module_from_spec
        sys.path.insert(0, str(PROJECT_ROOT / "lib"))
        spec = spec_from_file_location("ahc", PROJECT_ROOT / "lib" / "active_hooks_codec.py")
        ahc = module_from_spec(spec)
        spec.loader.exec_module(ahc)
        for stage in ("bootstrap", "plan", "design", "build", "review", "security", "ship"):
            self.assertIn(stage, ahc.DEFAULT_MATRIX, f"stage {stage} missing from matrix")

    def test_methodology_default_tdd(self):
        meth = json.loads((PROJECT_ROOT / "lib" / "methodology.json").read_text())
        self.assertEqual(meth["active"], "tdd")

    def test_pre_approved_gate_exists(self):
        p = PROJECT_ROOT / "docs" / "PRE-IMPL-CHECK.md"
        self.assertTrue(p.exists())
        content = p.read_text()
        # 5-Question + 8-Dimension sections both present
        self.assertIn("WHETHER Gate", content)
        self.assertIn("8-Dimension Cost/Risk", content)

    def test_cost_analysis_exists(self):
        p = PROJECT_ROOT / "docs" / "COST-ANALYSIS.md"
        self.assertTrue(p.exists())
        content = p.read_text()
        # 8 dimensions all covered
        for dim in ("Time Cost", "Monetary Cost", "Legal Risk", "Maintenance Risk",
                    "Opportunity Cost", "Compatibility Risk", "Security Risk", "Operational Risk"):
            self.assertIn(dim, content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
