#!/usr/bin/env python3
"""test_harness_audit.py — Phase 7 (issue #390) regression coverage.

Verifies `tools/harness_audit.py` (cross-harness quality audit) over
the 4 harnesses defined as of PR chore/remove-valuate: hooks, eval,
research, interview (LCS in #463, plan_value in chore/remove-valuate).

Covers:
- All 4 harnesses appear in audit output, in HARNESSES order
- HTML and JSON output paths are well-formed + machine-readable
- Read-only invariant: no .dev-kit/state.json mutation, no file write
  outside the user's chosen --html-out PATH (default writes a report
  to .dev-kit/harness-audit-report.html — that file is the audit
  artifact, not state mutation; the assertion checks state files stay
  untouched)
- Missing alpha / missing rubrics / missing harnesses surface as
  findings, not exceptions
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
TOOL = PROJECT_ROOT / "tools" / "harness_audit.py"

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
import harness_audit  # noqa: E402


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run `python3 tools/harness_audit.py` with args; capture stdout/stderr."""
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or PROJECT_ROOT),
    )


class TestHarnessAudit(unittest.TestCase):
    def test_audit_covers_all_six_harnesses(self):
        """audit dict must contain exactly 4 harnesses in HARNESSES order.

        The audit covered 6 harnesses through #447 (lcs / hooks / eval /
        plan_value / research / interview). PR #463 dropped the LCS
        substrate (6 → 5); PR chore/remove-valuate dropped plan_value
        (5 → 4). The test name is preserved (renaming would churn git
        history) but the assertion is updated.
        """
        audit = harness_audit.run_audit(PROJECT_ROOT)
        names = [h["name"] for h in audit["harnesses"]]
        self.assertEqual(names, list(harness_audit.HARNESSES))
        self.assertEqual(audit["summary"]["total"], 4)

    def test_audit_json_output_machine_readable(self):
        """--json emits parseable JSON with the canonical shape."""
        result = _run_cli("--json", "--project-root", str(PROJECT_ROOT))
        data = json.loads(result.stdout)
        self.assertIn("harnesses", data)
        self.assertIn("summary", data)
        self.assertIn("read_only", data)
        self.assertEqual(data["read_only"], True)
        self.assertEqual(len(data["harnesses"]), 4)

    def test_audit_emits_html_report(self):
        """--html-out PATH writes a self-contained HTML file with one row per harness."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "audit.html"
            _run_cli("--html-out", str(out),
                     "--project-root", str(PROJECT_ROOT))
            self.assertTrue(out.exists(), f"--html-out did not write {out}")
            html = out.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", html)
            self.assertIn("<table>", html)
            # 4 harness rows + 1 header row = 5 <tr>
            self.assertEqual(html.count("<tr>"), 5)
            # No external assets / no JavaScript
            self.assertNotIn("<script", html)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))

    def test_audit_is_read_only(self):
        """Audit must NOT mutate state files. Snapshot state.json + smoke.py before/after."""
        state_file = PROJECT_ROOT / ".dev-kit" / "state.json"
        had_state = state_file.exists()
        if had_state:
            before = state_file.read_bytes()
        else:
            before = b""
        smoke = PROJECT_ROOT / "tests" / "test_smoke.py"
        smoke_before = smoke.read_bytes()
        # Default audit writes .dev-kit/harness-audit-report.html; that's
        # the audit's own output artifact (like eval/report.md) — not a
        # state file. We only assert state.json + test_smoke.py are untouched.
        _run_cli("--project-root", str(PROJECT_ROOT))
        if had_state:
            self.assertEqual(state_file.read_bytes(), before,
                             "state.json was mutated by audit")
        self.assertEqual(smoke.read_bytes(), smoke_before,
                         "test_smoke.py was mutated by audit (would indicate state leak)")

    def test_audit_detects_missing_alpha_field(self):
        """A SKILL.md missing the `alpha:` field must surface as alpha_valid=False.

        Used to test against `skills/lcs/SKILL.md` because that was the
        simplest "state alpha" harness. After #463 the LCS substrate
        (and `skills/lcs/SKILL.md`) is gone, so the test exercises the
        same logic against `skills/evaluate/SKILL.md` (still shipped;
        the audit function reads alpha from frontmatter for this
        harness). The harness choice is incidental — the assertion is
        "any SKILL.md whose alpha frontmatter is missing surfaces as
        alpha_valid=False".
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills" / "evaluate").mkdir(parents=True)
            # SKILL.md without alpha field
            (root / "skills" / "evaluate" / "SKILL.md").write_text(
                "---\nname: evaluate\ncategory: enforcement\n---\nbody\n",
                encoding="utf-8",
            )
            audit = harness_audit.run_audit(root)
            eval_h = next(h for h in audit["harnesses"] if h["name"] == "eval")
            self.assertFalse(eval_h["alpha_valid"])
            self.assertTrue(any("alpha" in f for f in eval_h["findings"]))

    def test_audit_handles_missing_harnesses(self):
        """Missing research/interview files surface as findings, not exceptions."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Empty repo — all harnesses should report findings, no crashes
            audit = harness_audit.run_audit(root)
            self.assertEqual(audit["summary"]["total"], 4)
            for h in audit["harnesses"]:
                self.assertFalse(h["shipped"], f"{h['name']} unexpectedly shipped")
                self.assertGreater(len(h["findings"]), 0,
                                   f"{h['name']} has no findings")

    def test_audit_detects_missing_rubrics(self):
        """eval harness flags missing harness-quality.yaml / os-quality.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills" / "evaluate").mkdir(parents=True)
            (root / "skills" / "evaluate" / "SKILL.md").write_text(
                "---\nname: evaluate\ncategory: eval\nalpha: enforcement\n---\n",
                encoding="utf-8",
            )
            (root / "lib").mkdir()
            (root / "lib" / "eval_runner.py").write_text("# stub\n")
            (root / "lib" / "llm_judge.py").write_text("# stub\n")
            (root / "eval" / "rubrics").mkdir(parents=True)
            # Empty rubrics dir — both expected rubrics missing
            audit = harness_audit.run_audit(root)
            ev = next(h for h in audit["harnesses"] if h["name"] == "eval")
            self.assertFalse(ev["shipped"])
            self.assertEqual(ev["rubric_count"], 0)
            self.assertEqual(ev["rubric_expected"], 2)
            self.assertTrue(any("rubric" in f for f in ev["findings"]))

    def test_read_alpha_returns_none_when_pyyaml_absent(self):
        """Regression for inspect 2026-08-03 finding #1.

        The previous `except (ImportError, yaml.YAMLError)` referenced
        `yaml` in its own failure handler, so when PyYAML was absent the
        name was never bound and the except tuple itself raised
        NameError. Splitting the except into two clauses (ImportError
        first, then yaml.YAMLError only when yaml is bound) means a
        missing PyYAML now returns (None, False) cleanly.
        """
        import sys

        # Force `import yaml` inside _read_alpha to raise ImportError.
        saved_yaml = sys.modules.pop("yaml", None)
        sys.modules["yaml"] = None  # type: ignore[assignment]  # None triggers ImportError on re-import
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "skills" / "evaluate").mkdir(parents=True)
                (root / "skills" / "evaluate" / "SKILL.md").write_text(
                    "---\nname: evaluate\ncategory: eval\nalpha: enforcement\n---\n",
                    encoding="utf-8",
                )
                # Must not raise NameError; must return (None, False).
                alpha, valid = harness_audit._read_alpha(root, "evaluate")
                self.assertIsNone(alpha)
                self.assertFalse(valid)
        finally:
            del sys.modules["yaml"]
            if saved_yaml is not None:
                sys.modules["yaml"] = saved_yaml


if __name__ == "__main__":
    unittest.main(verbosity=2)
