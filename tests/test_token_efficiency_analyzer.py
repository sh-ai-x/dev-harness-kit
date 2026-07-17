#!/usr/bin/env python3
"""
test_token_efficiency_analyzer.py — Coverage for the token-analyzer skill.

Tests:
- Pure scoring rubric (stepped cache curve, weights, letter grade)
- Cost Gate evaluation (tokens + USD thresholds, bad status escalation)
- Per-warning $ attribution across all 3 reclaim axes
- Per-axis reclaim helpers (cache_miss / dup_read / model_downgrade)
- Pricing override merge + unknown-model warn
- JSON output shape + exit code 3 on bad gate
- End-to-end HTML render (every new panel + per-session Tools column)
- Stdout/stderr separation (WARN lines never leak into [ok] contract)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from token_efficiency_analyzer import (  # noqa: E402
    DEFAULT_COST_GATE_TOKENS,
    DEFAULT_COST_GATE_USD,
    DEFAULT_PRICING_KEY,
    PRICING,
    WARNING_RECOMMENDATIONS,
    _KNOWN_SOURCES,
    _aggregate_worktree_rows,
    _source_for,
    aggregate_session,
    cache_miss_reclaim,
    cost_gate_stderr_lines,
    cost_usd,
    discover_logs,
    dup_read_reclaim,
    enforce_cost_gate,
    estimated_savings,
    evaluate_warnings,
    filter_sessions,
    grade_for,
    load_pricing_override,
    main,
    model_downgrade_reclaim,
    pricing_for,
    render_dashboard,
    score_cache_utilization,
    score_session,
    worktree_from_cwd,
    worktree_from_path,
)

FIXTURE_LOGS = PROJECT_ROOT / "fixtures" / "logs" / "claude-code"


def _make_session(**overrides) -> dict:
    """Build a minimal session dict for unit tests (no JSONL needed)."""
    base = {
        "session_id": overrides.get("session_id", "test-session-id"),
        "source": "claude-code",
        "repo": overrides.get("repo", "test-repo"),
        "branch": overrides.get("branch", "main"),
        "worktree": overrides.get("worktree", "(main)"),
        "model": overrides.get("model", "claude-sonnet-5"),
        "first_ts": None,
        "last_ts": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "ephemeral_5m": 0,
        "ephemeral_1h": 0,
        "tool_counts": {},
        "read_files": {},
        "user_texts": [],
        "log_path": "/tmp/fake.jsonl",
    }
    base.update(overrides)
    return base


class TestScoreCacheUtilization(unittest.TestCase):
    """Stepped curve: 0 -> 0, 0.50 -> 50, 0.85 -> 100, >0.85 -> 100."""

    def test_zero_hit(self):
        self.assertEqual(score_cache_utilization(0.0), 0.0)

    def test_warn_threshold(self):
        self.assertEqual(score_cache_utilization(0.50), 50.0)

    def test_full_threshold(self):
        self.assertEqual(score_cache_utilization(0.85), 100.0)

    def test_above_full(self):
        self.assertEqual(score_cache_utilization(1.0), 100.0)

    def test_below_warn_slope_1_to_1(self):
        # Below 0.50, slope is 1:1 (1 unit ratio = 1 unit score).
        self.assertEqual(score_cache_utilization(0.25), 25.0)
        self.assertEqual(score_cache_utilization(0.10), 10.0)

    def test_between_warn_and_full(self):
        # 0.675 -> 50 + (0.175 * 142.857...) = 75
        score = score_cache_utilization(0.675)
        self.assertGreater(score, 70.0)
        self.assertLess(score, 80.0)


class TestGradeFor(unittest.TestCase):
    """A: 90+, B: 80+, C: 70+, D: 60+, F: <60."""

    def test_a(self):
        self.assertEqual(grade_for(95), "A")
        self.assertEqual(grade_for(90), "A")

    def test_b(self):
        self.assertEqual(grade_for(89), "B")
        self.assertEqual(grade_for(80), "B")

    def test_c(self):
        self.assertEqual(grade_for(79), "C")
        self.assertEqual(grade_for(70), "C")

    def test_d(self):
        self.assertEqual(grade_for(69), "D")
        self.assertEqual(grade_for(60), "D")

    def test_f(self):
        self.assertEqual(grade_for(59), "F")
        self.assertEqual(grade_for(0), "F")


class TestPricingFor(unittest.TestCase):
    def test_opus_substring(self):
        p = pricing_for("claude-opus-4-7")
        self.assertEqual(p["in"], PRICING["opus"]["in"])

    def test_sonnet_substring(self):
        p = pricing_for("claude-sonnet-5")
        self.assertEqual(p["in"], PRICING["sonnet"]["in"])

    def test_haiku_substring(self):
        p = pricing_for("claude-haiku-4-5")
        self.assertEqual(p["in"], PRICING["haiku"]["in"])

    def test_minimax_substring(self):
        # MiniMax tier must be matched by substring (covers MiniMax-M3,
        # MiniMax-M2.7, and any future variant) and NOT fall through to a
        # Claude tier via DEFAULT_PRICING_KEY.
        for mid in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
            p = pricing_for(mid)
            self.assertEqual(p["in"], PRICING["minimax"]["in"],
                             f"model {mid!r} did not route to minimax tier")
            self.assertEqual(p["out"], PRICING["minimax"]["out"])

    def test_minimax_routed_before_claude_tiers(self):
        # If a future "minimax-sonnet" variant exists, it must NOT match
        # the sonnet substring — Sonnet input is 10x more expensive than
        # MiniMax-M3 input, so misrouting would silently inflate costs.
        unknown: set[str] = set()
        p = pricing_for("minimax-sonnet", _unknown_models=unknown)
        self.assertEqual(unknown, set(),
                         "minimax-sonnet must resolve to minimax tier, not sonnet")
        self.assertEqual(p["in"], PRICING["minimax"]["in"])

    def test_minimax_known_after_pricing_add(self):
        """MiniMax-M3 now has its own tier — was previously unknown and
        silently fell back to sonnet pricing. Verify (a) it routes to
        PRICING['minimax'] and (b) it is NOT collected as unknown."""
        unknown: set[str] = set()
        p = pricing_for("MiniMax-M3", _unknown_models=unknown)
        self.assertNotIn("MiniMax-M3", unknown)
        self.assertEqual(p["in"], PRICING["minimax"]["in"])

    def test_gpt_5_codex_routes_before_gpt_5(self):
        self.assertEqual(
            pricing_for("gpt-5-codex-2025-08-07"),
            PRICING["gpt-5-codex"],
        )

    def test_gpt_5(self):
        self.assertEqual(pricing_for("gpt-5"), PRICING["gpt-5"])

    def test_openai_model_tiers(self):
        for model_id in ("gpt-4.1", "gpt-4o", "o3", "o4-mini"):
            with self.subTest(model_id=model_id):
                self.assertEqual(pricing_for(model_id), PRICING[model_id])

    def test_gpt_5_6_family_routes_before_gpt_5(self):
        # gpt-5 is a substring of gpt-5.6, so the matcher MUST check the
        # longer gpt-5.6-* keys first — otherwise every gpt-5.6 session
        # silently under-bills at the 4x cheaper legacy gpt-5 rate.
        for model_id, tier in (
            ("gpt-5.6-sol",   "gpt-5.6-sol"),
            ("gpt-5.6-terra", "gpt-5.6-terra"),
            ("gpt-5.6-luna",  "gpt-5.6-luna"),
        ):
            with self.subTest(model_id=model_id):
                self.assertIn(
                    tier, PRICING,
                    f"PRICING must have a dedicated row for {tier!r} "
                    f"(see developers.openai.com/api/docs/pricing)"
                )
                self.assertEqual(
                    pricing_for(model_id), PRICING[tier],
                    f"{model_id!r} must resolve to {tier!r}, not to the "
                    f"legacy gpt-5 tier (which is 4x cheaper and would "
                    f"silently under-bill codex gpt-5.6 sessions).",
                )
                self.assertNotEqual(
                    pricing_for(model_id), PRICING["gpt-5"],
                    f"{model_id!r} must NOT fall through to gpt-5",
                )

    def test_gpt_5_6_not_collected_as_unknown(self):
        # Code path that bug-reporters hit: an analyzer WARN line should
        # never claim gpt-5.6-* are unknown. They must be silently routed.
        unknown: set[str] = set()
        for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            with self.subTest(model_id=model_id):
                pricing_for(model_id, _unknown_models=unknown)
        self.assertEqual(
            unknown, set(),
            f"gpt-5.6-* ids must NOT be collected as unknown: {unknown!r}",
        )

    def test_unknown_openai_model_collects_and_falls_back(self):
        unknown: set[str] = set()
        model_id = "gpt-99-totally-fake"
        self.assertEqual(
            pricing_for(model_id, _unknown_models=unknown),
            PRICING[DEFAULT_PRICING_KEY],
        )
        self.assertEqual(unknown, {model_id})

    def test_unknown_collects(self):
        # An id matching no tier must be collected AND fall back to sonnet.
        unknown: set[str] = set()
        p = pricing_for("totally-unrecognized-model-abc", _unknown_models=unknown)
        self.assertIn("totally-unrecognized-model-abc", unknown)
        self.assertEqual(p["in"], PRICING["sonnet"]["in"])

    def test_empty_falls_back(self):
        unknown: set[str] = set()
        p = pricing_for("", _unknown_models=unknown)
        self.assertEqual(unknown, set())
        self.assertEqual(p["in"], PRICING["sonnet"]["in"])


class TestLoadPricingOverride(unittest.TestCase):
    def test_no_path_is_noop(self):
        before = dict(PRICING)
        load_pricing_override(None)
        self.assertEqual(PRICING, before)

    def test_merge_overrides_tier(self):
        before_opus_in = PRICING["opus"]["in"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"opus": {"in": 99.99}}, f)
            path = Path(f.name)
        try:
            load_pricing_override(path)
            self.assertEqual(PRICING["opus"]["in"], 99.99)
            # Other fields preserved
            self.assertEqual(PRICING["opus"]["out"], PRICING["opus"]["out"])
        finally:
            path.unlink()
            PRICING["opus"]["in"] = before_opus_in

    def test_missing_file_silently_noop(self):
        load_pricing_override(Path("/tmp/does-not-exist-token-analyzer.json"))


class TestCostUsd(unittest.TestCase):
    def test_basic_sonnet(self):
        # 1M input + 1M output + 1M cache_read on Sonnet
        cost = cost_usd("claude-sonnet-5",
                        input_tokens=1_000_000,
                        output_tokens=1_000_000,
                        cache_read_tokens=1_000_000)
        self.assertAlmostEqual(cost, 3.00 + 15.00 + 0.30, places=4)

    def test_5m_vs_1h_ttl_split(self):
        # 1M tokens written at 5m should be 1.25x in; at 1h should be 2.0x in.
        a = cost_usd("claude-sonnet-5",
                     input_tokens=0, output_tokens=0,
                     cache_write_5m_tokens=1_000_000,
                     cache_read_tokens=0)
        b = cost_usd("claude-sonnet-5",
                     input_tokens=0, output_tokens=0,
                     cache_write_1h_tokens=1_000_000,
                     cache_read_tokens=0)
        # Sonnet: in = 3.00
        self.assertAlmostEqual(a, 3.75, places=4)   # 1.25x
        self.assertAlmostEqual(b, 6.00, places=4)   # 2.0x

    def test_gpt_5_codex_input_cost(self):
        self.assertAlmostEqual(
            cost_usd("gpt-5-codex", input_tokens=1_000_000, output_tokens=0),
            1.25,
        )

    def test_gpt_5_codex_output_cost(self):
        self.assertAlmostEqual(
            cost_usd("gpt-5-codex", input_tokens=0, output_tokens=1_000_000),
            10.00,
        )

    def test_gpt_5_codex_cache_read_cost(self):
        self.assertAlmostEqual(
            cost_usd(
                "gpt-5-codex",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=1_000_000,
            ),
            0.625,
        )


class TestEvaluateWarnings(unittest.TestCase):
    def test_cache_hit_low_fires(self):
        s = _make_session(
            session_id="sid-cache-low",
            input_tokens=10_000, cache_read_tokens=1_000,  # hit = 1/11 ~= 0.091
            model="claude-sonnet-5",
        )
        sc = score_session(s)
        warns = evaluate_warnings(s, sc, reclaim_cache_miss=1.23, reclaim_dup_read=0.0, reclaim_downgrade=0.0)
        codes = [w.code for w in warns]
        self.assertIn("CACHE_HIT_LOW", codes)
        low = next(w for w in warns if w.code == "CACHE_HIT_LOW")
        self.assertEqual(low.reclaim_axis, "cache_miss")
        self.assertEqual(low.priority, 1)
        self.assertEqual(low.estimated_save_usd, 1.23)
        # Evidence must name the actual hit ratio, not a generic paragraph.
        self.assertEqual(low.session_id, "sid-cache-low")
        self.assertIn("9%", low.evidence)
        self.assertIn("85%", low.evidence)

    def test_read_heavy_fires_with_dup_read_attribution(self):
        # Repeatedly reading the same file should trigger READ_HEAVY.
        s = _make_session(
            session_id="sid-read-heavy",
            input_tokens=1000, output_tokens=200,
            tool_counts={"Read": 10, "Bash": 1},
            read_files={"/repo/big.py": 9},
            model="claude-sonnet-5",
        )
        sc = score_session(s)
        warns = evaluate_warnings(s, sc, reclaim_cache_miss=0.0, reclaim_dup_read=0.42, reclaim_downgrade=0.0)
        codes = [w.code for w in warns]
        self.assertIn("READ_HEAVY", codes)
        rh = next(w for w in warns if w.code == "READ_HEAVY")
        self.assertEqual(rh.reclaim_axis, "dup_read")
        self.assertEqual(rh.estimated_save_usd, 0.42)
        # Evidence must name the actual offending file + count.
        self.assertEqual(rh.session_id, "sid-read-heavy")
        self.assertIn("/repo/big.py", rh.evidence)
        self.assertIn("9x", rh.evidence)

    def test_model_overspec_only_for_opus_with_low_density(self):
        s = _make_session(session_id="sid-overspec", model="claude-opus-4-7",
                          input_tokens=10_000, output_tokens=50, cache_read_tokens=0)
        sc = score_session(s)
        warns = evaluate_warnings(s, sc, reclaim_cache_miss=0.0, reclaim_dup_read=0.0, reclaim_downgrade=4.5)
        codes = [w.code for w in warns]
        self.assertIn("MODEL_OVERSPEC", codes)
        mo = next(w for w in warns if w.code == "MODEL_OVERSPEC")
        self.assertEqual(mo.reclaim_axis, "model_downgrade")
        self.assertEqual(mo.estimated_save_usd, 4.5)
        self.assertEqual(mo.session_id, "sid-overspec")
        self.assertIn("opus", mo.evidence)

    def test_repeated_user_msg_fires(self):
        s = _make_session(
            session_id="sid-repeated",
            user_texts=["please continue, fix the loop above"] * 3,
            model="claude-sonnet-5",
        )
        sc = score_session(s)
        warns = evaluate_warnings(s, sc)
        codes = [w.code for w in warns]
        self.assertIn("REPEATED_USER_MSG", codes)
        rm = next(w for w in warns if w.code == "REPEATED_USER_MSG")
        self.assertEqual(rm.session_id, "sid-repeated")
        # Evidence cites the actual repeated text and its count, not a
        # generic paragraph.
        self.assertIn("please continue", rm.evidence)
        self.assertIn("3", rm.evidence)


class TestReclaimAxes(unittest.TestCase):
    def test_cache_miss_reclaim_zero_when_above_target(self):
        s = _make_session(input_tokens=100, cache_read_tokens=900)  # hit = 0.90 > 0.85
        sc = score_session(s)
        self.assertEqual(cache_miss_reclaim([(s, sc)]), [0.0])

    def test_cache_miss_reclaim_positive_below_target(self):
        s = _make_session(model="claude-sonnet-5",
                          input_tokens=10_000, cache_read_tokens=1_000)  # hit ~= 0.091
        sc = score_session(s)
        saves = cache_miss_reclaim([(s, sc)])
        self.assertGreater(saves[0], 0.0)

    def test_dup_read_reclaim_uses_default_2k_per_dup(self):
        # 1 file read 4x -> 3 dups * 2000 = 6000 tokens * sonnet_in
        s = _make_session(model="claude-sonnet-5",
                          read_files={"/r/x.py": 4},
                          output_tokens=1_000)
        sc = score_session(s)
        saves = dup_read_reclaim([(s, sc)])
        # 6000 * 3.00 / 1_000_000 = 0.018, rounded to 0.02 by the helper.
        self.assertAlmostEqual(saves[0], 0.02, places=2)

    def test_model_downgrade_only_for_opus_low_density(self):
        s_opus = _make_session(model="claude-opus-4-7",
                               input_tokens=1_000_000, output_tokens=1_000,
                               cache_read_tokens=0)
        sc_opus = score_session(s_opus)
        self.assertGreater(model_downgrade_reclaim([(s_opus, sc_opus)])[0], 0.0)

        s_sonnet = _make_session(model="claude-sonnet-5",
                                 input_tokens=1_000_000, output_tokens=1_000)
        sc_sonnet = score_session(s_sonnet)
        self.assertEqual(model_downgrade_reclaim([(s_sonnet, sc_sonnet)]), [0.0])

    def test_estimated_savings_dict_keys(self):
        s = _make_session(model="claude-sonnet-5",
                          input_tokens=10_000, cache_read_tokens=1_000,
                          read_files={"/x.py": 3})
        sc = score_session(s)
        out = estimated_savings([(s, sc)])
        self.assertEqual(set(out.keys()), {"cache_miss", "dup_read", "model_downgrade", "total"})
        # total = sum of others
        self.assertAlmostEqual(out["total"], out["cache_miss"] + out["dup_read"] + out["model_downgrade"], places=4)


class TestEnforceCostGate(unittest.TestCase):
    def test_ok_when_under_thresholds(self):
        s = _make_session(input_tokens=100, cache_read_tokens=100)
        sc = score_session(s)
        status, violations = enforce_cost_gate([(s, sc)], 1_000_000, 1000.0)
        self.assertEqual(status, "ok")
        self.assertEqual(violations, [])

    def test_warn_on_single_threshold_breach(self):
        s = _make_session(input_tokens=300_000, cache_read_tokens=0)
        sc = score_session(s)
        status, violations = enforce_cost_gate([(s, sc)], 200_000, 5.0)
        self.assertEqual(status, "warn")
        self.assertEqual(len(violations), 1)
        self.assertIn("input=300,000", violations[0]["reason"])

    def test_bad_on_huge_breach(self):
        s = _make_session(input_tokens=10_000_000, cache_read_tokens=0)
        sc = score_session(s)
        status, violations = enforce_cost_gate([(s, sc)], 200_000, 5.0)
        self.assertEqual(status, "bad")
        self.assertEqual(len(violations), 1)

    def test_stderr_lines_have_warn_prefix(self):
        s = _make_session(input_tokens=300_000, cache_read_tokens=0)
        sc = score_session(s)
        _, violations = enforce_cost_gate([(s, sc)], 200_000, 5.0)
        lines = cost_gate_stderr_lines(violations)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("WARN:"))


class TestFixtures(unittest.TestCase):
    """End-to-end: each fixture JSONL must produce its target warning code."""

    FIXTURE_TO_CODE = {
        "aaaa-low-cache.jsonl":      "CACHE_HIT_LOW",
        "bbbb-read-heavy.jsonl":     "READ_HEAVY",
        "cccc-heavy-ctx.jsonl":      "HEAVY_CONTEXT",
        "dddd-opus-typo.jsonl":      "MODEL_OVERSPEC",
        "eeee-write-not-reused.jsonl": "WRITE_NOT_REUSED",
        "ffff-repeated-msg.jsonl":   "REPEATED_USER_MSG",
    }

    def setUp(self):
        if not FIXTURE_LOGS.exists():
            self.skipTest(f"fixture dir missing: {FIXTURE_LOGS}")

    def test_each_fixture_aggregates_and_scores(self):
        for fname, code in self.FIXTURE_TO_CODE.items():
            with self.subTest(fixture=fname):
                s = aggregate_session(FIXTURE_LOGS / fname)
                self.assertIsNotNone(s, f"aggregate_session returned None for {fname}")
                sc = score_session(s)
                self.assertIn("total", sc)
                self.assertIn("grade", sc)
                self.assertIn(score_session(s)["grade"], "ABCDF")
                # Reclaim helpers do not raise.
                cache_miss_reclaim([(s, sc)])
                dup_read_reclaim([(s, sc)])
                model_downgrade_reclaim([(s, sc)])


class TestDiscoverLogsWorktree(unittest.TestCase):
    """Regression: discover_logs must include sibling worktree logs when
    given a repo_root; otherwise worktree-isolated sessions stay
    invisible to /dev-kit:token-analyzer run from the main checkout.

    Pre-fix bug: ``--logs-dir ./logs`` only scanned the cwd's own
    ``logs/``. Every worktree sits at ``.claude/worktrees/<slug>/``
    with its own ``logs/`` (gitignored, separate files), so sessions
    run in any worktree silently missed the dashboard.
    """

    @staticmethod
    def _touch(p: Path) -> Path:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
        return p

    def test_sibling_worktree_logs_included_when_repo_root_given(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_file = self._touch(root / "logs" / "claude-code" / "m.jsonl")
            wt1_file  = self._touch(root / ".claude" / "worktrees" / "wt1"
                                    / "logs" / "claude-code" / "a.jsonl")
            wt2_file  = self._touch(root / ".claude" / "worktrees" / "wt2"
                                    / "logs" / "claude-code" / "b.jsonl")
            files = discover_logs(root / "logs", repo_root=root)
            self.assertIn(main_file, files)
            self.assertIn(wt1_file, files)
            self.assertIn(wt2_file, files)

    def test_canonical_worktree_logs_are_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wt_file = self._touch(root / ".worktrees" / "wt" / "logs" / "codex" / "w.jsonl")
            self.assertIn(wt_file, discover_logs(root / "logs", repo_root=root))

    def test_no_repo_root_does_not_walk_worktrees(self):
        # Backward compat: positional single-dir contract stays pristine.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_file = self._touch(root / "logs" / "claude-code" / "m.jsonl")
            wt_file   = self._touch(root / ".claude" / "worktrees" / "wt"
                                    / "logs" / "claude-code" / "w.jsonl")
            files = discover_logs(root / "logs")
            self.assertIn(main_file, files)
            self.assertNotIn(wt_file, files)

    def test_missing_repo_root_worktrees_dir_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_file = self._touch(root / "logs" / "claude-code" / "m.jsonl")
            files = discover_logs(root / "logs", repo_root=root)
            self.assertEqual(files, [main_file])


class TestEndToEndDashboard(unittest.TestCase):
    """Run main() against a tmp logs dir, assert HTML + summary."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="token-analyzer-test-"))
        # Copy fixtures into <tmpdir>/logs/claude-code/
        target = self.tmpdir / "logs" / "claude-code"
        target.mkdir(parents=True)
        for f in FIXTURE_LOGS.glob("*.jsonl"):
            shutil.copy(f, target / f.name)
        self.out_html = self.tmpdir / "dashboard.html"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_html_contains_every_new_section(self):
        rc = main([
            "--repo", "fixture-repo",
            "--days", "30",
            "--logs-dir", str(self.tmpdir / "logs"),
            "--out", str(self.out_html),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(self.out_html.exists())
        html_text = self.out_html.read_text()
        for needle in ("Cost Gate:", "Cost by Model", "Cache TTL Mix",
                       "ROI Actions", "Recommended Optimizations",
                       "class=\"grade grade-", "Tools</th>"):
            self.assertIn(needle, html_text, f"missing section: {needle}")

    def test_other_repo_sessions_excluded_when_repo_flag_set(self):
        """--repo must scope EVERY aggregate panel — Cost by Repository,
        Cost by Branch, Cost by Worktree, Cost by Model, the sessions
        tables — to sessions whose cwd basename matches ``--repo``.
        Sessions from any other project must never appear in the HTML.

        Regression: ``main()`` used to build ``windowed`` (the
        ``all_sessions_in_window`` pool feeding all panels) with an empty
        repo filter, so a multi-repo logs root leaked other-repo rows
        into the per-repo panel even when ``--repo this-project`` was set.
        """
        target = self.tmpdir / "logs" / "claude-code"
        other_dir = self.tmpdir / "logs-other" / "claude-code"
        other_dir.mkdir(parents=True)
        # Reuse the aaaa-low-cache fixture as the "this-project" session
        # (its cwd = /tmp/fixture-repo). Add a NEW fixture for
        # "other-project" — one minimal session under a different cwd
        # basename. This makes the "other-project" string trivial to grep
        # for in the rendered HTML.
        other_session_id = "gggg-other-project-session"
        sample = (
            '{"timestamp":"2026-07-09T10:00:00.000Z",'
            '"message":{"role":"user","content":"echo"},'
            '"type":"user","sessionId":"' + other_session_id + '",'
            '"cwd":"/tmp/other-project","gitBranch":"main",'
            '"userType":"external","version":"test"}\n'
            '{"timestamp":"2026-07-09T10:00:01.000Z",'
            '"message":{"id":"m1","type":"message","role":"assistant",'
            '"content":[{"type":"text","text":"hi"}],"model":"claude-haiku-4-5",'
            '"stop_reason":"end_turn","usage":{"input_tokens":100,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
            '"output_tokens":10}},'
            '"type":"assistant","sessionId":"' + other_session_id + '",'
            '"cwd":"/tmp/other-project","gitBranch":"main",'
            '"userType":"external","version":"test"}\n'
        )
        (other_dir / "other-project.jsonl").write_text(sample)
        # Also drop the same other-project file into the main logs dir so
        # aggregate_session() can pick it up in the same scan run.
        (target / "other-project.jsonl").write_text(sample)

        rc = main([
            "--repo", "fixture-repo",
            "--days", "30",
            "--logs-dir", str(self.tmpdir / "logs"),
            "--no-include-worktree-logs",
            "--out", str(self.out_html),
        ])
        self.assertEqual(rc, 0)
        html_text = self.out_html.read_text()
        # "other-project" must not appear ANYWHERE in the rendered
        # dashboard — not in the Cost by Repository row, not in any
        # worktree label, not in any session branch/cwd cell.
        # (We grep the rendered HTML because aggregate scoping is a
        # presentation concern; if a session never reaches the panels,
        # its branch/worktree label never gets rendered either.)
        self.assertNotIn(
            "other-project", html_text,
            "Other-project sessions leaked into the dashboard despite "
            "--repo fixture-repo being set. The Cost-by-Repository/Branch/"
            "Worktree pool must be repo-scoped, not time-window-only.",
        )
        # And the in-scope session count from JSON output must also be
        # scoped to fixture-repo only (no double-count).
        import io
        from contextlib import redirect_stdout, redirect_stderr
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "30",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--no-include-worktree-logs",
                "--json",
            ])
        self.assertEqual(rc, 0)
        data = json.loads(stdout_buf.getvalue())
        # fixture-repo fixtures = 6 sessions (FIXTURE_LOGS copy).
        # The other-project file adds 1 session to the directory but must
        # be excluded by --repo fixture-repo, so total stays at 6.
        self.assertEqual(
            data["sessions"], 6,
            f"--repo must scope sessions to 6 (fixture-repo only); got "
            f"{data['sessions']} which means other-project sessions leaked "
            f"into the total cost / savings aggregation.",
        )

    def test_stderr_warn_does_not_leak_into_stdout(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "30",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--out", str(self.out_html),
            ])
        self.assertEqual(rc, 0)
        # Stdout has the [ok] summary lines, NEVER the WARN lines.
        self.assertIn("[ok]", stdout_buf.getvalue())
        self.assertNotIn("WARN:", stdout_buf.getvalue())
        # Stderr may have WARN lines from cost gate.
        self.assertIn("WARN:", stderr_buf.getvalue())


