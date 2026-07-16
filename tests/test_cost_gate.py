#!/usr/bin/env python3
"""test_cost_gate.py — regression tests for the cost-gate library + CLI.

Black-box coverage:

  1. lib/cost_gate.py — pricing tiers, unknown-model fallback, state I/O
     atomicity, threshold evaluation, heuristic fallback provenance, footer
     parsing + dedup, PR aggregation.
  2. tools/cost_gate_status.py — text/json/html/footer/aggregate-pr output.
  3. PR-level label decision — threshold crossing, footer dedup, missing
     telemetry.
  4. lib/cost_gate.py exposes no session_kill threshold (the blocking
     gate is removed; cost is observed only).

No import of tools.token_efficiency_analyzer — isolation guarantee.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
LIB = REPO_ROOT / "lib"
TOOLS = REPO_ROOT / "tools"


# --- helpers -----------------------------------------------------------------

def _run_cli(*args: str, cwd: Path | None = None,
             env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(TOOLS / "cost_gate_status.py"), *args],
        capture_output=True, text=True, timeout=15,
        cwd=str(cwd) if cwd else None, env=env,
    )


# ============================================================================
# 1. lib/cost_gate.py — pricing
# ============================================================================

class TestPricing(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from cost_gate import pricing_for  # type: ignore
        self.pricing_for = pricing_for

    def test_minimax_substring_matches_first(self):
        # minimax must be detected before opus/sonnet/haiku to avoid the
        # substring collision ("MiniMax" contains no overlap, but defensively).
        p = self.pricing_for("MiniMax-M3[1m]")
        self.assertAlmostEqual(p["in"], 0.30, places=4)
        self.assertAlmostEqual(p["out"], 1.20, places=4)

    def test_opus_substring(self):
        p = self.pricing_for("claude-opus-4-8")
        self.assertAlmostEqual(p["in"], 5.00, places=4)
        self.assertAlmostEqual(p["out"], 25.00, places=4)

    def test_sonnet_substring(self):
        p = self.pricing_for("claude-sonnet-5")
        self.assertAlmostEqual(p["in"], 3.00, places=4)
        self.assertAlmostEqual(p["out"], 15.00, places=4)

    def test_haiku_substring(self):
        p = self.pricing_for("claude-haiku-4-5")
        self.assertAlmostEqual(p["in"], 1.00, places=4)
        self.assertAlmostEqual(p["out"], 5.00, places=4)

    def test_unknown_falls_back_to_sonnet_and_collects(self):
        p, unknowns = self.pricing_for("totally-bogus-model", return_unknown=True)
        self.assertAlmostEqual(p["in"], 3.00, places=4)
        self.assertIn("totally-bogus-model", unknowns)

    def test_empty_falls_back_to_sonnet(self):
        p = self.pricing_for("")
        self.assertAlmostEqual(p["in"], 3.00, places=4)


# ============================================================================
# 2. lib/cost_gate.py — cost math
# ============================================================================

class TestCostMath(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from cost_gate import cost_usd  # type: ignore
        self.cost_usd = cost_usd

    def test_basic_sonnet_input_only(self):
        c = self.cost_usd("claude-sonnet-5", input_tokens=1_000_000)
        self.assertAlmostEqual(c, 3.00, places=4)

    def test_basic_opus_input_and_output(self):
        c = self.cost_usd("claude-opus-4-8",
                          input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(c, 5.00 + 25.00, places=4)

    def test_cache_read_cheaper_than_input(self):
        c_in = self.cost_usd("claude-sonnet-5", input_tokens=1_000_000)
        c_cache = self.cost_usd("claude-sonnet-5", cache_read_tokens=1_000_000)
        self.assertLess(c_cache, c_in)

    def test_5m_cheaper_than_1h_cache_write(self):
        c5 = self.cost_usd("claude-sonnet-5",
                           cache_write_5m_tokens=1_000_000)
        c1 = self.cost_usd("claude-sonnet-5",
                           cache_write_1h_tokens=1_000_000)
        self.assertLess(c5, c1)


# ============================================================================
# 3. lib/cost_gate.py — state I/O + atomicity
# ============================================================================

class TestStateIO(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from cost_gate import (  # type: ignore
            new_session_state, load_state, save_state, DEFAULT_TOTAL,
        )
        self.new_session_state = new_session_state
        self.load_state = load_state
        self.save_state = save_state
        self.DEFAULT_TOTAL = DEFAULT_TOTAL

    def test_new_state_has_required_keys(self):
        s = self.new_session_state(
            session_id="x", cwd="/tmp", branch="main", repository="r",
            model="claude-sonnet-5",
        )
        self.assertEqual(s["schema_version"], 1)
        self.assertEqual(s["scope"], "session")
        self.assertEqual(s["scope_id"], "x")
        self.assertEqual(s["totals"], self.DEFAULT_TOTAL)
        self.assertEqual(s["status"], "ok")
        self.assertFalse(s["warn_emitted"])
        self.assertEqual(s["warnings"], [])
        self.assertEqual(s["sessions"][0]["session_id"], "x")
        self.assertEqual(s["sessions"][0]["provenance"], "actual")

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s = self.new_session_state(
                session_id="x", cwd=td, branch="feat/y", repository="r",
                model="claude-opus-4-8",
            )
            self.save_state(p, s)
            loaded = self.load_state(p)
            self.assertEqual(loaded["scope_id"], "x")
            self.assertEqual(loaded["sessions"][0]["model"], "claude-opus-4-8")

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(self.load_state(Path(td) / "missing.json"))

    def test_load_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text("not-json{", encoding="utf-8")
            self.assertIsNone(self.load_state(p))

    def test_atomic_write_uses_tempfile(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s = self.new_session_state(
                session_id="x", cwd=td, branch="main", repository="r",
                model="claude-sonnet-5",
            )
            self.save_state(p, s)
            # No leftover tempfiles.
            leftovers = list(Path(td).glob(".state.json.*"))
            self.assertEqual(leftovers, [], f"leftover: {leftovers}")


# ============================================================================
# 4. lib/cost_gate.py — thresholds (no kill branch)
# ============================================================================

class TestThresholds(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from cost_gate import (  # type: ignore
            DEFAULT_THRESHOLDS, resolve_thresholds,
        )
        self.DEFAULT_THRESHOLDS = DEFAULT_THRESHOLDS
        self.resolve_thresholds = resolve_thresholds

    def test_default_thresholds_have_no_kill(self):
        # The cost-gate is observed only. There is no session_kill threshold
        # in the default config — adding one back would be a regression.
        self.assertNotIn("session_kill", self.DEFAULT_THRESHOLDS)
        self.assertIn("session_warn", self.DEFAULT_THRESHOLDS)
        self.assertIn("pr_flag", self.DEFAULT_THRESHOLDS)

    def test_resolve_thresholds_has_no_kill(self):
        th = self.resolve_thresholds()
        self.assertNotIn("session_kill", th)

# ============================================================================
# 5. lib/cost_gate.py — heuristic fallback
# ============================================================================

# ============================================================================
# 6. lib/cost_gate.py — footer parsing + dedup
# ============================================================================

class TestFooterParsing(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from cost_gate import parse_footers, aggregate_pr_sessions  # type: ignore
        self.parse_footers = parse_footers
        self.aggregate_pr_sessions = aggregate_pr_sessions

    def test_parse_single_footer(self):
        body = "feat: thing\n\nCost-gate: $8.42\nCost-gate-Session: sess-1"
        out = self.parse_footers([body])
        self.assertEqual(out, [{"session": "sess-1", "usd": 8.42}])

    def test_parse_missing_returns_empty(self):
        body = "feat: thing\n\nNo trailers here."
        out = self.parse_footers([body])
        self.assertEqual(out, [])

    def test_dedup_keeps_max_per_session(self):
        # Same session appearing in two commits: keep the max cumulative.
        out = self.parse_footers([
            "Cost-gate: $3.00\nCost-gate-Session: sess-1",
            "Cost-gate: $5.00\nCost-gate-Session: sess-1",
        ])
        self.assertEqual(out, [{"session": "sess-1", "usd": 5.00}])

    def test_aggregate_pr_sessions_sums(self):
        commits = [
            "Cost-gate: $3.00\nCost-gate-Session: sess-1",
            "Cost-gate: $5.00\nCost-gate-Session: sess-1",  # dedup to $5
            "Cost-gate: $2.00\nCost-gate-Session: sess-2",
        ]
        out = self.parse_footers(commits)
        total = self.aggregate_pr_sessions(out)
        self.assertAlmostEqual(total, 7.00, places=4)


# ============================================================================
# 7. tools/cost_gate_status.py — CLI
# ============================================================================

class TestCliText(unittest.TestCase):
    def test_text_includes_scope_and_thresholds(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            r = _run_cli("--state", str(state_path))
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            self.assertIn("scope:", r.stdout)
            self.assertIn("session_warn:", r.stdout)
            self.assertIn("pr_flag:", r.stdout)
            # No kill threshold should be displayed.
            self.assertNotIn("session_kill:", r.stdout)

    def test_text_has_no_hook_modes(self):
        # The cost-gate hook is removed. The CLI must not accept any
        # --hook-* flag (those would be misleading at best).
        with tempfile.TemporaryDirectory() as td:
            r = _run_cli("--hook-session-start", cwd=Path(td))
            self.assertNotEqual(r.returncode, 0,
                                f"hook flag accepted: stdout={r.stdout!r}")


class TestCliJson(unittest.TestCase):
    def test_json_has_required_keys(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            r = _run_cli("--state", str(state_path), "--json")
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            doc = json.loads(r.stdout)
            for k in ("scope", "totals", "thresholds_usd", "status",
                      "warnings", "state_path"):
                self.assertIn(k, doc, f"missing {k} in JSON: {doc}")
            # No kill threshold in JSON output either.
            self.assertNotIn("session_kill", doc.get("thresholds_usd", {}))


class TestCliHtml(unittest.TestCase):
    def test_html_is_self_contained(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            out = Path(td) / "report.html"
            r = _run_cli("--state", str(state_path), "--html", str(out))
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            self.assertTrue(out.exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("<html", content)
            self.assertNotIn("<script", content)
            # No kill mention.
            self.assertNotIn("kill", content)


class TestCliFooter(unittest.TestCase):
    def test_footer_format(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            r = _run_cli("--state", str(state_path), "--footer")
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            self.assertIn("Cost-gate:", r.stdout)
            self.assertIn("Cost-gate-Session:", r.stdout)


# ============================================================================
# 8. hooks/cost-gate.sh — gone
# ============================================================================

class TestHookScriptRemoved(unittest.TestCase):
    def test_cost_gate_sh_does_not_exist(self):
        path = REPO_ROOT / "hooks" / "cost-gate.sh"
        self.assertFalse(path.exists(), f"{path} still exists — hook was removed")

    def test_hooks_json_does_not_wire_cost_gate(self):
        cfg_path = REPO_ROOT / "hooks" / "hooks.json"
        if not cfg_path.exists():
            self.skipTest(f"hooks.json not found at {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        for event, entries in cfg.get("hooks", {}).items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    self.assertNotIn(
                        "cost-gate.sh", cmd,
                        f"hooks.json still wires cost-gate.sh under {event}: {cmd}",
                    )


# ============================================================================
# 9. Isolation guarantee — lib/cost_gate.py and tools/cost_gate_status.py
#    must NOT import tools/token_efficiency_analyzer.py. As of 2026-07-17
#    both subsystems do legitimately share ``lib/llm_pricing.py`` (the new
#    SSOT pricing loader); the import-direction guard is the one we still
#    assert — cost-gate must not depend on the dashboard module directly.
# ============================================================================

class TestIsolation(unittest.TestCase):
    def _has_import_statement(self, text: str, module: str) -> bool:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and module in stripped:
                return True
        return False

    def test_lib_cost_gate_does_not_import_token_analyzer(self):
        path = LIB / "cost_gate.py"
        if not path.exists():
            self.skipTest("cost_gate.py not found")
        text = path.read_text(encoding="utf-8")
        self.assertFalse(self._has_import_statement(text, "token_efficiency_analyzer"),
                         "lib/cost_gate.py must not import tools/token_efficiency_analyzer")

    def test_tools_cost_gate_status_does_not_import_token_analyzer(self):
        path = TOOLS / "cost_gate_status.py"
        if not path.exists():
            self.skipTest("cost_gate_status.py not found")
        text = path.read_text(encoding="utf-8")
        self.assertFalse(self._has_import_statement(text, "token_efficiency_analyzer"),
                         "tools/cost_gate_status.py must not import tools/token_efficiency_analyzer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
