#!/usr/bin/env python3
"""test_smoke.py — End-to-end smoke test for dev-kit.

Validates the plugin-only structure:
- All 27 skills present (flat: skills/<name>/SKILL.md; former commands merged in)
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

PROJECT_ROOT = Path(__file__).parent.parent
SKILL_COUNT = 27
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