class TestJsonOutput(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="token-analyzer-json-"))
        target = self.tmpdir / "logs" / "claude-code"
        target.mkdir(parents=True)
        for f in FIXTURE_LOGS.glob("*.jsonl"):
            shutil.copy(f, target / f.name)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_json_emits_expected_keys(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "30",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--json",
            ])
        self.assertEqual(rc, 0)
        data = json.loads(stdout_buf.getvalue())
        self.assertEqual(data["repo"], "fixture-repo")
        self.assertEqual(data["days"], 30)
        self.assertEqual(data["sessions"], 6)
        self.assertEqual(data["branch"], "")
        self.assertFalse(data["branch_filter_active"])
        self.assertEqual(set(data["estimated_savings_usd"].keys()),
                         {"cache_miss", "dup_read", "model_downgrade", "total"})
        self.assertIn("cost_gate", data)
        self.assertIn(data["cost_gate"]["status"], ("ok", "warn", "bad"))
        self.assertIsInstance(data["warnings"], list)
        # Active/inactive split — all 6 fixtures run under a plain cwd with
        # no .claude/worktrees/ segment, so worktree_state stamps "main"
        # (active) for every one of them; none are merged/gone.
        self.assertEqual(data["active_sessions"], 6)
        self.assertEqual(data["inactive_sessions"], 0)
        self.assertEqual(data["active_sessions"] + data["inactive_sessions"], data["sessions"])
        # Each warning instance must carry per-session evidence, not just a code.
        for w in data["warnings"]:
            self.assertIn("evidence", w)
            self.assertIsInstance(w["evidence"], str)

    def test_json_exit_code_3_on_bad_gate(self):
        rc = main([
            "--repo", "fixture-repo",
            "--days", "30",
            "--logs-dir", str(self.tmpdir / "logs"),
            "--cost-gate-tokens", "1000",   # every session will breach
            "--json",
        ])
        self.assertEqual(rc, 3)

    def test_json_empty_logs_returns_2(self):
        empty = self.tmpdir / "empty"
        (empty / "claude-code").mkdir(parents=True)
        # --no-include-worktree-logs scopes discovery to the test's empty
        # tempdir; without it the analyzer auto-walks Path.cwd()/.claude/
        # worktrees/*/logs/ on the test machine, finds live JSONL, and
        # returns 0 instead of the expected "empty → 2".
        rc = main([
            "--repo", "fixture-repo",
            "--days", "30",
            "--logs-dir", str(empty),
            "--no-include-worktree-logs",
            "--json",
        ])
        self.assertEqual(rc, 2)


