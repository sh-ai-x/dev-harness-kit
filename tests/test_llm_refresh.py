#!/usr/bin/env python3
"""test_llm_refresh.py — Contract tests for /dev-kit:llm-refresh.

Locks the shape of docs/llm-info/* and the behaviour of
skills/llm-refresh/scripts/refresh.py so a future refactor cannot silently
break the SSOT.

Parser accuracy is OUT OF SCOPE here: each provider's HTML structure is owned
by the vendor. Tests assert *contract* (schema, presence, types), never exact
prices or model ids.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LLM_INFO = PROJECT_ROOT / "docs" / "llm-info"
SKILL_DIR = PROJECT_ROOT / "skills" / "llm-refresh"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFRESH_PY = SKILL_DIR / "scripts" / "refresh.py"
EXPECTED_PROVIDERS = ("claude", "codex", "minimax", "deepseek")

REQUIRED_TOP_KEYS = {"provider", "label", "source_url", "fetched_at", "currency", "models", "plans"}
REQUIRED_MODEL_KEYS = {
    "id", "display_name", "context_window",
    "input_price_per_mtok", "output_price_per_mtok",
    "deprecated", "notes",
}


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.+?)\n---", text, re.DOTALL)
    if not m:
        return {}
    return _yamlish(m.group(1))


def _yamlish(block: str) -> dict:
    """Tiny YAML-subset parser (only enough for SKILL.md frontmatter).

    Hand-rolled to avoid adding a PyYAML dependency to the plugin's tests.
    Supports `key: scalar` lines and `key:` followed by indented bullet list.
    """
    out: dict = {}
    current_list_key: str | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(("  ", "\t", "-")):
            stripped = raw.strip()
            if stripped.startswith("- ") and current_list_key is not None:
                out[current_list_key].append(stripped[2:].strip())
            continue
        if ":" in raw:
            key, _, value = raw.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "":
                out[key] = []
                current_list_key = key
            else:
                current_list_key = None
                out[key] = value
    return out


class TestSourcesRegistry(unittest.TestCase):
    def test_sources_json_loads_and_has_schema_version(self):
        data = json.loads((LLM_INFO / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(data.get("schema_version"), "1.0.0")
        self.assertIsInstance(data.get("providers"), list)

    def test_sources_json_lists_all_four_providers(self):
        ids = {p["id"] for p in json.loads((LLM_INFO / "sources.json").read_text(encoding="utf-8"))["providers"]}
        self.assertEqual(ids, set(EXPECTED_PROVIDERS))

    def test_sources_json_ids_match_filenames(self):
        providers = json.loads((LLM_INFO / "sources.json").read_text(encoding="utf-8"))["providers"]
        for p in providers:
            path = LLM_INFO / f"{p['id']}.json"
            self.assertTrue(path.exists(), f"missing provider file: {path.relative_to(PROJECT_ROOT)}")

    def test_sources_json_parser_is_registered(self):
        providers = json.loads((LLM_INFO / "sources.json").read_text(encoding="utf-8"))["providers"]
        # Import PARSERS via subprocess-free trick: parse the literal in refresh.py source.
        text = REFRESH_PY.read_text(encoding="utf-8")
        # The PARSERS dict literal must mention every parser kind.
        for p in providers:
            self.assertIn(
                f'"{p["parser"]}"', text,
                f"parser kind '{p['parser']}' (provider {p['id']}) not wired in refresh.py",
            )


class TestProviderPayloads(unittest.TestCase):
    def test_each_provider_file_loads_as_json(self):
        for pid in EXPECTED_PROVIDERS:
            with self.subTest(provider=pid):
                data = json.loads((LLM_INFO / f"{pid}.json").read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict)

    def test_each_provider_has_required_top_level_keys(self):
        for pid in EXPECTED_PROVIDERS:
            with self.subTest(provider=pid):
                data = json.loads((LLM_INFO / f"{pid}.json").read_text(encoding="utf-8"))
                self.assertEqual(set(data.keys()) & REQUIRED_TOP_KEYS, REQUIRED_TOP_KEYS,
                                 f"{pid}: missing keys {REQUIRED_TOP_KEYS - set(data.keys())}")

    def test_each_provider_has_at_least_one_model(self):
        for pid in EXPECTED_PROVIDERS:
            with self.subTest(provider=pid):
                data = json.loads((LLM_INFO / f"{pid}.json").read_text(encoding="utf-8"))
                self.assertGreaterEqual(
                    len(data["models"]), 1,
                    f"{pid}: models[] must have ≥1 entry",
                )

    def test_models_have_required_keys_and_typed_prices(self):
        for pid in EXPECTED_PROVIDERS:
            data = json.loads((LLM_INFO / f"{pid}.json").read_text(encoding="utf-8"))
            for i, m in enumerate(data["models"]):
                with self.subTest(provider=pid, index=i):
                    self.assertEqual(set(m.keys()) & REQUIRED_MODEL_KEYS, REQUIRED_MODEL_KEYS,
                                     f"{pid}.models[{i}]: missing keys")
                    self.assertIsInstance(m["input_price_per_mtok"], (int, float))
                    self.assertIsInstance(m["output_price_per_mtok"], (int, float))
                    self.assertIsInstance(m["context_window"], int)
                    self.assertIsInstance(m["deprecated"], bool)

    def test_prices_are_nonnegative(self):
        for pid in EXPECTED_PROVIDERS:
            data = json.loads((LLM_INFO / f"{pid}.json").read_text(encoding="utf-8"))
            for i, m in enumerate(data["models"]):
                with self.subTest(provider=pid, index=i):
                    self.assertGreaterEqual(m["input_price_per_mtok"], 0)
                    self.assertGreaterEqual(m["output_price_per_mtok"], 0)

    def test_currency_is_recognized(self):
        # Each vendor sets the unit (USD for claude/codex/deepseek; CNY for MiniMax).
        recognized = {"USD", "EUR", "GBP", "CNY", "JPY"}
        for pid in EXPECTED_PROVIDERS:
            with self.subTest(provider=pid):
                data = json.loads((LLM_INFO / f"{pid}.json").read_text(encoding="utf-8"))
                self.assertIn(data["currency"], recognized, f"{pid}: unrecognized currency")


class TestParserFixtures(unittest.TestCase):
    """End-to-end parser tests against locally-saved vendor pages.

    Proves the parsers actually match real HTML structure (not just that
    they compile). One fixture per provider; refresh fixtures by running
    `curl -A Mozilla/5.0 -o skills/llm-refresh/tests/fixtures/<name>.<ext>`
    against the URL listed in docs/llm-info/sources.json.
    """

    def _fetch_fixture(self, provider: str, fixture_name: str) -> Dict[str, Any]:
        fixture = SKILL_DIR / "tests" / "fixtures" / fixture_name
        self.assertTrue(fixture.exists(), f"missing fixture: {fixture}")
        r = subprocess.run(
            [sys.executable, str(REFRESH_PY), "--fetch-fixture", provider, str(fixture)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(r.returncode, 0,
                         f"--fetch-fixture {provider} failed: stderr={r.stderr!r} stdout={r.stdout[:400]!r}")
        return json.loads(r.stdout)

    def test_deepseek_fixture_parses_to_two_models_with_correct_prices(self):
        payload = self._fetch_fixture("deepseek", "deepseek_pricing.html")
        self.assertEqual(payload["provider"], "deepseek")
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(len(payload["models"]), 2)
        flash = next(m for m in payload["models"] if m["id"] == "deepseek-v4-flash")
        pro = next(m for m in payload["models"] if m["id"] == "deepseek-v4-pro")
        self.assertAlmostEqual(flash["input_price_per_mtok"], 0.14)
        self.assertAlmostEqual(flash["output_price_per_mtok"], 0.28)
        self.assertAlmostEqual(pro["input_price_per_mtok"], 0.435)
        self.assertAlmostEqual(pro["output_price_per_mtok"], 0.87)
        self.assertIn("Cache hit: $0.0028/MTok", flash["notes"])

    def test_anthropic_fixture_extracts_top_of_line_models(self):
        payload = self._fetch_fixture("claude", "anthropic_pricing.html")
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["currency"], "USD")
        ids = {m["id"] for m in payload["models"]}
        # Parser encodes lifecycle / band suffixes in the id; assert top-of-line
        # models appear with at least one canonical fragment in their id.
        for required_fragment in ("claude-fable-5", "claude-opus-4-8",
                                  "claude-haiku-4-5"):
            self.assertTrue(
                any(required_fragment in i for i in ids),
                f"missing {required_fragment} (ids: {sorted(ids)[:5]}...)",
            )
        opus = next(m for m in payload["models"] if m["id"].startswith("claude-opus-4-8"))
        self.assertAlmostEqual(opus["input_price_per_mtok"], 5.00)
        self.assertAlmostEqual(opus["output_price_per_mtok"], 25.00)
        self.assertEqual(opus["context_window"], 1000000)
        # Deprecated rows must be marked so consumers can distinguish.
        deprecated_models = [m for m in payload["models"] if m["deprecated"]]
        self.assertGreater(len(deprecated_models), 0,
                           "expected at least one deprecated Claude model in the fixture")

    def test_openai_fixture_extracts_gpt5_family(self):
        payload = self._fetch_fixture("codex", "openai_pricing.html")
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["currency"], "USD")
        ids = {m["id"] for m in payload["models"]}
        for required in ("gpt-5-5", "gpt-5-5-pro", "gpt-5-4", "gpt-5-4-mini", "gpt-5-4-nano"):
            self.assertIn(required, ids, f"missing {required}")
        pro = next(m for m in payload["models"] if m["id"] == "gpt-5-5-pro")
        self.assertAlmostEqual(pro["input_price_per_mtok"], 30.00)
        self.assertAlmostEqual(pro["output_price_per_mtok"], 180.00)
        nano = next(m for m in payload["models"] if m["id"] == "gpt-5-4-nano")
        self.assertAlmostEqual(nano["input_price_per_mtok"], 0.20)

    def test_minimax_fixture_extracts_llm_models_in_cny(self):
        payload = self._fetch_fixture("minimax", "minimax_pricing.md")
        self.assertEqual(payload["provider"], "minimax")
        self.assertEqual(payload["currency"], "CNY")
        ids = {m["id"] for m in payload["models"]}
        # The fixture includes both LLM models and the HAILUO video section,
        # so we only assert the LLM ones are present and the parser mapped
        # the M3 standard-tier price correctly. ID suffix is implementation
        # detail; we match on the model-prefix and any band marker.
        for fragment in ("minimax-m2-7", "minimax-m2", "minimax-m3"):
            self.assertTrue(
                any(fragment in i for i in ids),
                f"missing id containing {fragment} (have: {sorted(ids)[:5]})",
            )
        # Find the M3 row at standard ≤512k tier (price 2.10) regardless
        # of how the parser slugifies the band suffix.
        m3_standard = next(
            m for m in payload["models"]
            if "m3" in m["id"].lower() and "hailuo" not in m["id"].lower()
            and m["input_price_per_mtok"] == 2.10
            and m["output_price_per_mtok"] == 8.40
        )
        self.assertEqual(m3_standard["context_window"], 512000)
        # Priority tier exists separately and costs 1.5x.
        m3_priority = [
            m for m in payload["models"]
            if "m3" in m["id"].lower()
            and "priority" in m.get("notes", "").lower()
        ]
        self.assertGreater(len(m3_priority), 0,
                           "expected at least one priority-tier M3 row")


class TestRefreshScript(unittest.TestCase):
    def test_refresh_py_exists(self):
        self.assertTrue(REFRESH_PY.is_file(), f"missing script: {REFRESH_PY}")

    def test_refresh_py_compiles(self):
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", str(REFRESH_PY)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0,
                         f"py_compile failed: {r.stderr or r.stdout}")

    def test_refresh_py_help_exits_zero(self):
        r = subprocess.run(
            [sys.executable, str(REFRESH_PY), "--help"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(r.returncode, 0, f"--help failed: {r.stderr}")
        self.assertIn("--provider", r.stdout)
        self.assertIn("--check", r.stdout)

    def test_refresh_py_rejects_unknown_provider(self):
        r = subprocess.run(
            [sys.executable, str(REFRESH_PY), "--provider", "bogus-vendor"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(r.returncode, 3, f"unknown provider must exit 3, got {r.returncode}")
        self.assertIn("bogus-vendor", r.stderr + r.stdout)


class TestSkillFrontmatter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL_MD.read_text(encoding="utf-8")
        cls.fm = _frontmatter(cls.text)
        for raw, key in (("WebFetch", "disallowed-tools"),):
            cls._has_disallow_raw = raw
            cls._has_disallow_key = key

    def test_skill_md_exists(self):
        self.assertTrue(SKILL_MD.is_file(), f"missing: {SKILL_MD}")

    def test_directory_matches_frontmatter_name(self):
        self.assertEqual(SKILL_DIR.name, self.fm.get("name"))

    def test_category_is_allowed(self):
        allowed = {"bootstrap", "plan", "build", "review", "security",
                   "audit", "shortcuts", "ship", "config", "eval",
                   "onboard", "repair", "status"}
        self.assertIn(self.fm.get("category"), allowed)

    def test_disallowed_tools_includes_webfetch(self):
        disallowed = self.fm.get("disallowed-tools", "")
        self.assertIn("WebFetch", disallowed,
                      "WebFetch must be disallowed (repo policy)")

    def test_disallowed_tools_includes_write_and_edit(self):
        # Body delegates all writes to scripts/refresh.py via Bash; SKILL must not
        # write files directly.
        disallowed = self.fm.get("disallowed-tools", "")
        self.assertIn("Write", disallowed)
        self.assertIn("Edit", disallowed)

    def test_model_specified(self):
        self.assertIn("model:", self.text)
        model = self.fm.get("model", "")
        self.assertIn(model, {"opus", "sonnet", "haiku"})

    def test_user_invocable_true(self):
        self.assertEqual(self.fm.get("user-invocable"), "true")

    def test_no_h1_in_body(self):
        # rules/skill-authoring.md: "no H1 — title is in frontmatter".
        # The conventional "# /dev-kit:<skill> — ..." title line is allowed
        # (matches ci-doctor, codex-cache-update, etc.); only a body-section H1
        # is forbidden.
        body = self.text.split("---", 2)[-1] if self.text.startswith("---") else self.text
        title_line = re.search(r"^# /dev-kit:\S+", body, re.MULTILINE)
        if title_line is None:
            self.skipTest("no /dev-kit: H1 title to permit; skipping body-H1 assertion shape")
            return
        non_title_h1 = re.findall(r"^# [^/].+", body, re.MULTILINE)
        bad = [h for h in non_title_h1 if not h.lstrip("# ").startswith("/dev-kit:")]
        self.assertEqual(bad, [],
                         f"SKILL.md body has non-title H1: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
