#!/usr/bin/env python3
"""test_llm_pricing.py — Contract tests for the shared pricing loader.

Verifies:
- ``llm_pricing.load_pricing()`` reads the SSOT JSON and merges every
  model row.
- ``llm_pricing.pricing_for()`` does longest-prefix-first substring
  matching, handles dashes/dots/underscores equivalently, falls back to
  sonnet on unknown ids.
- ``lib/cost_gate.py`` and ``tools/token_efficiency_analyzer.py`` both
  bill sessions at the JSON-sourced OPUS / Sonnet / haiku rates (not at
  the legacy inline values that used to drift independently).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LLM_INFO = PROJECT_ROOT / "docs" / "llm-info"
CLAUDE_JSON = LLM_INFO / "claude.json"
CODEX_JSON = LLM_INFO / "codex.json"

# Allow `import llm_pricing` from repo root without installing.
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import llm_pricing  # noqa: E402
import cost_gate  # noqa: E402

# Token analyzer is a CLI script in tools/ — adapt sys.path so we can import it.
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import token_efficiency_analyzer as tea  # noqa: E402


class TestSSOTLoader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pricing, cls.sources = llm_pricing.load_pricing()

    def test_loader_returns_a_dict(self):
        self.assertIsInstance(self.pricing, dict)
        self.assertGreater(len(self.pricing), 0, "loader produced an empty PRICING")

    def test_loader_reads_claude_opus_4_8_at_documented_rate(self):
        # Cross-check against the verified value in docs/llm-info/claude.json.
        claude = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
        opus_json = next(m for m in claude["models"] if m["id"] == "claude-opus-4-8")
        expected_in = opus_json["input_price_per_mtok"] / llm_pricing._currency_multiplier("USD")
        self.assertIn("claude-opus-4-8", self.pricing)
        self.assertAlmostEqual(self.pricing["claude-opus-4-8"]["in"], expected_in, places=4)
        self.assertAlmostEqual(self.pricing["claude-opus-4-8"]["out"], 25.00, places=4)

    def test_loader_reads_gpt_5_5_pro_at_documented_rate(self):
        codex = json.loads(CODEX_JSON.read_text(encoding="utf-8"))
        pro_json = next(m for m in codex["models"] if m["id"] == "gpt-5.5-pro")
        # The JSON price is in USD; loader must pass it through unchanged.
        self.assertIn("gpt-5.5-pro", self.pricing)
        self.assertAlmostEqual(self.pricing["gpt-5.5-pro"]["in"], pro_json["input_price_per_mtok"], places=4)
        self.assertAlmostEqual(self.pricing["gpt-5.5-pro"]["out"], pro_json["output_price_per_mtok"], places=4)

    def test_minimax_loader_converts_cny_to_usd(self):
        # MiniMax publishes in CNY; loader must convert to USD so the
        # cost_usd math returns dollar amounts. JSON rows keep their
        # original mixed-case id; the matcher lowercases for lookup.
        minimax_keys = [k for k in self.pricing if k.lower().startswith("minimax")]
        self.assertGreater(len(minimax_keys), 0,
                           "no MiniMax rows loaded (CNY->USD conversion may be broken)")
        for k in minimax_keys:
            self.assertLess(self.pricing[k]["in"], 5.0,
                            f"{k}: MiniMax rate looks unconverted (should be < $5/MTok USD)")


class TestPricingFor(unittest.TestCase):
    def setUp(self):
        llm_pricing.clear_cache()

    def test_exact_id_resolves_to_json_rate(self):
        # Loader returns a row whose "in" matches the JSON.
        claude = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
        opus_json = next(m for m in claude["models"] if m["id"] == "claude-opus-4-8")
        expected_in = opus_json["input_price_per_mtok"]  # USD
        row = llm_pricing.pricing_for("claude-opus-4-8")
        self.assertAlmostEqual(row["in"], expected_in, places=2)

    def test_longest_prefix_first_for_gpt_5_5_vs_gpt_5(self):
        # gpt-5.5 and gpt-5 are distinct tiers; "gpt-5.5" must hit its
        # own row, not fall through to the cheaper legacy "gpt-5".
        json_5_5 = llm_pricing.pricing_for("gpt-5.5")
        json_5 = llm_pricing.pricing_for("gpt-5")
        self.assertGreater(json_5_5["in"], json_5["in"],
                           "gpt-5.5 must NOT match the cheaper gpt-5 tier")

    def test_normalized_matching_handles_dots_dashes_underscores(self):
        # `gpt-5.5-pro` (live id) must hit the `gpt-5-5-pro` JSON slug.
        row = llm_pricing.pricing_for("gpt-5.5-pro")
        # Match against what the JSON rows have.
        self.assertAlmostEqual(row["in"], 30.00, places=2)
        self.assertAlmostEqual(row["out"], 180.00, places=2)

    def test_empty_id_falls_back_to_sonnet(self):
        row = llm_pricing.pricing_for("")
        # Sonnet = $3 / $15 in USD legacy (closest to JSON claude-sonnet-5).
        self.assertEqual(row["out"], 15.00)

    def test_unknown_id_warns_to_stderr_and_returns_sonnet(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            row = llm_pricing.pricing_for("totally-unknown-model-xyz")
        self.assertEqual(row["out"], 15.00)
        self.assertIn("WARN: unknown model", buf.getvalue())


class TestConsumersUseSSOT(unittest.TestCase):
    """Both lib/cost_gate.py and the token analyzer must bill at the JSON rates."""

    @classmethod
    def setUpClass(cls):
        llm_pricing.clear_cache()

    def test_cost_gate_pricing_for_matches_loader(self):
        # The two entry points must return identical rows for the same id.
        from_loader = llm_pricing.pricing_for("claude-opus-4-8")
        from_cost_gate = cost_gate.pricing_for("claude-opus-4-8")
        self.assertEqual(from_loader, from_cost_gate)

    def test_token_analyzer_pricing_for_matches_loader(self):
        from_loader = llm_pricing.pricing_for("claude-sonnet-5")
        from_analyzer = tea.pricing_for("claude-sonnet-5")
        self.assertEqual(from_loader, from_analyzer)

    def test_cost_usd_matches_analyzer_cost_usd(self):
        # Same usage, same model id, same dollar cost — they were two
        # separate impls that historically drifted.
        args = dict(model_id="claude-opus-4-8", input_tokens=500_000, output_tokens=100_000)
        cg = cost_gate.cost_usd(**args)
        ta = tea.cost_usd(**args)
        self.assertAlmostEqual(cg, ta, places=6)


class TestPricingIntegrity(unittest.TestCase):
    def test_no_negative_prices_in_loaded_rows(self):
        pricing, _ = llm_pricing.load_pricing()
        for k, row in pricing.items():
            for field in ("in", "out", "cache_write_5m", "cache_write_1h", "cache_read"):
                self.assertGreaterEqual(row[field], 0, f"{k}: {field}={row[field]} is negative")

    def test_required_keys_present_in_every_row(self):
        pricing, _ = llm_pricing.load_pricing()
        required = {"in", "out", "cache_write_5m", "cache_write_1h", "cache_read"}
        for k, row in pricing.items():
            self.assertEqual(required & set(row.keys()), required,
                             f"{k} missing one of {required}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