class TestWeightInvariants(unittest.TestCase):
    def test_score_session_uses_40_20_20_20(self):
        s = _make_session(input_tokens=100, cache_read_tokens=900,
                          output_tokens=50, tool_counts={"Read": 1},
                          read_files={})
        sc = score_session(s)
        # cache_hit = 0.90 -> score_cache_utilization returns 100
        # density: 50/1000 * 400 = 20
        # redundancy: 100 (max_repeat = 1 -> 100 - 0*12.5)
        # economy: depends on tools/output but should be <=100
        expected_cache = 100.0
        self.assertEqual(sc["cache"], expected_cache)
        self.assertAlmostEqual(sc["density"], 20.0)
        # Now compute expected total using the new weights
        expected = round(0.40 * sc["cache"] + 0.20 * sc["density"]
                         + 0.20 * sc["redundancy"] + 0.20 * sc["economy"], 1)
        self.assertEqual(sc["total"], expected)


class TestBranchAwareness(unittest.TestCase):
    """Per-branch discovery, extraction, and filtering."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="token-analyzer-branch-"))
        self._now = None  # filled per-test if needed

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_session(self, subdir: str, sid: str, *,
                       branch: str | None = "main",
                       cwd: str = "/tmp/fixture-repo") -> Path:
        """Write one minimal session record under logs/claude-code/<subdir>/."""
        d = self.tmpdir / "logs" / "claude-code" / subdir
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "type": "assistant",
            "sessionId": sid,
            "cwd": cwd,
            "timestamp": "2026-07-09T10:00:00.000Z",
            "gitBranch": branch,
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 500,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                    },
                },
            },
        }
        # If branch is None, drop the field entirely (simulates wire-format omission).
        if branch is None:
            del rec["gitBranch"]
        p = d / f"{sid}.jsonl"
        p.write_text(json.dumps(rec) + "\n")
        return p

    def test_source_for_walks_up_for_nested(self):
        nested = Path("/tmp/foo/logs/claude-code/main/sid.jsonl")
        self.assertEqual(_source_for(nested), "claude-code")
        flat = Path("/tmp/foo/logs/claude-code/sid.jsonl")
        self.assertEqual(_source_for(flat), "claude-code")
        self.assertEqual(set(_KNOWN_SOURCES), {"claude-code", "codex"})

    def test_discover_logs_walks_recursively(self):
        self._write_session("main", "s1", branch="main")
        self._write_session("feature-x", "s2", branch="feature-x")
        # Legacy flat file alongside the nested ones.
        flat = self.tmpdir / "logs" / "claude-code" / "legacy.jsonl"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_text("{}\n")
        names = {p.name for p in discover_logs(self.tmpdir / "logs")}
        self.assertEqual(names, {"s1.jsonl", "s2.jsonl", "legacy.jsonl"})

    def test_aggregate_session_extracts_branch_from_wire_format(self):
        p = self._write_session("main", "sid-w", branch="main")
        s = aggregate_session(p)
        self.assertEqual(s["branch"], "main")
        self.assertEqual(s["source"], "claude-code")

    def test_aggregate_session_source_from_top_level_subdir(self):
        # Path .../logs/claude-code/main/sid.jsonl — parent.name is "main"
        # but source must remain "claude-code" (not the branch dir).
        p = self._write_session("main", "sid-src", branch="main")
        s = aggregate_session(p)
        self.assertEqual(s["source"], "claude-code")
        self.assertEqual(s["branch"], "main")

    def test_aggregate_session_branch_fallback_to_path_when_no_wire(self):
        p = self._write_session("release-1.0", "sid-no-wire", branch=None)
        s = aggregate_session(p)
        self.assertEqual(s["branch"], "release-1.0")
        self.assertEqual(s["source"], "claude-code")

    def test_aggregate_session_flat_legacy_buckets_as_main(self):
        flat = self.tmpdir / "logs" / "claude-code" / "legacy.jsonl"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_text(json.dumps({
            "type": "assistant",
            "sessionId": "x",
            "cwd": "/tmp/fixture-repo",
            "timestamp": "2026-07-09T10:00:00.000Z",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1000, "output_tokens": 100,
                                  "cache_read_input_tokens": 500}},
        }) + "\n")
        s = aggregate_session(flat)
        # Flat layout: parent.name == source tool subdir → branch buckets to "main".
        self.assertEqual(s["branch"], "main")
        self.assertEqual(s["source"], "claude-code")

    def test_filter_sessions_branch_substring_match(self):
        from datetime import datetime, timezone, timedelta
        p1 = self._write_session("main", "s-main", branch="main")
        p2 = self._write_session("feature-x", "s-feat", branch="feature-x")
        sessions = [aggregate_session(p) for p in (p1, p2)]
        # Force last_ts to now so the days filter doesn't drop them.
        now = datetime.now(timezone.utc)
        for s in sessions:
            s["first_ts"] = now
            s["last_ts"] = now
        kept = filter_sessions(sessions, repo="", days=30, branch="feature")
        self.assertEqual([s["session_id"] for s in kept], ["s-feat"])

    def test_filter_sessions_empty_branch_disables_filter(self):
        from datetime import datetime, timezone
        p1 = self._write_session("main", "s-main", branch="main")
        p2 = self._write_session("feature-x", "s-feat", branch="feature-x")
        sessions = [aggregate_session(p) for p in (p1, p2)]
        now = datetime.now(timezone.utc)
        for s in sessions:
            s["first_ts"] = now
            s["last_ts"] = now
        kept = filter_sessions(sessions, repo="", days=30)
        self.assertEqual(len(kept), 2)

    def test_main_branch_filter_json(self):
        from io import StringIO
        import contextlib
        self._write_session("main", "s-main", branch="main")
        self._write_session("feature-x", "s-feat", branch="feature-x")
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "30",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--branch", "feature",
                "--json",
            ])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["branch"], "feature")
        self.assertTrue(data["branch_filter_active"])
        self.assertEqual(data["sessions"], 1)

    def test_main_mixed_flat_and_nested_does_not_crash(self):
        from io import StringIO
        import contextlib
        self._write_session("main", "s-main", branch="main")
        flat = self.tmpdir / "logs" / "claude-code" / "legacy.jsonl"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_text("{}\n")
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "30",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--json",
            ])
        # rc == 0 (legacy flat yields branch="main" which still matches).
        self.assertIn(rc, (0, 2))


class TestWorktreeAwareness(unittest.TestCase):
    """Per-worktree derivation, extraction, aggregation, and filtering."""

    def test_worktree_from_cwd_main_checkout(self):
        self.assertEqual(worktree_from_cwd("/Users/sanghee/dev/dev-harness-kit"), "(main)")
        self.assertEqual(worktree_from_cwd("/tmp/random/path"), "(main)")

    def test_worktree_from_cwd_worktree_checkout(self):
        self.assertEqual(
            worktree_from_cwd("/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/fix-x"),
            "fix-x",
        )
        self.assertEqual(
            worktree_from_cwd("/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/feat-per-branch-log"),
            "feat-per-branch-log",
        )
        self.assertEqual(
            worktree_from_cwd("/Users/sanghee/dev/dev-harness-kit/.worktrees/fix-x"),
            "fix-x",
        )

    def test_worktree_from_cwd_nested_subdir_inside_worktree(self):
        # cwd may point anywhere inside the worktree, not just at its root.
        self.assertEqual(
            worktree_from_cwd(
                "/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/fix-x/tools/sub/dir"
            ),
            "fix-x",
        )

    def test_worktree_from_cwd_missing_returns_unknown(self):
        self.assertEqual(worktree_from_cwd(""), "(unknown)")
        self.assertEqual(worktree_from_cwd(None), "(unknown)")

    def test_worktree_from_cwd_does_not_match_unrelated_claude_dir(self):
        # A non-worktree '.claude' segment elsewhere in the path must not
        # trigger the worktrees branch.
        self.assertEqual(
            worktree_from_cwd("/Users/sanghee/dev/dev-harness-kit/.claude/settings.json"),
            "(main)",
        )

    def test_worktree_from_path_main_checkout_logs(self):
        # File under <repo>/logs/... — main checkout, not a worktree.
        self.assertEqual(
            worktree_from_path("/Users/sanghee/dev/dev-harness-kit/logs/claude-code/main/foo.jsonl"),
            "(main)",
        )
        self.assertEqual(
            worktree_from_path("/Users/sanghee/dev/dev-harness-kit/logs/claude-code/foo.jsonl"),
            "(main)",
        )

    def test_worktree_from_path_sibling_worktree_logs(self):
        # File under <repo>/.claude/worktrees/<name>/logs/... must resolve
        # to <name>, even if cwd in the session transcript says main.
        self.assertEqual(
            worktree_from_path(
                "/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/cost-gate"
                "/logs/claude-code/feat-cost-gate/foo.jsonl"
            ),
            "cost-gate",
        )
        self.assertEqual(
            worktree_from_path(
                "/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/fix-x"
                "/logs/claude-code/main/x.jsonl"
            ),
            "fix-x",
        )
        self.assertEqual(
            worktree_from_path(
                "/Users/sanghee/dev/dev-harness-kit/.worktrees/fix-x"
                "/logs/codex/main/x.jsonl"
            ),
            "fix-x",
        )

    def test_worktree_from_path_empty_or_unrelated_returns_main(self):
        self.assertEqual(worktree_from_path(""), "(main)")
        self.assertEqual(worktree_from_path(None), "(main)")
        self.assertEqual(worktree_from_path("/tmp/random/foo.jsonl"), "(main)")
        # A '.claude' segment not followed by 'worktrees/<name>' stays main.
        self.assertEqual(
            worktree_from_path("/Users/sanghee/dev/dev-harness-kit/.claude/settings.json"),
            "(main)",
        )

    def test_aggregate_session_resolves_worktree_from_file_path(self):
        # Regression: when cwd points at the main checkout but the JSONL
        # file physically lives under .claude/worktrees/<name>/logs/, the
        # worktree bucket must come from the file path (authoritative),
        # not the cwd field (which can be the parent checkout).
        with tempfile.TemporaryDirectory(prefix="wt-path-") as td:
            td_path = Path(td)
            wt_logs = td_path / ".claude" / "worktrees" / "cost-gate" / "logs" / "claude-code" / "feat-cost-gate"
            wt_logs.mkdir(parents=True)
            rec = {
                "type": "assistant",
                "sessionId": "s-wt-path",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",  # wrong: main checkout
                "gitBranch": "feat/cost-gate",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {"role": "assistant", "model": "claude-sonnet-5",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10,
                                      "cache_read_input_tokens": 50,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                         "ephemeral_1h_input_tokens": 0}}},
            }
            p = wt_logs / "s-wt.jsonl"
            p.write_text(json.dumps(rec) + "\n")
            s = aggregate_session(p)
            self.assertEqual(s["worktree"], "cost-gate")
            self.assertEqual(s["branch"], "feat/cost-gate")

    def test_aggregate_session_main_path_uses_cwd_fallback(self):
        # When the file is under the main logs/ dir, fall back to the
        # cwd-derived bucket so the existing main-checkout path keeps working.
        with tempfile.TemporaryDirectory(prefix="wt-path-main-") as td:
            d = Path(td) / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            rec = {
                "type": "assistant",
                "sessionId": "s-main-path",
                "cwd": "/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/fix-x",
                "gitBranch": "fix/x",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {"role": "assistant", "model": "claude-sonnet-5",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10,
                                      "cache_read_input_tokens": 50}},
            }
            p = d / "s-main.jsonl"
            p.write_text(json.dumps(rec) + "\n")
            s = aggregate_session(p)
            self.assertEqual(s["worktree"], "fix-x")

    def test_discover_logs_walks_nested_worktree_dirs(self):
        # Regression: a nested worktree like
        # <root>/.claude/worktrees/A/.claude/worktrees/B/logs/... must be
        # reachable through discover_logs(repo_root=root). Previously only
        # the first level was walked, silently dropping nested captures.
        with tempfile.TemporaryDirectory(prefix="wt-nested-") as td:
            td_path = Path(td)
            a = td_path / ".claude" / "worktrees" / "A"
            b = a / ".claude" / "worktrees" / "B" / "logs" / "claude-code" / "feat-b"
            b.mkdir(parents=True)
            top_level = td_path / "logs" / "claude-code" / "main"
            top_level.mkdir(parents=True)
            nested_jsonl = b / "nested.jsonl"
            top_jsonl = top_level / "top.jsonl"
            nested_jsonl.write_text("{}\n")
            top_jsonl.write_text("{}\n")
            found = discover_logs(td_path / "logs", repo_root=td_path)
            found_str = {str(p) for p in found}
            self.assertIn(str(nested_jsonl), found_str)
            self.assertIn(str(top_jsonl), found_str)

    def test_discover_logs_handles_nested_symlink_cycle(self):
        # A nested .claude/worktrees/ that loops back to itself via symlink
        # must not infinite-recurse; the _seen set caps the walk.
        with tempfile.TemporaryDirectory(prefix="wt-cycle-") as td:
            td_path = Path(td)
            cycle = td_path / ".claude" / "worktrees" / "loop"
            cycle.mkdir(parents=True)
            (cycle / "logs" / "claude-code" / "main").mkdir(parents=True)
            (cycle / "logs" / "claude-code" / "main" / "x.jsonl").write_text("{}\n")
            # Create a symlink inside that points back at the worktrees dir.
            try:
                (cycle / "loop_link").symlink_to(td_path / ".claude" / "worktrees",
                                                 target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported on this fs")
            # Must terminate (no infinite recursion).
            found = discover_logs(td_path / "logs", repo_root=td_path)
            self.assertTrue(any(p.name == "x.jsonl" for p in found))

    def test_aggregate_session_extracts_worktree_from_cwd(self):
        with tempfile.TemporaryDirectory(prefix="wt-agg-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            rec = {
                "type": "assistant",
                "sessionId": "s-wt",
                "cwd": "/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/feat-per-branch-log",
                "gitBranch": "feat/per-branch-log",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "output_tokens": 10,
                              "cache_read_input_tokens": 50,
                              "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                 "ephemeral_1h_input_tokens": 0}},
                },
            }
            p = d / "s-wt.jsonl"
            p.write_text(json.dumps(rec) + "\n")
            s = aggregate_session(p)
            self.assertEqual(s["worktree"], "feat-per-branch-log")
            self.assertEqual(s["branch"], "feat/per-branch-log")

    def test_aggregate_session_main_checkout_buckets_as_main(self):
        with tempfile.TemporaryDirectory(prefix="wt-main-") as td:
            d = Path(td) / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            rec = {
                "type": "assistant",
                "sessionId": "s-main",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {"role": "assistant", "model": "claude-sonnet-5",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10,
                                      "cache_read_input_tokens": 50}},
            }
            p = d / "s-main.jsonl"
            p.write_text(json.dumps(rec) + "\n")
            s = aggregate_session(p)
            self.assertEqual(s["worktree"], "(main)")

    def test_filter_sessions_worktree_substring_match(self):
        from datetime import datetime, timezone
        sessions = [
            _make_session(session_id="s-m", worktree="(main)", repo="repo",
                          last_ts=datetime.now(timezone.utc)),
            _make_session(session_id="s-w", worktree="fix-x", repo="repo",
                          last_ts=datetime.now(timezone.utc)),
        ]
        kept = filter_sessions(sessions, repo="", days=30, worktree="fix")
        self.assertEqual([s["session_id"] for s in kept], ["s-w"])

    def test_filter_sessions_empty_worktree_disables_filter(self):
        from datetime import datetime, timezone
        sessions = [
            _make_session(session_id="s-m", worktree="(main)"),
            _make_session(session_id="s-w", worktree="fix-x"),
        ]
        now = datetime.now(timezone.utc)
        for s in sessions:
            s["first_ts"] = now
            s["last_ts"] = now
        kept = filter_sessions(sessions, repo="", days=30)
        self.assertEqual(len(kept), 2)

    def test_filter_sessions_worktree_and_branch_compose(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        sessions = [
            _make_session(session_id="s-1", branch="main", worktree="(main)", last_ts=now),
            _make_session(session_id="s-2", branch="feat", worktree="(main)", last_ts=now),
            _make_session(session_id="s-3", branch="feat", worktree="feat-per-branch-log", last_ts=now),
        ]
        kept = filter_sessions(sessions, repo="", days=30, branch="feat", worktree="feat")
        self.assertEqual([s["session_id"] for s in kept], ["s-3"])

    def test_main_worktree_filter_json(self):
        from io import StringIO
        import contextlib
        with tempfile.TemporaryDirectory(prefix="wt-main-json-") as td:
            td_path = Path(td)
            # Two sessions: one in main checkout, one in a worktree.
            # Use --repo="" (no repo filter) because a worktree session's
            # ``cwd`` basename IS the worktree dir, not the project root.
            for sid, cwd in (
                ("s-main", "/Users/sanghee/dev/dev-harness-kit"),
                ("s-wt",   "/Users/sanghee/dev/dev-harness-kit/.claude/worktrees/feat-x"),
            ):
                d = td_path / "logs" / "claude-code" / "main"
                d.mkdir(parents=True, exist_ok=True)
                rec = {
                    "type": "assistant",
                    "sessionId": sid,
                    "cwd": cwd,
                    "gitBranch": "main",
                    "timestamp": "2026-07-09T10:00:00.000Z",
                    "message": {"role": "assistant", "model": "claude-sonnet-5",
                                "content": [{"type": "text", "text": "ok"}],
                                "usage": {"input_tokens": 100, "output_tokens": 10,
                                          "cache_read_input_tokens": 50,
                                          "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                             "ephemeral_1h_input_tokens": 0}}},
                }
                (d / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([
                    "--repo", "",
                    "--days", "30",
                    "--logs-dir", str(td_path / "logs"),
                    "--worktree", "feat",
                    # Scope the walk to the test's tempdir; without this the
                    # analyzer auto-discovers Path.cwd()/.claude/worktrees/*/
                    # logs/ on the dev machine and files_scanned picks up
                    # siblings, breaking the strict 2-fixture invariant below.
                    "--no-include-worktree-logs",
                    "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["worktree"], "feat")
            self.assertTrue(data["worktree_filter_active"])
            self.assertEqual(data["sessions"], 1)
            self.assertEqual(data["files_scanned"], 2)
            # And the kept session must carry the worktree field on its warning.
            if data["warnings"]:
                self.assertEqual(data["warnings"][0]["worktree"], "feat-x")

    def test_render_dashboard_contains_worktree_panel_and_column(self):
        from io import StringIO
        import contextlib
        with tempfile.TemporaryDirectory(prefix="wt-render-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            rec = {
                "type": "assistant",
                "sessionId": "s-r",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {"role": "assistant", "model": "claude-sonnet-5",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10,
                                      "cache_read_input_tokens": 50}},
            }
            (d / "s-r.jsonl").write_text(json.dumps(rec) + "\n")
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([
                    "--repo", "dev-harness-kit",
                    "--days", "30",
                    "--logs-dir", str(td_path / "logs"),
                ])
            self.assertEqual(rc, 0)
            html_path = Path("token-dashboard-dev-harness-kit-30d.html")
            try:
                src = html_path.read_text()
                self.assertIn("Cost by Worktree", src)
                self.assertIn("<th>Worktree</th>", src)
                self.assertIn("Inactive Sessions", src)
                # Scope the column count to the Active Sessions table only.
                sessions_thead = src.split('<div class="section-title">Active Sessions', 1)[1].split("</thead>", 1)[0]
                # 12 columns: Session, Branch, Worktree, Model, Started,
                # Input, Output, Tools, Cache Hit, Cost, Score, Warnings.
                # Count open <th...> tags (not <thead>) to skip the wrapper element.
                import re
                self.assertEqual(len(re.findall(r"<th[\s>]", sessions_thead)), 12)
            finally:
                html_path.unlink(missing_ok=True)

    def test_dedupe_dual_write_session(self):
        """Same sessionId in two files -> one counted, cost from fuller copy."""
        import json, tempfile
        from io import StringIO
        import contextlib
        with tempfile.TemporaryDirectory(prefix="dedupe-") as td:
            td_path = Path(td)
            SID = "dedup-test-sid"
            rec = {
                "type": "assistant",
                "sessionId": SID,
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": "2026-07-15T00:00:00.000Z",
                "message": {"role": "assistant", "model": "claude-sonnet-5",
                            "content": [{"type": "text", "text": "hi"}],
                            "usage": {"input_tokens": 100, "output_tokens": 10,
                                      "cache_read_input_tokens": 50,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                         "ephemeral_1h_input_tokens": 0}}},
            }
            # Main-side copy: 1 assistant record (stale, partial snapshot).
            main_dir = td_path / "logs" / "claude-code" / "main"
            main_dir.mkdir(parents=True)
            (main_dir / f"{SID}.jsonl").write_text(json.dumps(rec) + "\n")
            # Worktree-side copy: 5 assistant records (more complete).
            wt_dir = (td_path / "logs" / "claude-code"
                      / "feat-x" / ".claude" / "worktrees" / "feat-x")
            # Note: discover_logs walks <logs_dir>/<source>/** and any
            # nested .claude/worktrees/ dirs, so place worktree copy under
            # <logs_dir>/claude-code/<branch>/.claude/worktrees/<wt>/ to
            # mimic the dual-write layout discovered at runtime.
            wt = td_path / "logs" / "claude-code" / "feat-x" / ".claude" / "worktrees" / "feat-x"
            wt.mkdir(parents=True)
            lines = []
            for i in range(5):
                lines.append(json.dumps({**rec, "timestamp": f"2026-07-15T00:00:0{i}.000Z"}))
            (wt / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([
                    "--repo", "",
                    "--days", "30",
                    "--logs-dir", str(td_path / "logs"),
                    "--no-include-worktree-logs",
                    "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(
                data["sessions"], 1,
                msg=f"dedup should collapse to 1 session; got {data['sessions']}",
            )
            self.assertEqual(
                data["files_scanned"], 1,
                msg="files_scanned reflects the deduped file set, not raw discovery",
            )


class TestWorktreePanelFullCoverage(unittest.TestCase):
    """Cost-by-Worktree panel must list every disk worktree dir, not only
    those that ran a session inside the current --days window.

    Pre-fix bug: the panel iterated only ``worktree_costs`` (built from
    scored sessions). A repo with 99 worktree dirs on disk but sessions
    in only ``(main)`` rendered exactly one row, hiding every other
    worktree's state (live / merged / gone). Users had no way to spot
    stale worktrees that consumed past token spend.
    """

    def test_aggregate_rows_include_disk_only_worktrees(self):
        # One session lives in (main); wt_meta carries two more dirs that
        # did not run a session in the window. Both must appear in the JSON
        # payload as zero-cost rows with state from wt_meta.
        session = {
            "worktree": "(main)",
            "worktree_state": "main",
            "model": "sonnet",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "ephemeral_5m": 0,
            "ephemeral_1h": 0,
            "repo": "dev-harness-kit",
            "branch": "main",
            "tool_counts": {},
        }
        wt_meta = {
            "(main)": {"state": "main", "branch_name": "main",
                        "branch_tip": "abc123",
                        "branch_merged_into_main": False},
            "stale-feature-branch": {"state": "merged", "branch_name": "feat/stale",
                                      "branch_tip": "def456",
                                      "branch_merged_into_main": True},
            "orphan-dir": {"state": "gone", "branch_name": "feat/orphan",
                            "branch_tip": "789abc",
                            "branch_merged_into_main": True},
        }
        rows = _aggregate_worktree_rows([session], wt_meta)
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"(main)", "stale-feature-branch", "orphan-dir"})
        # The two disk-only rows must carry zero cost and authoritative state
        # straight from wt_meta (no session override possible).
        orphan = next(r for r in rows if r["name"] == "orphan-dir")
        self.assertEqual(orphan["state"], "gone")
        self.assertEqual(orphan["sessions"], 0)
        self.assertEqual(orphan["cost_usd"], 0.0)
        self.assertEqual(orphan["branch_name"], "feat/orphan")
        merged = next(r for r in rows if r["name"] == "stale-feature-branch")
        self.assertEqual(merged["state"], "merged")
        self.assertEqual(merged["sessions"], 0)
        self.assertTrue(merged["branch_merged_into_main"])

    def test_aggregate_rows_unaffected_when_wt_meta_empty(self):
        # Existing behavior preserved: when --no-include-worktree-logs or a
        # non-git worktree leaves wt_meta empty, the panel still shows
        # session-derived rows without zero-cost backfill.
        session = {
            "worktree": "(main)",
            "worktree_state": "main",
            "model": "sonnet",
            "input_tokens": 1000, "output_tokens": 200,
            "cache_write_tokens": 0, "cache_read_tokens": 0,
            "ephemeral_5m": 0, "ephemeral_1h": 0,
            "repo": "r", "branch": "main", "tool_counts": {},
        }
        rows = _aggregate_worktree_rows([session], None)
        self.assertEqual([r["name"] for r in rows], ["(main)"])


class TestCacheTtlMixEmpty(unittest.TestCase):
    """Cache TTL Mix panel — collapse behavior when no Anthropic-style
    5m/1h cache_creation split is published (e.g. MiniMax sessions).

    Three states:
      (a) no cache-write activity at all            -> single annotation row
      (b) cache_write_tokens > 0 but no 5m/1h split -> single "TTL unspecified" bar
      (c) full 5m + 1h split                        -> existing 4-bar layout

    The existing render test (above) covers state (a) implicitly via the
    fixture repo (every fixture has cache_creation_input_tokens == 0).
    These tests cover all three states explicitly with synthetic sessions.
    """

    def _run_main_and_read_html(self, td_path, out_html, *, repo="dev-harness-kit"):
        from io import StringIO
        import contextlib
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", repo,
                "--days", "30",
                "--logs-dir", str(td_path / "logs"),
                "--out", str(out_html),
            ])
        self.assertEqual(rc, 0, msg=f"main() failed: {buf.getvalue()}")
        return out_html.read_text()

    def _write_session(self, d, sid, *, git_branch, cwd,
                       cache_creation_input_tokens=0,
                       ephemeral_5m=0, ephemeral_1h=0):
        d.mkdir(parents=True, exist_ok=True)
        cc = {"ephemeral_5m_input_tokens": ephemeral_5m,
              "ephemeral_1h_input_tokens": ephemeral_1h}
        rec = {
            "type": "assistant",
            "sessionId": sid,
            "cwd": cwd,
            "gitBranch": git_branch,
            "timestamp": "2026-07-09T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "cache_creation": cc,
                },
            },
        }
        (d / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")

    def test_state_a_no_cache_write_at_all_renders_annotation(self):
        """cache_creation_input_tokens == 0 across the whole window.

        Expected: a single annotation row replacing the two TTL bars.
        NOT expected: any element with class="bar write5m" or "bar write1h".
        """
        with tempfile.TemporaryDirectory(prefix="ttl-empty-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            self._write_session(d, "empty-1", git_branch="main",
                                cwd="/Users/sanghee/dev/dev-harness-kit")
            out = td_path / "dashboard.html"
            html = self._run_main_and_read_html(td_path, out)

            # Must show the annotation row telling the user there is
            # nothing to attribute, not two empty bars.
            self.assertIn("no cache-write activity", html)
            # The two legacy bars must not appear in their pre-change form.
            self.assertNotIn('class="bar write5m"', html)
            self.assertNotIn('class="bar write1h"', html)
            # cache_read and pure miss are still rendered.
            self.assertIn('class="bar read"', html)
            self.assertIn('class="bar miss"', html)

    def test_state_b_legacy_writes_only_renders_single_combined_bar(self):
        """cache_creation_input_tokens > 0 but no ephemeral 5m/1h split.

        Expected: one combined bar labelled "TTL unspecified" carrying the
        legacy write total. NOT expected: separate 5m/1h bars or the
        empty-state annotation.
        """
        with tempfile.TemporaryDirectory(prefix="ttl-legacy-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            self._write_session(d, "legacy-1", git_branch="main",
                                cwd="/Users/sanghee/dev/dev-harness-kit",
                                cache_creation_input_tokens=4000,
                                ephemeral_5m=0, ephemeral_1h=0)
            out = td_path / "dashboard.html"
            html = self._run_main_and_read_html(td_path, out)

            # Single combined bar shows the legacy total, labeled as TTL
            # unspecified (since neither 5m nor 1h buckets were reported).
            self.assertIn("TTL unspecified", html)
            self.assertIn("4,000", html)  # the legacy token total
            # Two-bar layout must NOT appear, empty annotation must NOT appear.
            self.assertNotIn('class="bar write5m"', html)
            self.assertNotIn('class="bar write1h"', html)
            self.assertNotIn("no cache-write activity", html)

    def test_state_c_full_5m_and_1h_split_keeps_two_rows(self):
        """Both ephemeral_5m and ephemeral_1h buckets populated.

        Expected: the pre-change 4-bar layout — separate write5m +
        write1h rows. NOT expected: collapse or empty-state annotation.
        """
        with tempfile.TemporaryDirectory(prefix="ttl-full-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            # Two sessions to cover both 5m and 1h paths.
            self._write_session(d, "full-5m", git_branch="main",
                                cwd="/Users/sanghee/dev/dev-harness-kit",
                                cache_creation_input_tokens=3000,
                                ephemeral_5m=3000, ephemeral_1h=0)
            self._write_session(d, "full-1h", git_branch="main",
                                cwd="/Users/sanghee/dev/dev-harness-kit",
                                cache_creation_input_tokens=2000,
                                ephemeral_5m=0, ephemeral_1h=2000)
            out = td_path / "dashboard.html"
            html = self._run_main_and_read_html(td_path, out)

            # Two distinct rows, no collapse.
            self.assertIn('class="bar write5m"', html)
            self.assertIn('class="bar write1h"', html)
            # Empty annotation must NOT appear.
            self.assertNotIn("no cache-write activity", html)
            self.assertNotIn("TTL unspecified", html)


class TestWorktreeStaleness(unittest.TestCase):
    """classify_worktree_dir() + dashboard surface (State column, stale chips,
    stale_cost tile, stdout field, JSON keys)."""

    def _git_runner(self, *, porcelain="", tip="abc1234", merged=False, unique_commits="",
                    head_full="", main_full=""):
        """Build a fake ``git_runner`` that responds based on the command argv.

        Matches six subcommand shapes:
          - ``git -C <root> worktree list --porcelain``              → ``porcelain``
          - ``git -C <wt> rev-parse --short HEAD``                   → ``tip``
          - ``git -C <wt> rev-parse HEAD``                           → ``head_full`` (full SHA, default empty)
          - ``git -C <root> rev-parse origin/main``                  → ``main_full`` (full SHA, default empty)
          - ``git -C <wt> log origin/main..HEAD --oneline``          → ``unique_commits``
            (empty stdout ⇒ ``state="merged"``; non-empty ⇒ ``state="live"``)
          - ``git -C <wt> merge-base --is-ancestor …``               → ``merged`` flag (legacy)
        Anything else returns a CompletedProcess with rc=0 and empty output.
        """
        import subprocess

        def fake_run(args, **_kwargs):
            cmd = " ".join(str(a) for a in args)
            if "worktree list --porcelain" in cmd:
                return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
            if "rev-parse --short HEAD" in cmd:
                return subprocess.CompletedProcess(args, 0, stdout=tip, stderr="")
            if "rev-parse origin/main" in cmd:
                return subprocess.CompletedProcess(args, 0, stdout=main_full, stderr="")
            if "rev-parse HEAD" in cmd:
                return subprocess.CompletedProcess(args, 0, stdout=head_full, stderr="")
            if "log origin/main..HEAD" in cmd:
                return subprocess.CompletedProcess(args, 0, stdout=unique_commits, stderr="")
            if "merge-base --is-ancestor" in cmd:
                return subprocess.CompletedProcess(args, 0 if merged else 1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return fake_run

    def test_classify_worktree_dir_no_git_calls_returns_live(self):
        """New contract (post-slow-CI fix): ``classify_worktree_dir`` makes no
        ``subprocess.run`` calls. Every worktree dir under
        ``.claude/worktrees/`` (or any of the other ``WORKTREE_ROOT_NAMES``)
        is reported as ``state="live"``. The dashboard still renders the
        worktree bucket because the basenames land in ``wt_meta``; the
        Stale-Cost tile falls back to non-stale.
        """
        from token_efficiency_analyzer import classify_worktree_dir

        # Spy runner: any invocation is a contract violation.
        def spy_runner(args, **_kwargs):
            raise AssertionError(
                f"classify_worktree_dir made a subprocess call: {args}"
            )

        with tempfile.TemporaryDirectory(prefix="wt-nogit-") as td:
            root = Path(td)
            wt = root / ".claude" / "worktrees" / "feat-x"
            wt.mkdir(parents=True)

            meta = classify_worktree_dir(wt, root, git_runner=spy_runner)
            # All required dict keys present.
            self.assertEqual(set(meta.keys()),
                             {"state", "worktree_listed", "branch_merged_into_main",
                              "is_fresh", "branch_tip", "branch_name"})
            self.assertEqual(meta["state"], "live")
            self.assertTrue(meta["worktree_listed"])
            self.assertFalse(meta["branch_merged_into_main"])
            self.assertFalse(meta["is_fresh"])
            self.assertEqual(meta["branch_tip"], "")
            self.assertEqual(meta["branch_name"], "")

    def test_cost_by_worktree_panel_renders_state_column(self):
        from io import StringIO
        import contextlib
        from token_efficiency_analyzer import main
        from collections import Counter
        now = datetime.now(timezone.utc)

        def _s(sid, worktree, state, model="claude-sonnet-5", tokens=100):
            return {
                "session_id": sid, "source": "claude-code", "repo": "test-repo",
                "branch": "main", "worktree": worktree, "worktree_state": state,
                "model": model,
                "first_ts": now, "last_ts": now,
                "input_tokens": tokens, "output_tokens": 10,
                "cache_write_tokens": 0, "cache_read_tokens": 50,
                "ephemeral_5m": 0, "ephemeral_1h": 0,
                "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
                "log_path": "/tmp/fake.jsonl",
            }

        # four worktrees with four different states
        sessions = [
            _s("main-s",  "(main)",                  "main"),
            _s("live-s",  "feat-still-alive",        "live",  tokens=200),
            _s("gone-s",  "prune-baseline",          "gone",  tokens=50),
            _s("merge-s", "feature-and-adapt-skills","merged",tokens=80),
            _s("fresh-s", "fix/fresh-worktree-state","fresh", tokens=10),
        ]
        from token_efficiency_analyzer import score_session
        scored = [(s, score_session(s)) for s in sessions]
        empty_warns: list[list] = [[] for _ in scored]

        html = render_dashboard(
            repo="test-repo", days=30, sessions=sessions, scored=scored,
            warnings_per_session=empty_warns,
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
        )

        # Worktree panel must have a State header (scoped to the worktree panel).
        panel = html.split("Cost by Worktree", 1)[1].split("Cost by Model", 1)[0]
        self.assertIn(">State<", panel)
        # Each state label must render at least once.
        for state_label in ("live", "merged", "gone", "main", "fresh"):
            self.assertIn(f">{state_label}<", panel)

    def test_sessions_split_into_active_and_inactive_tables(self):
        from collections import Counter
        from datetime import datetime, timezone
        from token_efficiency_analyzer import score_session
        now = datetime.now(timezone.utc)

        def _s(sid, worktree, state):
            return {
                "session_id": sid, "source": "claude-code", "repo": "test-repo",
                "branch": "main", "worktree": worktree, "worktree_state": state,
                "model": "claude-sonnet-5",
                "first_ts": now, "last_ts": now,
                "input_tokens": 100, "output_tokens": 10,
                "cache_write_tokens": 0, "cache_read_tokens": 50,
                "ephemeral_5m": 0, "ephemeral_1h": 0,
                "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
                "log_path": "/tmp/fake.jsonl",
            }

        stale_sessions = [
            _s("stale-1", "prune-baseline",   "gone"),     # inactive
            _s("stale-2", "merged-feat",      "merged"),   # inactive
            _s("fresh-1", "feat-still-alive", "live"),     # active
            _s("main-1",  "(main)",           "main"),     # active
            _s("fresh-wt","fix/fresh-wt",     "fresh"),    # active
        ]
        scored = [(s, score_session(s)) for s in stale_sessions]
        empty_warns: list[list] = [[] for _ in scored]

        html = render_dashboard(
            repo="test-repo", days=30, sessions=stale_sessions, scored=scored,
            warnings_per_session=empty_warns,
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
        )

        active_block = html.split('<div class="section-title">Active Sessions', 1)[1] \
                            .split('<div class="section-title">Inactive Sessions', 1)[0]
        inactive_block = html.split('<div class="section-title">Inactive Sessions', 1)[1] \
                             .split("</table>", 1)[0]

        for sid in ("fresh-1", "main-1", "fresh-wt"):
            self.assertIn(sid, active_block, f"{sid} should be in Active Sessions")
            self.assertNotIn(sid, inactive_block, f"{sid} should not be in Inactive Sessions")
        for sid in ("stale-1", "stale-2"):
            self.assertIn(sid, inactive_block, f"{sid} should be in Inactive Sessions")
            self.assertNotIn(sid, active_block, f"{sid} should not be in Active Sessions")

        # Overview tile reflects the same split.
        self.assertIn(">3<", html.split("Active Sessions</div>", 1)[1][:60])

    def test_stdout_summary_includes_stale_cost(self):
        """Drive main() with --no-include-worktree-logs to skip real git, then
        patch classify_all_worktrees to return a fake map. Assert the [ok]
        line carries stale_cost=."""
        from io import StringIO
        import contextlib
        from unittest import mock
        from token_efficiency_analyzer import main

        with tempfile.TemporaryDirectory(prefix="wt-stdout-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            # one main-checkout session, cost ~$0.001
            rec = {
                "type": "assistant", "sessionId": "s-stale",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {
                    "role": "assistant", "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "output_tokens": 10,
                              "cache_read_input_tokens": 50,
                              "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                 "ephemeral_1h_input_tokens": 0}},
                },
            }
            (d / "s-stale.jsonl").write_text(json.dumps(rec) + "\n")

            fake_meta = {
                "(main)": {"state": "main", "branch_merged_into_main": False,
                           "branch_tip": "", "worktree_listed": True},
                "stale-x": {"state": "merged", "branch_merged_into_main": True,
                            "branch_tip": "abc1234", "worktree_listed": True},
            }
            buf = StringIO()
            with mock.patch("token_efficiency_analyzer.classify_all_worktrees",
                            return_value=fake_meta):
                with contextlib.redirect_stdout(buf):
                    rc = main([
                        "--repo", "dev-harness-kit",
                        "--days", "30",
                        "--logs-dir", str(td_path / "logs"),
                        "--no-include-worktree-logs",
                        "--out", str(td_path / "dash.html"),
                    ])
            self.assertEqual(rc, 0)
            stdout = buf.getvalue()
            self.assertIn("[ok]", stdout)
            self.assertIn("stale_cost=", stdout)
            # Main session only → stale_cost equals $0.00
            self.assertIn("stale_cost=$0.00", stdout)

    def test_json_payload_includes_worktrees_and_stale_cost(self):
        """Drive main() with --json + mock classify_all_worktrees. Assert the
        JSON payload carries ``worktrees``, ``stale_cost_usd``, ``stale_pct``."""
        from io import StringIO
        import contextlib
        from unittest import mock
        from token_efficiency_analyzer import main

        with tempfile.TemporaryDirectory(prefix="wt-json-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            rec = {
                "type": "assistant", "sessionId": "s-j",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": "2026-07-09T10:00:00.000Z",
                "message": {
                    "role": "assistant", "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "output_tokens": 10,
                              "cache_read_input_tokens": 50,
                              "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                 "ephemeral_1h_input_tokens": 0}},
                },
            }
            (d / "s-j.jsonl").write_text(json.dumps(rec) + "\n")

            fake_meta = {
                "(main)": {"state": "main", "branch_merged_into_main": False,
                           "branch_tip": "", "worktree_listed": True},
            }
            buf = StringIO()
            with mock.patch("token_efficiency_analyzer.classify_all_worktrees",
                            return_value=fake_meta):
                with contextlib.redirect_stdout(buf):
                    rc = main([
                        "--repo", "dev-harness-kit",
                        "--days", "30",
                        "--logs-dir", str(td_path / "logs"),
                        "--no-include-worktree-logs",
                        "--json",
                    ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertIn("stale_cost_usd", data)
            self.assertIn("stale_pct", data)
            self.assertIn("worktrees", data)
            self.assertIsInstance(data["worktrees"], list)
            self.assertEqual(data["stale_cost_usd"], 0.0)
            # worktree row for "(main)" present with state="main"
            main_row = next(w for w in data["worktrees"] if w["name"] == "(main)")
            self.assertEqual(main_row["state"], "main")


class TestRoiActionsSpecificity(unittest.TestCase):
    """ROI Actions rows must name the offending session + concrete evidence,
    not just a generic per-code paragraph (the ROI complaint this feature
    fixes: "정확하게 해야할 일을 정확하게 집어주지 않는다")."""

    def test_roi_item_names_session_and_evidence(self):
        s = _make_session(
            session_id="roi-target-sid",
            input_tokens=10_000, cache_read_tokens=1_000,  # low cache hit -> CACHE_HIT_LOW
            model="claude-sonnet-5",
        )
        sc = score_session(s)
        warns = evaluate_warnings(s, sc, reclaim_cache_miss=3.50,
                                  reclaim_dup_read=0.0, reclaim_downgrade=0.0)
        html = render_dashboard(
            repo="test-repo", days=30, sessions=[s], scored=[(s, sc)],
            warnings_per_session=[warns],
            estimated={"cache_miss": 3.50, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 3.50},
        )
        roi_block = html.split('<ol class="roi">', 1)[1].split("</ol>", 1)[0]
        self.assertIn("roi-targ", roi_block)     # short session id (first 8 chars)
        self.assertIn("cache hit", roi_block)    # concrete evidence, not boilerplate
        self.assertIn("$3.50", roi_block)

    def test_roi_empty_when_no_savings(self):
        s = _make_session(session_id="clean-sid", input_tokens=100, cache_read_tokens=900)
        sc = score_session(s)
        html = render_dashboard(
            repo="test-repo", days=30, sessions=[s], scored=[(s, sc)],
            warnings_per_session=[[]],
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
        )
        roi_block = html.split('<ol class="roi">', 1)[1].split("</ol>", 1)[0]
        self.assertIn("No reclaimable savings", roi_block)


if __name__ == "__main__":
    unittest.main()


class TestCodexSessionParsing(unittest.TestCase):
    """_aggregate_session parses codex event_msg/turn_context/response_item shapes.

    The actual codex schema carries:
      - ``turn_context.payload.model`` → per-turn model
      - ``event_msg`` with ``payload.type == "token_count"`` and
        ``payload.info.total_token_usage.{input_tokens,cached_input_tokens,
        output_tokens,reasoning_output_tokens}`` → cumulative per-session totals
      - ``response_item`` with ``payload.type == "function_call"`` →
        ``payload.name`` populates ``tool_counts``
      - ``event_msg`` with ``payload.type == "user_message"`` →
        ``payload.message`` populates ``user_texts``

    These tests pin each branch independently.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="analyzer-codex-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _write_codex(self, sid: str, records: list[dict], *,
                     subdir: str = "main") -> Path:
        d = self.tmpdir / "logs" / "codex" / subdir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return p

    def test_turn_context_populates_model(self) -> None:
        p = self._write_codex("sid-turn", [
            {"type": "turn_context", "payload": {"model": "gpt-5.6-luna"},
             "timestamp": "2026-07-15T10:00:00.000Z",
             "cwd": "/tmp/fix-repo"},
        ])
        s = aggregate_session(p)
        self.assertEqual(s["model"], "gpt-5.6-luna")

    def test_latest_turn_model_is_shown_when_session_switches_models(self) -> None:
        p = self._write_codex("sid-model-switch", [
            {"type": "turn_context", "payload": {"model": "MiniMax-M3"},
             "timestamp": "2026-07-15T10:00:00.000Z",
             "cwd": "/tmp/fix-repo"},
            {"type": "turn_context", "payload": {"model": "MiniMax-M3"},
             "timestamp": "2026-07-15T10:01:00.000Z",
             "cwd": "/tmp/fix-repo"},
            {"type": "turn_context", "payload": {"model": "gpt-5.6-luna"},
             "timestamp": "2026-07-15T10:02:00.000Z",
             "cwd": "/tmp/fix-repo"},
        ])
        s = aggregate_session(p)
        self.assertEqual(s["model"], "gpt-5.6-luna")

    def test_missing_model_metadata_stays_empty(self) -> None:
        p = self._write_codex("sid-no-model", [
            {"type": "session_meta", "payload": {
                "session_id": "sid-no-model",
                "cwd": "/tmp/fix-repo",
            }},
        ])
        s = aggregate_session(p)
        self.assertEqual(s["model"], "")

    def test_event_msg_token_count_overwrites_with_final_snapshot(self) -> None:
        """codex emits incremental ``token_count`` snapshots; we keep the final."""
        p = self._write_codex("sid-tokens", [
            {"type": "event_msg", "payload": {"type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 20,
                    "output_tokens": 10, "reasoning_output_tokens": 2,
                    "total_tokens": 132}}}},
            {"type": "event_msg", "payload": {"type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 500, "cached_input_tokens": 400,
                    "output_tokens": 50, "reasoning_output_tokens": 20,
                    "total_tokens": 570}}}},
        ])
        s = aggregate_session(p)
        # Final snapshot wins (cumulative figure, not a delta).
        self.assertEqual(s["input_tokens"], 500 - 400)        # non-cached input
        self.assertEqual(s["cache_read_tokens"], 400)
        self.assertEqual(s["output_tokens"], 50 + 20)          # codex bills reasoning as output
        self.assertEqual(s["cache_write_tokens"], 0)

    def test_event_msg_user_message_lands_in_user_texts(self) -> None:
        p = self._write_codex("sid-umsg", [
            {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "hello codex"}},
            {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "  trim me  "}},
            {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": ""}},  # dropped
        ])
        s = aggregate_session(p)
        self.assertEqual(s["user_texts"], ["hello codex", "trim me"])

    def test_response_item_function_call_populates_tool_counts(self) -> None:
        p = self._write_codex("sid-tool", [
            {"type": "response_item", "payload": {"type": "function_call",
                "name": "Read", "input": {"file_path": "/x/y"}}},
            {"type": "response_item", "payload": {"type": "custom_tool_call",
                "name": "shell_cmd"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
                "output": "huge blob — ignored"}},  # explicitly dropped
        ])
        s = aggregate_session(p)
        self.assertEqual(s["tool_counts"]["Read"], 1)
        self.assertEqual(s["tool_counts"]["shell_cmd"], 1)
        # Outputs are not tool calls, so total stays 2.
        self.assertEqual(sum(s["tool_counts"].values()), 2)
        # Read populates read_files too.
        self.assertIn("/x/y", s["read_files"])


class TestZeroTurnSessionSuppressed(unittest.TestCase):
    """Zero-turn sessions (user only, no assistant reply) must NOT render in
    either Active or Inactive Sessions table rows. Transcript Index may still
    list them for traceability.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="analyzer-zt-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _write_claude(self, sid: str, *, n_user_turns: int) -> Path:
        d = self.tmpdir / "logs" / "claude-code" / "main"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for i in range(n_user_turns):
                fh.write(json.dumps({
                    "type": "user",
                    "sessionId": sid,
                    "cwd": "/Users/sanghee/dev/dev-harness-kit",
                    "timestamp": f"2026-07-09T10:0{i}:00.000Z",
                    "gitBranch": "main",
                    "message": {"role": "user",
                                "content": f"user turn {i}"},
                }) + "\n")
        return p

    def test_zero_turn_session_dropped_from_active_panel(self) -> None:
        # Use the public main() entrypoint.
        import io
        from contextlib import redirect_stdout, redirect_stderr

        sid_active = "a2914f3e-cf19-4421-a1fb-7f9b81cc92e8"  # active worktree
        sid_inactive = "b72bba75-3406-4841-8fdb-b3f86985bae7"  # stale
        self._write_claude(sid_active, n_user_turns=3)
        self._write_claude(sid_inactive, n_user_turns=1)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            old_argv = sys.argv
            sys.argv = ["analyzer", "--repo", "dev-harness-kit",
                        "--days", "30", "--logs-dir", str(self.tmpdir / "logs"),
                        "--out", str(self.tmpdir / "dash.html")]
            try:
                rc = main()
            finally:
                sys.argv = old_argv
            self.assertEqual(rc, 0)
            html = (self.tmpdir / "dash.html").read_text()

        # Both session ids must be excluded from BOTH Active AND Inactive table
        # bodies (they appear in section titles and the Transcript Index, but
        # those are not score-coded rows).
        active_body = html.split("Active Sessions", 1)[1].split("Inactive Sessions", 1)[0]
        inactive_body = html.split("Inactive Sessions", 1)[1].split("Transcript Index", 1)[0]
        for sid in (sid_active, sid_inactive):
            self.assertNotIn(sid[:8], active_body,
                f"{sid[:8]} should not appear in Active Sessions table")
            self.assertNotIn(sid[:8], inactive_body,
                f"{sid[:8]} should not appear in Inactive Sessions table")

        # ... but the Transcript Index (which iterates `scored`, not the
        # filtered pairs) DOES list their worktree rows. The worktree stub
        # is "(main)" (no .worktrees/ ancestor), so we only assert the html
        # renders at all without error.

    def test_is_zero_turn_helper_unit(self) -> None:
        from token_efficiency_analyzer import _is_zero_turn_session
        self.assertTrue(_is_zero_turn_session({
            "input_tokens": 0, "output_tokens": 0,
            "tool_counts": Counter(),
        }))
        self.assertFalse(_is_zero_turn_session({
            "input_tokens": 100, "output_tokens": 0,
            "tool_counts": Counter(),
        }))
        self.assertFalse(_is_zero_turn_session({
            "input_tokens": 0, "output_tokens": 0,
            "tool_counts": Counter({"Read": 1}),
        }))


class TestCwdHarvest(unittest.TestCase):
    """codex rollouts carry cwd inside ``payload.cwd`` (session_meta + turn_context)
    — NOT at the record top level. The aggregator must harvest from both shapes
    so the repo filter resolves against the user's project, not the filename.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="analyzer-cwd-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _write(self, sid: str, records: list[dict]) -> Path:
        d = self.tmpdir / "logs" / "codex" / "main"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return p

    def test_repo_from_payload_cwd(self) -> None:
        p = self._write("sid-cwd", [
            {"type": "session_meta", "payload": {
                "session_id": "sid-cwd",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
            }},
        ])
        s = aggregate_session(p)
        self.assertEqual(s["repo"], "dev-harness-kit")

    def test_filter_sessions_keeps_payload_cwd(self) -> None:
        """End-to-end: codex rollout with cwd only on payload survives
        filter_sessions(repo='dev-harness-kit', days=30)."""
        from token_efficiency_analyzer import filter_sessions
        p = self._write("sid-payload-cwd", [
            {"type": "session_meta", "payload": {
                "session_id": "sid-payload-cwd",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "timestamp": "2026-07-15T10:00:00Z",
            }},
            # A model so the session has signal.
            {"type": "turn_context", "payload": {"model": "gpt-5.6-luna",
                "cwd": "/Users/sanghee/dev/dev-harness-kit"},
                "timestamp": "2026-07-15T10:00:01Z"},
        ])
        s = aggregate_session(p)
        self.assertEqual(s["repo"], "dev-harness-kit")
        kept = filter_sessions([s], "dev-harness-kit", 30)
        self.assertEqual(len(kept), 1)
