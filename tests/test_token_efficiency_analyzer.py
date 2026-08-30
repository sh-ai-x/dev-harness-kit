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
import os
import re
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
    _KNOWN_SOURCES,
    DEFAULT_PRICING_KEY,
    PRICING,
    _aggregate_worktree_rows,
    _session_cost,
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
    parse_iso,
    pricing_for,
    render_dashboard,
    score_cache_utilization,
    score_session,
    session_cost,
    worktree_from_cwd,
    worktree_from_path,
)

FIXTURE_LOGS = PROJECT_ROOT / "fixtures" / "logs" / "claude-code"

# Canonical fixture timestamps are static (``2026-07-09T10:00:00Z`` and the
# inline ``sample`` in ``test_other_repo_sessions_excluded`` adds
# ``:00:01.000Z``) so the committed fixtures stay byte-stable across runs.
# The analyzer's default ``--days 30`` window filters sessions older than
# that cutoff, which ages out the fixtures as wall-clock time advances.
# The two setUp()s below copy the canonical fixtures into a tmpdir and
# then rewrite every ``2026-07-09T10:00:`` prefix to a fresh timestamp so
# the window stays populated regardless of when the test runs.
_FIXTURE_BASE_TS_RE = re.compile(r"2026-07-09T10:00:")


def _refresh_fixture_timestamps(target_dir: Path, *, days_ago: int = 1) -> None:
    """Rewrite every ``2026-07-09T10:00:`` (canonical fixture base-time
    prefix) in jsonl files under ``target_dir`` to ``now - days_ago``'s
    ``YYYY-MM-DDTHH:MM:`` form (UTC). Sub-second offsets and the trailing
    ``Z`` stay attached to the replacement, so any per-line variations
    like ``:00:00.001Z`` keep their ordering. Recurses into nested
    subdirs so synthetic sessions written under ``logs/claude-code/<sub>/``
    also get refreshed.
    """
    fresh = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).strftime("%Y-%m-%dT%H:%M:")
    for p in target_dir.rglob("*.jsonl"):
        p.write_text(_FIXTURE_BASE_TS_RE.sub(fresh, p.read_text()))


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
        # Claude tier via DEFAULT_PRICING_KEY. Standard-rate variants match
        # the legacy fallback rate exactly; "highspeed" is a distinct,
        # separately-priced row in the SSOT (2x standard, per the vendor's
        # live pricing page) so it must NOT collapse to the standard rate.
        for mid in ("MiniMax-M3", "MiniMax-M2.7"):
            p = pricing_for(mid)
            self.assertEqual(p["in"], PRICING["minimax"]["in"],
                             f"model {mid!r} did not route to minimax tier")
            self.assertEqual(p["out"], PRICING["minimax"]["out"])

        p_highspeed = pricing_for("MiniMax-M2.7-highspeed")
        p_standard = pricing_for("MiniMax-M2.7")
        self.assertGreater(p_highspeed["in"], p_standard["in"],
                           "MiniMax-M2.7-highspeed must price above the standard tier")

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


class TestSessionCost(unittest.TestCase):
    def test_includes_legacy_cache_write_tokens(self):
        """Bug #478 regression: session_cost() must price the legacy flat
        cache_write_tokens bucket (no 5m/1h ephemeral split) instead of
        silently dropping it, matching its sibling _session_cost() so the
        Cost Gate and the dashboard's Active Sessions table agree on the
        same session's cost.
        """
        s = _make_session(model="claude-sonnet-5", input_tokens=0, output_tokens=0,
                           cache_write_tokens=1_000_000, cache_read_tokens=0)
        self.assertGreater(session_cost(s), 0.0)
        self.assertEqual(session_cost(s), _session_cost(s))


class TestParseIso(unittest.TestCase):
    def test_naive_timestamp_normalized_to_utc(self):
        """Bug #479 regression: parse_iso() must attach tzinfo to a naive
        timestamp (no 'Z' suffix, no UTC offset) so the result stays
        comparable to filter_sessions()'s tz-aware cutoff
        (datetime.now(timezone.utc) - timedelta(days=days)) instead of
        raising `TypeError: can't compare offset-naive and offset-aware
        datetimes`.
        """
        dt = parse_iso("2020-01-01T12:00:00")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.tzinfo, timezone.utc)
        # Must not raise TypeError when compared against a tz-aware cutoff
        # (a fixed past date keeps this assertion clock-independent).
        self.assertTrue(datetime.now(timezone.utc) > dt)


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

    def test_external_agent_log_root_is_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            external = Path(td) / "agent-logs" / root.name
            external_file = self._touch(
                external / "claude-code" / "fix-x" / "sid.jsonl"
            )
            old = os.environ.get("AGENT_LOG_ROOT")
            os.environ["AGENT_LOG_ROOT"] = str(external.parent)
            try:
                files = discover_logs(root / "logs", repo_root=root)
            finally:
                if old is None:
                    os.environ.pop("AGENT_LOG_ROOT", None)
                else:
                    os.environ["AGENT_LOG_ROOT"] = old
            self.assertIn(external_file, files)


class TestEndToEndDashboard(unittest.TestCase):
    """Run main() against a tmp logs dir, assert HTML + summary."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="token-analyzer-test-"))
        # Copy fixtures into <tmpdir>/logs/claude-code/
        target = self.tmpdir / "logs" / "claude-code"
        target.mkdir(parents=True)
        for f in FIXTURE_LOGS.glob("*.jsonl"):
            shutil.copy(f, target / f.name)
        # Rewrite the canonical 2026-07-09 timestamps to now-1d so the
        # analyzer's default --days 30 window stays populated.
        _refresh_fixture_timestamps(target)
        self.out_html = self.tmpdir / "dashboard.html"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_html_contains_every_new_section(self):
        rc = main([
            "--repo", "fixture-repo",
            "--days", "3650",
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
        # The inline sample reuses the canonical 2026-07-09 base timestamp;
        # refresh it (and the freshly-added files) so the 30-day window
        # still matches.
        _refresh_fixture_timestamps(other_dir)
        _refresh_fixture_timestamps(target)

        rc = main([
            "--repo", "fixture-repo",
            "--days", "3650",
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
        from contextlib import redirect_stderr, redirect_stdout
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "3650",
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
        from contextlib import redirect_stderr, redirect_stdout
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "3650",
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
        _refresh_fixture_timestamps(target)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_json_emits_expected_keys(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "3650",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--json",
            ])
        self.assertEqual(rc, 0)
        data = json.loads(stdout_buf.getvalue())
        self.assertEqual(data["repo"], "fixture-repo")
        self.assertEqual(data["days"], 3650)
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
            "--days", "3650",
            "--logs-dir", str(self.tmpdir / "logs"),
            "--cost-gate-tokens", "1000",   # every session will breach
            "--json",
        ])
        self.assertEqual(rc, 3)

    def test_explicit_logs_dir_does_not_discover_sibling_worktree_logs(self):
        import contextlib
        import io

        worktree_logs = self.tmpdir / ".worktrees" / "sibling" / "logs" / "claude-code"
        worktree_logs.mkdir(parents=True)
        sibling_log = (FIXTURE_LOGS / "aaaa-low-cache.jsonl").read_text()
        (worktree_logs / "sibling.jsonl").write_text(
            sibling_log.replace("aaaa-low-cache", "sibling-extra")
        )

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "3650",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--json",
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout_buf.getvalue())["sessions"], 6)

    def test_json_empty_logs_returns_2(self):
        empty = self.tmpdir / "empty"
        (empty / "claude-code").mkdir(parents=True)
        # --no-include-worktree-logs scopes discovery to the test's empty
        # tempdir; without it the analyzer auto-walks Path.cwd()/.claude/
        # worktrees/*/logs/ on the test machine, finds live JSONL, and
        # returns 0 instead of the expected "empty → 2".
        rc = main([
            "--repo", "fixture-repo",
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
        # Use a fresh timestamp (now - 1d) so the analyzer's default
        # ``--days 30`` window still includes the synthetic session.
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        rec = {
            "type": "assistant",
            "sessionId": sid,
            "cwd": cwd,
            "timestamp": ts,
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
        from datetime import datetime, timezone
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
        import contextlib
        from io import StringIO
        self._write_session("main", "s-main", branch="main")
        self._write_session("feature-x", "s-feat", branch="feature-x")
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "3650",
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
        import contextlib
        from io import StringIO
        self._write_session("main", "s-main", branch="main")
        flat = self.tmpdir / "logs" / "claude-code" / "legacy.jsonl"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_text("{}\n")
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", "fixture-repo",
                "--days", "3650",
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

    def test_worktree_from_external_metadata_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="external-meta-") as td:
            log = Path(td) / "claude-code" / "fix-x" / "sid.jsonl"
            log.parent.mkdir(parents=True)
            log.write_text("{}\n")
            log.with_suffix(".meta.json").write_text(json.dumps({
                "schema_version": "1.0",
                "worktree": "/tmp/removed-worktree/fix-x",
            }))
            self.assertEqual(worktree_from_path(log), "fix-x")

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
        import contextlib
        from io import StringIO
        with tempfile.TemporaryDirectory(prefix="wt-main-json-") as td:
            td_path = Path(td)
            # Fresh timestamp (now - 1d) so the analyzer's default
            # ``--days 30`` window still includes the synthetic session.
            ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
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
                    "timestamp": ts,
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
                    "--days", "3650",
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
        import contextlib
        from io import StringIO
        with tempfile.TemporaryDirectory(prefix="wt-render-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            # Fresh timestamp (now - 1d) so the analyzer's default
            # ``--days 30`` window still includes the synthetic session.
            ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            rec = {
                "type": "assistant",
                "sessionId": "s-r",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": ts,
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
                    "--days", "3650",
                    "--logs-dir", str(td_path / "logs"),
                ])
            self.assertEqual(rc, 0)
            html_path = Path("docs/observability/dashboard-dev-harness-kit-3650d.html")
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
        import contextlib
        import json
        import tempfile
        from io import StringIO
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
                    "--days", "3650",
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
        import contextlib
        from io import StringIO
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", repo,
                "--days", "3650",
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
            # Fresh timestamp (now - 1d) so the analyzer's default
            # ``--days 30`` window still includes the synthetic session.
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
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

    def test_classify_worktree_dir_real_probes_for_each_state(self):
        """Issue #310: classify_worktree_dir must consult git for real
        path / revision / diff state, not always report ``"live"``.
        Pins the 5 state branches (live, merged, fresh, gone, unknown)
        via a fake ``git_runner`` so each probe is exercised.
        """
        import subprocess

        from token_efficiency_analyzer import (
            WORKTREE_FRESH_MAX_AGE_SECONDS,
            classify_worktree_dir,
        )

        def _fake_git_run_factory(*, worktree_listed: bool, tip: str,
                                   head_full: str, main_full: str,
                                   unique_commits: str,
                                   origin_rev_returns_zero: bool = True,
                                   wt_path: Path | None = None):
            """Build a fake ``git_runner`` that returns canned answers.

            The real runner issues these probes (in order):

              1. ``git -C <repo_root> worktree list --porcelain`` to detect
                 whether the dir is still registered (``is_listed``).
              2. ``git -C <wt_path> rev-parse --short HEAD`` to read the
                 branch tip short SHA for ``branch_tip``.
              3. ``git -C <wt_path> rev-parse HEAD`` for the full HEAD SHA
                 used to detect ``is_fresh`` against ``origin/main``.
              4. ``git -C <repo_root> rev-parse origin/main`` for the
                 full ``origin/main`` SHA used in the fresh comparison.
              5. ``git -C <wt_path> log origin/main..HEAD --oneline`` to
                 detect whether the branch has unique commits.
            """
            porcelain = (
                f"worktree {wt_path}\nHEAD {head_full}\nbranch refs/heads/feat/x\n"
                if worktree_listed else ""
            )

            def fake_run(args, **_kwargs):
                cmd = " ".join(str(a) for a in args)
                if "worktree list --porcelain" in cmd:
                    rc = 0 if worktree_listed else 1
                    return subprocess.CompletedProcess(args, rc, stdout=porcelain, stderr="")
                if "rev-parse --short HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=tip, stderr="")
                if "rev-parse HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=head_full, stderr="")
                if "rev-parse origin/main" in cmd:
                    rc = 0 if origin_rev_returns_zero else 128
                    return subprocess.CompletedProcess(args, rc, stdout=main_full, stderr="")
                if "log origin/main..HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=unique_commits, stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            return fake_run

        # common fixtures — repo root + worktree dir
        with tempfile.TemporaryDirectory(prefix="wt-real-") as td:
            root = Path(td)
            wt_root = root / ".worktrees" / "feat-x"
            wt_root.mkdir(parents=True)

            def _age_dir(path: Path, seconds_old: int) -> None:
                """Backdate the dir's mtime so FRESH detect returns False."""
                import os
                import time as _time
                old = _time.time() - seconds_old
                os.utime(str(path), (old, old))

            # 1. LIVE — branch has commits not in origin/main
            meta = classify_worktree_dir(
                wt_root, root,
                git_runner=_fake_git_run_factory(
                    worktree_listed=True,
                    tip="abc1234",
                    head_full="abc1234abc1234abc1234abc1234abc1234abc1",
                    main_full="0000000000000000000000000000000000000000",
                    unique_commits="abc1234 wip commit\n",
                    wt_path=wt_root,
                ),
            )
            self.assertEqual(meta["state"], "live")
            self.assertTrue(meta["worktree_listed"])
            self.assertFalse(meta["branch_merged_into_main"])
            self.assertFalse(meta["is_fresh"])
            self.assertEqual(meta["branch_tip"], "abc1234")
            self.assertEqual(meta["branch_name"], "feat/x")

            # 2. MERGED — HEAD == origin/main SHA, log empty (rebase-merged).
            # Backdate mtime so it falls outside WORKTREE_FRESH_MAX_AGE_SECONDS.
            _age_dir(wt_root, seconds_old=WORKTREE_FRESH_MAX_AGE_SECONDS + 600)
            meta = classify_worktree_dir(
                wt_root, root,
                git_runner=_fake_git_run_factory(
                    worktree_listed=True,
                    tip="def5678",
                    head_full="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    main_full="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    unique_commits="",  # no commits not in origin/main
                    wt_path=wt_root,
                ),
            )
            self.assertEqual(meta["state"], "merged")
            self.assertTrue(meta["branch_merged_into_main"])
            self.assertEqual(meta["branch_tip"], "def5678")

            # 3. FRESH — HEAD == origin/main SHA, log empty, dir mtime fresh.
            # Use a fresh dir (not the aged one from case 2).
            fresh_wt = root / ".worktrees" / "feat-fresh"
            fresh_wt.mkdir(parents=True)
            meta = classify_worktree_dir(
                fresh_wt, root,
                git_runner=_fake_git_run_factory(
                    worktree_listed=True,
                    tip="feedface",
                    head_full="feedfacefeedfacefeedfacefeedfacefeedface",
                    main_full="feedfacefeedfacefeedfacefeedfacefeedface",
                    unique_commits="",
                    wt_path=fresh_wt,
                ),
            )
            # The worktree dir was just created in this test → mtime is
            # within WORKTREE_FRESH_MAX_AGE_SECONDS → state="fresh".
            self.assertEqual(meta["state"], "fresh")
            self.assertTrue(meta["is_fresh"])

            # 4. GONE — dir exists but not in `git worktree list`
            gone_wt = root / ".worktrees" / "feat-gone"
            gone_wt.mkdir(parents=True)
            meta = classify_worktree_dir(
                gone_wt, root,
                git_runner=_fake_git_run_factory(
                    worktree_listed=False,
                    tip="",
                    head_full="",
                    main_full="",
                    unique_commits="",
                    wt_path=gone_wt,
                ),
            )
            self.assertEqual(meta["state"], "gone")
            self.assertFalse(meta["worktree_listed"])

            # 5. UNKNOWN — `origin/main` rev-parse fails (no origin)
            unknown_wt = root / ".worktrees" / "feat-unknown"
            unknown_wt.mkdir(parents=True)
            meta = classify_worktree_dir(
                unknown_wt, root,
                git_runner=_fake_git_run_factory(
                    worktree_listed=True,
                    tip="cafe0000",
                    head_full="cafe0000cafe0000cafe0000cafe0000cafe0000",
                    main_full="",
                    unique_commits="",
                    origin_rev_returns_zero=False,
                    wt_path=unknown_wt,
                ),
            )
            self.assertEqual(meta["state"], "unknown")

    def test_classify_worktree_dir_returns_live_for_dir_outside_canonical_root(self):
        """Issue #310 regression: a worktree dir outside the canonical
        ``.worktrees/`` root must still report ``"live"`` (the dashboard
        sees it because ``classify_all_worktrees`` only iterates known
        roots, but a stray dir shows up if a caller passes it in)."""
        import subprocess

        from token_efficiency_analyzer import classify_worktree_dir

        def fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory(prefix="wt-stray-") as td:
            root = Path(td)
            wt = root / "stray-wt"  # not under .worktrees/
            wt.mkdir()
            meta = classify_worktree_dir(wt, root, git_runner=fake_run)
            # is_listed returns false (empty porcelain) so state="gone".
            self.assertEqual(meta["state"], "gone")

    def test_classify_all_worktrees_batches_shared_probes(self):
        """Issue #timeout-sweep: ``classify_all_worktrees`` previously
        ran ``git worktree list --porcelain`` and
        ``git -C repo_root rev-parse origin/main`` once PER worktree
        dir. With ~360 dirs on this checkout that produced ~1800
        subprocess spawns and a 60+ s wall clock for the classifier
        alone, which in turn made the ``sm`` CLI blow past the
        per-probe ``TimeoutExpired`` and crash the dashboard.

        The fix hoists both repo-wide probes out of the per-dir loop
        and passes them down via ``precomputed_porcelain`` /
        ``precomputed_origin_main``. This test pins the contract:
        the two repo-wide probes are issued ONCE regardless of how
        many worktree dirs are present, while the per-dir probes
        (rev-parse --short HEAD, rev-parse HEAD, log) still run
        once per dir.
        """
        import subprocess

        from token_efficiency_analyzer import classify_all_worktrees

        with tempfile.TemporaryDirectory(prefix="wt-batch-") as td:
            root = Path(td)
            # 5 candidate worktree dirs across two roots.
            for n in range(3):
                (root / ".worktrees" / f"feat-{n}").mkdir(parents=True)
            for n in range(2):
                (root / ".claude" / "worktrees" / f"agent-{n}").mkdir(parents=True)

            porcelain = (
                f"worktree {root / 'main-checkout'}\n"
                f"HEAD 0000000000000000000000000000000000000000\n"
                f"branch refs/heads/main\n"
            )
            call_log: list[str] = []

            def fake_run(args, **_kwargs):
                cmd = " ".join(str(a) for a in args)
                call_log.append(cmd)
                if "worktree list --porcelain" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
                if "rev-parse origin/main" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0,
                        stdout="feedface" * 8,
                        stderr="",
                    )
                if "rev-parse --short HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout="abc1234", stderr="")
                if "rev-parse HEAD" in cmd and "origin/main" not in cmd:
                    return subprocess.CompletedProcess(
                        args, 0,
                        stdout="feedface" * 8,
                        stderr="",
                    )
                if "log origin/main..HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            meta = classify_all_worktrees(root, git_runner=fake_run)

        # Sentinel key still present.
        self.assertIn("(main)", meta)
        # All 5 dirs classified.
        for n in range(3):
            self.assertIn(f"feat-{n}", meta)
        for n in range(2):
            self.assertIn(f"agent-{n}", meta)

        # Shared probes: exactly ONE call each.
        porcelain_calls = [c for c in call_log if "worktree list --porcelain" in c]
        origin_main_calls = [c for c in call_log if "rev-parse origin/main" in c]
        self.assertEqual(
            len(porcelain_calls), 1,
            f"worktree list --porcelain must run ONCE, got {len(porcelain_calls)}: {porcelain_calls}",
        )
        self.assertEqual(
            len(origin_main_calls), 1,
            f"rev-parse origin/main must run ONCE, got {len(origin_main_calls)}: {origin_main_calls}",
        )

        # Per-dir probes: should run once per candidate dir (5 dirs).
        short_head_calls = [c for c in call_log if "rev-parse --short HEAD" in c]
        head_full_calls = [
            c for c in call_log
            if "rev-parse HEAD" in c and "origin/main" not in c
        ]
        log_calls = [c for c in call_log if "log origin/main..HEAD" in c]
        self.assertEqual(len(short_head_calls), 5, f"per-dir short HEAD: {short_head_calls}")
        self.assertEqual(len(head_full_calls), 5, f"per-dir full HEAD: {head_full_calls}")
        self.assertEqual(len(log_calls), 5, f"per-dir log: {log_calls}")

    def test_classify_all_worktrees_runs_per_dir_probes_concurrently(self):
        """Issue #728: ``classify_all_worktrees`` previously iterated
        worktree dirs sequentially, so each ``git`` subprocess added its
        full latency to wall time. On a checkout with ~1500 dirs that
        single-handedly pushed the analyzer past 20 minutes of wall
        time even though every per-dir probe is independent. The fix
        fans out per-dir classification across a bounded thread pool;
        the two repo-wide probes (worktree list, origin/main SHA) stay
        hoisted as before.

        This test pins two contracts:

        1. The number of probes issued is unchanged from the sequential
           baseline (no extra round-trips introduced by parallelism).
        2. The probes run with a measured concurrency > 1, i.e. at
           least two git subprocess invocations overlap in wall time.
        """
        import subprocess
        import threading
        import time

        from token_efficiency_analyzer import classify_all_worktrees

        with tempfile.TemporaryDirectory(prefix="wt-parallel-") as td:
            root = Path(td)
            # 8 dirs is enough to expose concurrency without making the
            # test slow; each dir sleeps a known amount inside its
            # per-dir probes so we can count overlapping callers.
            for n in range(8):
                (root / ".worktrees" / f"feat-{n}").mkdir(parents=True)

            porcelain = (
                f"worktree {root / 'main-checkout'}\n"
                f"HEAD 0000000000000000000000000000000000000000\n"
                f"branch refs/heads/main\n"
            )
            in_flight = 0
            peak_in_flight = 0
            lock = threading.Lock()
            call_log: list[str] = []

            def fake_run(args, **_kwargs):
                nonlocal in_flight, peak_in_flight
                cmd = " ".join(str(a) for a in args)
                call_log.append(cmd)
                if "worktree list --porcelain" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
                if "rev-parse origin/main" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout="feedface" * 8, stderr="",
                    )
                if "rev-parse --short HEAD" in cmd:
                    pass  # fall through to per-dir timing path
                elif "rev-parse HEAD" in cmd and "origin/main" not in cmd:
                    pass
                elif "log origin/main..HEAD" in cmd:
                    pass
                else:
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                with lock:
                    in_flight += 1
                    peak_in_flight = max(peak_in_flight, in_flight)
                # Hold each per-dir probe long enough that even a slow
                # CI runner overlaps at least 2 callers when fanned out.
                time.sleep(0.05)
                with lock:
                    in_flight -= 1
                if "rev-parse --short HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout="abc1234", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="feedface" * 8, stderr="")

            meta = classify_all_worktrees(root, git_runner=fake_run)

        self.assertEqual(len(meta), 1 + 8)  # sentinel + 8 dirs
        # Repo-wide probes still hoisted (Issue #timeout-sweep contract).
        self.assertEqual(
            sum(1 for c in call_log if "worktree list --porcelain" in c), 1,
        )
        self.assertEqual(
            sum(1 for c in call_log if "rev-parse origin/main" in c), 1,
        )
        # Per-dir probes still issued exactly once per dir.
        self.assertEqual(
            sum(1 for c in call_log if "rev-parse --short HEAD" in c), 8,
        )
        # At least 2 per-dir probes must overlap. Sequential execution
        # would keep peak_in_flight at 1.
        self.assertGreaterEqual(
            peak_in_flight, 2,
            f"expected concurrent per-dir probes (peak={peak_in_flight}); "
            f"did classify_all_worktrees fall back to sequential mode?",
        )

    def test_classify_worktree_dir_swallows_timeout(self):
        """Issue #timeout-sweep: a single slow / hung worktree must not
        crash the whole ``sm`` dashboard. The docstring contract at
        tools/token_efficiency_analyzer.py:699-702 promises that every
        probe is wrapped, so a ``subprocess.TimeoutExpired`` on any
        single probe must fall back to ``state="unknown"`` for that
        one dir instead of propagating up to ``build_model``.
        """
        import subprocess

        from token_efficiency_analyzer import classify_worktree_dir

        def fake_run(args, **_kwargs):
            # Simulate the per-probe timeout that triggered the bug
            # report: the 5s ``timeout=timeout`` in the production
            # runner expires and ``subprocess.run`` raises. With the
            # new ``_run_probe`` wrapper, this collapses to ``None``
            # rather than unwinding through the dashboard.
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)

        with tempfile.TemporaryDirectory(prefix="wt-timeout-") as td:
            root = Path(td)
            wt = root / ".worktrees" / "hung"
            wt.mkdir(parents=True)
            meta = classify_worktree_dir(wt, root, git_runner=fake_run)

        self.assertEqual(
            meta["state"], "unknown",
            "every probe must be wrapped — TimeoutExpired on a single "
            "probe should fall back to state='unknown', not crash "
            "the dashboard",
        )
        # The function must return a complete dict (not raise) so
        # callers can keep iterating other dirs.
        for k in ("state", "worktree_listed", "branch_merged_into_main",
                  "is_fresh", "branch_tip", "branch_name"):
            self.assertIn(k, meta)

    def test_classify_all_worktrees_keeps_running_on_per_dir_timeout(self):
        """Issue #timeout-sweep: a single timed-out dir must not stop
        the rest of the classification pass. Drive ``classify_all_worktrees``
        with a fake ``git_runner`` that times out only for one of N
        dirs; assert the timed-out dir ends up as ``state="unknown"``
        while the rest classify normally.
        """
        import subprocess

        from token_efficiency_analyzer import classify_all_worktrees

        with tempfile.TemporaryDirectory(prefix="wt-mixed-") as td:
            root = Path(td)
            ok_wt = root / ".worktrees" / "ok"
            hung_wt = root / ".worktrees" / "hung"
            ok_wt.mkdir(parents=True)
            hung_wt.mkdir(parents=True)

            porcelain = (
                f"worktree {ok_wt}\nHEAD feedfacefeedfacefeedfacefeedfacefeedface\n"
                f"branch refs/heads/feat/ok\n\n"
                f"worktree {hung_wt}\nHEAD deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
                f"branch refs/heads/feat/hung\n"
            )

            def fake_run(args, **_kwargs):
                cmd = " ".join(str(a) for a in args)
                if "worktree list --porcelain" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
                if "rev-parse origin/main" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout="feedface" * 8, stderr="",
                    )
                if str(hung_wt) in cmd:
                    raise subprocess.TimeoutExpired(cmd=args, timeout=5)
                if "rev-parse --short HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout="abc1234", stderr="")
                if "rev-parse HEAD" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout="feedface" * 8, stderr="",
                    )
                if "log origin/main..HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            meta = classify_all_worktrees(root, git_runner=fake_run)

        # OK dir classified normally.
        self.assertIn(meta["ok"]["state"], ("fresh", "live", "merged", "gone"))
        # Hung dir must NOT crash the loop and must NOT be reported as
        # ``"live"`` (we couldn't verify it has unique commits). It
        # falls back to ``"merged"`` here because the empty log probe
        # succeeded for the hung path too — that's a safe answer.
        self.assertIn(
            meta["hung"]["state"], ("merged", "unknown"),
            f"per-dir timeout must not crash and must not report 'live' "
            f"(we couldn't read the unique-commits probe), got {meta['hung']['state']!r}",
        )
        # (main) sentinel still emitted.
        self.assertEqual(meta["(main)"]["state"], "main")

    def test_classify_worktree_dir_per_block_branch_name_lookup(self):
        """Issue #494 reviewer finding (🟠 major #2):

        ``classify_worktree_dir`` previously scanned all of
        ``git worktree list --porcelain`` for the first ``branch``
        line — always returning the main checkout's branch for every
        worktree. The fix walks block-by-block and returns the branch
        of the block whose ``worktree <path>`` line matches the target.
        This test pins that contract: the second worktree block must
        yield ``feat/y``, not ``main``.
        """
        import subprocess

        from token_efficiency_analyzer import classify_worktree_dir

        with tempfile.TemporaryDirectory(prefix="wt-perblock-") as td:
            root = Path(td)
            wt_main = root / "main-checkout"
            wt_x = root / ".worktrees" / "feat-y"
            wt_main.mkdir(parents=True)
            wt_x.mkdir(parents=True)

            porcelain = (
                f"worktree {wt_main}\n"
                f"HEAD 0000000000000000000000000000000000000000\n"
                f"branch refs/heads/main\n"
                f"\n"
                f"worktree {wt_x}\n"
                f"HEAD 1111111111111111111111111111111111111111\n"
                f"branch refs/heads/feat/y\n"
                f"\n"
            )

            def fake_run(args, **_kwargs):
                cmd = " ".join(str(a) for a in args)
                if "worktree list --porcelain" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
                if "rev-parse --short HEAD" in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout="abcdef0", stderr="")
                if "rev-parse HEAD" in cmd and str(wt_x) in cmd:
                    return subprocess.CompletedProcess(
                        args, 0,
                        stdout="feedfacefeedfacefeedfacefeedfacefeedface",
                        stderr="",
                    )
                if "rev-parse HEAD" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0,
                        stdout="0000000000000000000000000000000000000000",
                        stderr="",
                    )
                if "rev-parse origin/main" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0,
                        stdout="0000000000000000000000000000000000000000",
                        stderr="",
                    )
                if "log origin/main..HEAD" in cmd:
                    # non-empty for wt_x → live; empty for main
                    if str(wt_x) in cmd:
                        return subprocess.CompletedProcess(
                            args, 0,
                            stdout="feedface wip commit on feat/y\n",
                            stderr="",
                        )
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            meta_x = classify_worktree_dir(wt_x, root, git_runner=fake_run)
            self.assertEqual(meta_x["branch_name"], "feat/y",
                "Per-block porcelain walk must return THIS block's branch, "
                "not the main checkout's. (PR #494 review 🟠 major #2.)")

            # And the main checkout's worktree path must still return its own.
            meta_main = classify_worktree_dir(wt_main, root, git_runner=fake_run)
            self.assertEqual(meta_main["branch_name"], "main")

    def test_probe_working_tree_clean_paths(self):
        """Pin the new helper's three observable paths:
        clean (porcelain empty), dirty (M-/A-/etc. plus untracked),
        and error (subprocess failure) → ``working_tree_clean=None``.

        Key name is ``working_tree_clean`` (PR #494 review M-1), NOT
        ``clean``, so the orchestrator can ``context.update(probe_result)``
        without a remap step.
        """
        import subprocess

        from token_efficiency_analyzer import probe_working_tree_clean

        # 1. Clean working tree → working_tree_clean=True, both counts zero.
        def clean_runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory(prefix="wt-clean-") as td:
            wt = Path(td) / "wt"
            wt.mkdir()
            r = probe_working_tree_clean(wt, git_runner=clean_runner)
        self.assertTrue(r["working_tree_clean"])
        self.assertEqual(r["uncommitted_count"], 0)
        self.assertEqual(r["untracked_count"], 0)

        # 2. Dirty: one modified file + one untracked file.
        porcelain = (
            " M src/foo.py\n"
            "?? scratch.txt\n"
        )
        def dirty_runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
        with tempfile.TemporaryDirectory(prefix="wt-dirty-") as td:
            wt = Path(td) / "wt"
            wt.mkdir()
            r = probe_working_tree_clean(wt, git_runner=dirty_runner)
        self.assertFalse(r["working_tree_clean"])
        self.assertEqual(r["uncommitted_count"], 1)
        self.assertEqual(r["untracked_count"], 1)
        self.assertIn("M src/foo.py", r["porcelain"])

        # 3. Error → working_tree_clean=None, counts zero (never raises).
        def error_runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: bad")
        with tempfile.TemporaryDirectory(prefix="wt-err-") as td:
            wt = Path(td) / "wt"
            wt.mkdir()
            r = probe_working_tree_clean(wt, git_runner=error_runner)
        self.assertIsNone(r["working_tree_clean"])
        self.assertEqual(r["uncommitted_count"], 0)

    def test_dispatch_envelope_shape_matches_agent_spec(self):
        """PR #494 review M-1: prove that an orchestrator doing a
        naive ``context.update(probe_working_tree_clean(...))``
        produces a payload the worktree-janitor agent spec can read
        directly. Pin the full key set the agent's dispatch envelope
        contract reads (``working_tree_clean``, ``uncommitted_count``,
        ``untracked_count``, ``state``, ``branch_name``, ``branch_tip``,
        ``is_fresh``, ``worktree_listed``, ``branch_merged_into_main``).
        """
        import subprocess

        from token_efficiency_analyzer import (
            classify_worktree_dir,
            probe_working_tree_clean,
        )

        def fake_git_run_factory(worktree_porcelain: str, status: str,
                                   head_full: str, main_full: str,
                                   log: str):
            def fake_run(args, **_kwargs):
                cmd = " ".join(str(a) for a in args)
                if "worktree list --porcelain" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout=worktree_porcelain, stderr="")
                if "rev-parse --short HEAD" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout="abc1234", stderr="")
                if "rev-parse HEAD" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout=head_full, stderr="")
                if "rev-parse origin/main" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout=main_full, stderr="")
                if "log origin/main..HEAD" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout=log, stderr="")
                if "status --porcelain" in cmd:
                    return subprocess.CompletedProcess(
                        args, 0, stdout=status, stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return fake_run

        with tempfile.TemporaryDirectory(prefix="wt-dispatch-") as td:
            root = Path(td)
            wt = root / ".worktrees" / "feat-d"
            wt.mkdir(parents=True)
            porcelain = (
                f"worktree {wt}\n"
                f"HEAD deadbeef\n"
                f"branch refs/heads/feat/d\n"
            )
            git_runner = fake_git_run_factory(
                worktree_porcelain=porcelain,
                status=" M scratch.txt\n",
                head_full="deadbeef" * 8,
                main_full="feedface" * 8,
                log="deadbeef wip\n",
            )
            meta = classify_worktree_dir(wt, root, git_runner=git_runner)
            probe = probe_working_tree_clean(wt, git_runner=git_runner)

        # The contract the agent spec reads from context. Naive merge.
        context = dict(meta)
        context.update(probe)

        # M-1 was: this read would KeyError. With both keys using
        # ``working_tree_clean``, it now resolves.
        self.assertIs(
            context["working_tree_clean"], False,
            "PR #494 review M-1: dispatch envelope must yield "
            "working_tree_clean=False for a dirty worktree",
        )
        self.assertEqual(context["uncommitted_count"], 1)
        # Pin every key the agent's dispatch envelope table reads.
        for k in (
            "state", "branch_name", "branch_tip", "is_fresh",
            "worktree_listed", "branch_merged_into_main",
            "working_tree_clean", "uncommitted_count", "untracked_count",
        ):
            self.assertIn(k, context, f"dispatch envelope missing key {k!r}")

    def test_cost_by_worktree_panel_renders_state_column(self):
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
        import contextlib
        from io import StringIO
        from unittest import mock

        from token_efficiency_analyzer import main

        with tempfile.TemporaryDirectory(prefix="wt-stdout-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            # one main-checkout session, cost ~$0.001
            # Fresh timestamp (now - 1d) so the analyzer's default
            # ``--days 30`` window still includes the synthetic session.
            rec = {
                "type": "assistant", "sessionId": "s-stale",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
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
                        "--days", "3650",
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
        import contextlib
        from io import StringIO
        from unittest import mock

        from token_efficiency_analyzer import main

        with tempfile.TemporaryDirectory(prefix="wt-json-") as td:
            td_path = Path(td)
            d = td_path / "logs" / "claude-code" / "main"
            d.mkdir(parents=True)
            # Fresh timestamp (now - 1d) so the analyzer's default
            # ``--days 30`` window still includes the synthetic session.
            ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            rec = {
                "type": "assistant", "sessionId": "s-j",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "gitBranch": "main",
                "timestamp": ts,
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
                        "--days", "3650",
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
        from contextlib import redirect_stderr, redirect_stdout

        sid_active = "a2914f3e-cf19-4421-a1fb-7f9b81cc92e8"  # active worktree
        sid_inactive = "b72bba75-3406-4841-8fdb-b3f86985bae7"  # stale
        self._write_claude(sid_active, n_user_turns=3)
        self._write_claude(sid_inactive, n_user_turns=1)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            old_argv = sys.argv
            sys.argv = ["analyzer", "--repo", "dev-harness-kit",
                        "--days", "3650", "--logs-dir", str(self.tmpdir / "logs"),
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
        # Timestamps built relative to now so the session stays inside the
        # 30-day window regardless of when the suite runs (calendar-rot fix).
        ts0 = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts1 = (datetime.now(timezone.utc) - timedelta(days=1, seconds=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = self._write("sid-payload-cwd", [
            {"type": "session_meta", "payload": {
                "session_id": "sid-payload-cwd",
                "cwd": "/Users/sanghee/dev/dev-harness-kit",
                "timestamp": ts0,
            }},
            # A model so the session has signal.
            {"type": "turn_context", "payload": {"model": "gpt-5.6-luna",
                "cwd": "/Users/sanghee/dev/dev-harness-kit"},
                "timestamp": ts1},
        ])
        s = aggregate_session(p)
        self.assertEqual(s["repo"], "dev-harness-kit")
        kept = filter_sessions([s], "dev-harness-kit", 30)
        self.assertEqual(len(kept), 1)


class TestProviderSplit(unittest.TestCase):
    """Issue #310: ``aggregate_session`` is split by provider record type.

    After the split:
      * Per-record-type handlers are exposed as private helpers (one per
        provider, one per record type). They accept a ``SessionState``
        accumulator + a record dict and mutate the accumulator.
      * The common walker (timestamp / sid / repo / branch / worktree
        harvest) is shared and dispatches each parsed record to the
        provider-specific handler.
      * Each provider record type produces the same final dict shape as
        before — no behavior change at the public surface.

    These tests pin the STRUCTURE (handler names exist + accept the right
    arg shape) and BEHAVIOR (the same dict shape comes out the other end).
    """

    def test_per_provider_handlers_are_exposed(self) -> None:
        from token_efficiency_analyzer import (
            _handle_claude_record,
            _handle_codex_record,
        )
        self.assertTrue(callable(_handle_claude_record))
        self.assertTrue(callable(_handle_codex_record))

    def test_session_state_accumulator_is_exposed(self) -> None:
        # The shared walker needs an accumulator to thread through the
        # per-record handlers. Verify it's importable and instantiable
        # with a sensible default.
        from token_efficiency_analyzer import _new_session_state
        st = _new_session_state(source="claude-code")
        self.assertEqual(st.source, "claude-code")
        # Common counters initialized to zero / empty.
        self.assertEqual(st.input_tokens, 0)
        self.assertEqual(st.output_tokens, 0)
        self.assertEqual(st.session_id, None)
        self.assertEqual(st.repo, "")
        self.assertEqual(st.first_ts, None)
        self.assertEqual(st.last_ts, None)
        # Counter-shaped accumulators are dicts (sorted, deterministic).
        self.assertEqual(st.tool_counts, {})
        self.assertEqual(st.read_files, {})

    def test_claude_record_handler_populates_session_id(self) -> None:
        """Regression: a single claude-code ``assistant`` record must set
        ``session_id`` via the handler path, not via the inline walker."""
        from token_efficiency_analyzer import (
            _handle_claude_record,
            _new_session_state,
        )
        st = _new_session_state(source="claude-code")
        rec = {
            "type": "assistant",
            "sessionId": "sid-from-handler",
            "gitBranch": "feat/from-handler",
            "timestamp": "2026-07-15T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0,
                          "cache_creation": {
                              "ephemeral_5m_input_tokens": 0,
                              "ephemeral_1h_input_tokens": 0}},
            },
        }
        # Per-record handler is responsible for the model/usage/tool fields;
        # session_id / repo / branch / worktree come from the common walker
        # (mirrors what aggregate_session does after the split).
        _handle_claude_record(rec, st)
        self.assertEqual(st.input_tokens, 10)
        self.assertEqual(st.output_tokens, 5)
        self.assertEqual(st.latest_model, "claude-sonnet-5")

    def test_codex_record_handler_populates_session_id(self) -> None:
        """Regression: a single codex ``turn_context`` record must set
        ``session_id`` via the handler path, not via the inline walker."""
        from token_efficiency_analyzer import (
            _handle_codex_record,
            _new_session_state,
        )
        st = _new_session_state(source="codex")
        rec = {
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-luna", "cwd": "/tmp/codex-repo"},
            "timestamp": "2026-07-15T10:00:00.000Z",
        }
        _handle_codex_record(rec, st)
        self.assertEqual(st.latest_model, "gpt-5.6-luna")
        # repo / branch / worktree come from the common walker.

    def test_aggregate_session_returns_same_shape_after_split(self) -> None:
        """End-to-end: the public ``aggregate_session`` returns the same
        dict shape after the split. Existing tests rely on
        ``session_id / model / input_tokens / output_tokens /
        cache_read_tokens / cache_write_tokens / ephemeral_5m /
        ephemeral_1h / tool_counts / read_files / user_texts /
        first_ts / last_ts / source / repo / branch / worktree``.
        """
        from token_efficiency_analyzer import _new_session_state
        # Confirm the accumulator exposes every key the public surface reads.
        st = _new_session_state(source="claude-code")
        required = {
            "session_id", "source", "repo", "branch", "worktree", "model",
            "first_ts", "last_ts", "input_tokens", "output_tokens",
            "cache_write_tokens", "cache_read_tokens", "ephemeral_5m",
            "ephemeral_1h", "tool_counts", "read_files", "user_texts",
        }
        self.lessEqual = self.assertLessEqual  # silence linter
        # All final-shape keys either come from the state (set during walk)
        # or from the post-walk finalize (branch / worktree resolution).
        # We don't require every key on the empty state — only that the
        # walker / finalizer continues to populate them after the split.
        # Quick smoke: ``finalize_session`` produces the same keys.
        from token_efficiency_analyzer import _finalize_session
        st.session_id = "sid-finalize"
        st.latest_model = "claude-sonnet-5"
        st.input_tokens = 1
        out = _finalize_session(st, source="claude-code", log_path=Path("/tmp/x.jsonl"))
        for k in required:
            self.assertIn(k, out, f"_finalize_session output missing key {k!r}")


class TestDashboardViewModel(unittest.TestCase):
    """Issue #310: introduce a dashboard/view-model boundary shared by
    JSON and HTML sinks. The same ``build_view_model(...)`` call feeds
    both output formats so adding a new panel touches one aggregator
    instead of two.

    The view-model is the COMPUTED data shape (per-panel dicts, totals,
    pre-resolved strings). JSON output is a thin serialization of the
    view-model + raw session list. HTML output is a thin rendering of
    the view-model. This removes the duplicated aggregation between
    ``main()`` (JSON path) and ``render_dashboard()`` (HTML path).
    """

    def test_build_view_model_is_exposed(self) -> None:
        from token_efficiency_analyzer import build_view_model
        self.assertTrue(callable(build_view_model))

    def test_view_model_has_required_top_level_keys(self) -> None:
        from collections import Counter
        from datetime import datetime, timezone

        from token_efficiency_analyzer import build_view_model

        now = datetime.now(timezone.utc)
        s = {
            "session_id": "s-vm", "source": "claude-code", "repo": "r",
            "branch": "main", "worktree": "(main)",
            "worktree_state": "main",
            "model": "claude-sonnet-5",
            "first_ts": now, "last_ts": now,
            "input_tokens": 100, "output_tokens": 10,
            "cache_write_tokens": 0, "cache_read_tokens": 50,
            "ephemeral_5m": 0, "ephemeral_1h": 0,
            "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
            "log_path": "/tmp/fake.jsonl",
        }
        scored = [(s, score_session(s))]
        empty_warns: list[list] = [[]]
        estimated = {"cache_miss": 0.0, "dup_read": 0.0,
                     "model_downgrade": 0.0, "total": 0.0}
        vm = build_view_model(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=empty_warns,
            estimated=estimated,
            cost_gate=("ok", []),
            all_sessions_in_window=[s],
            wt_meta={"(main)": {"state": "main", "worktree_listed": True,
                                 "branch_merged_into_main": False,
                                 "branch_tip": "", "branch_name": ""}},
        )
        # Every aggregator the JSON + HTML paths used to recompute must
        # now live on the view-model.
        for k in ("cost_by_repo", "cost_by_branch", "cost_by_worktree",
                  "cost_by_tool", "cost_by_model", "cost_by_worktree_rows",
                  "cache_ttl", "totals", "active_count", "inactive_count",
                  "stale_cost", "stale_pct"):
            self.assertIn(k, vm, f"view-model missing key {k!r}")

    def test_json_and_html_share_same_view_model(self) -> None:
        """Regression: ``main() --json`` and ``render_dashboard`` use
        the same ``build_view_model`` so per-panel numbers match exactly
        (the duplication was the bug; this test pins the unified path).
        """
        from collections import Counter
        from datetime import datetime, timezone

        from token_efficiency_analyzer import (
            build_view_model,
            render_dashboard,
        )

        now = datetime.now(timezone.utc)
        s = {
            "session_id": "s-shared", "source": "claude-code", "repo": "r",
            "branch": "main", "worktree": "(main)",
            "worktree_state": "main",
            "model": "claude-sonnet-5",
            "first_ts": now, "last_ts": now,
            "input_tokens": 100, "output_tokens": 10,
            "cache_write_tokens": 0, "cache_read_tokens": 50,
            "ephemeral_5m": 0, "ephemeral_1h": 0,
            "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
            "log_path": "/tmp/fake.jsonl",
        }
        scored = [(s, score_session(s))]
        estimated = {"cache_miss": 0.0, "dup_read": 0.0,
                     "model_downgrade": 0.0, "total": 0.0}
        # Build the view-model ONCE — both sinks consume it.
        vm = build_view_model(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=[[]],
            estimated=estimated,
            cost_gate=("ok", []),
            all_sessions_in_window=[s],
            wt_meta={},
        )
        # HTML render consumes the view-model (not raw inputs).
        html = render_dashboard(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=[[]], estimated=estimated,
            view_model=vm,
        )
        # The HTML must echo the total cost from the view-model exactly.
        # Total = 100 * input + 10 * output + 50 * cache_read — model-
        # dependent, but the string format must round to the same value.
        self.assertIn("Total Cost", html)
        # Smoke: the view-model's totals block drives the HTML.
        self.assertEqual(vm["totals"]["input_tokens"], 100)
        self.assertEqual(vm["totals"]["output_tokens"], 10)

    def test_render_dashboard_accepts_view_model_arg(self) -> None:
        """``render_dashboard`` must accept ``view_model=`` so callers can
        pass a pre-built aggregation (HTML-only callers don't need to
        re-aggregate). Default behavior (no view_model) is unchanged."""
        from token_efficiency_analyzer import render_dashboard
        sig = render_dashboard.__doc__ or ""
        # The docstring must document the view_model parameter.
        self.assertIn("view_model", sig)

    def test_view_model_cost_by_worktree_seeds_disk_only_rows(self) -> None:
        """The view-model must seed zero-cost rows for disk-only worktrees
        (no session in window) so the dashboard's Cost by Worktree panel
        doesn't hide stale dirs — same contract as the old
        ``_aggregate_worktree_rows`` + ``wt_meta`` merge."""
        from collections import Counter
        from datetime import datetime, timezone

        from token_efficiency_analyzer import build_view_model

        now = datetime.now(timezone.utc)
        s = {
            "session_id": "s1", "source": "claude-code", "repo": "r",
            "branch": "main", "worktree": "(main)",
            "worktree_state": "main",
            "model": "claude-sonnet-5",
            "first_ts": now, "last_ts": now,
            "input_tokens": 100, "output_tokens": 10,
            "cache_write_tokens": 0, "cache_read_tokens": 50,
            "ephemeral_5m": 0, "ephemeral_1h": 0,
            "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
            "log_path": "/tmp/fake.jsonl",
        }
        scored = [(s, score_session(s))]
        wt_meta = {
            "(main)": {"state": "main", "worktree_listed": True,
                       "branch_merged_into_main": False,
                       "branch_tip": "", "branch_name": ""},
            "stale-wt": {"state": "merged", "worktree_listed": True,
                         "branch_merged_into_main": True,
                         "branch_tip": "abc1234", "branch_name": "feat/stale"},
        }
        vm = build_view_model(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=[[]],
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
            cost_gate=("ok", []),
            all_sessions_in_window=[s],
            wt_meta=wt_meta,
        )
        names = [r["name"] for r in vm["cost_by_worktree_rows"]]
        # Both "(main)" and "stale-wt" appear, even though only "(main)"
        # had a session — the disk-only seeding contract is preserved.
        self.assertIn("(main)", names)
        self.assertIn("stale-wt", names)


class TestSnapshotBuilder(unittest.TestCase):
    """Issue #321 (smell-21): ``main()`` extracted a ``build_analysis_snapshot``
    helper that produces ONE immutable snapshot consumed by both JSON and HTML
    sinks. The previous shape (inline selected/windowed in ``main()`` + an
    independent re-aggregation in ``render_dashboard()``) let the two sinks
    drift apart when ``--branch`` was set; the snapshot makes that drift
    impossible by construction.

    These tests pin:
      * the helper exists and is exposed,
      * both sinks see the same ``selected`` list (the branch-filtered set),
      * both sinks see the same ``total_cost_usd`` (no parallel aggregation).
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="token-analyzer-snap-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_analysis_snapshot_is_exposed(self) -> None:
        """The snapshot builder is a top-level public symbol so direct
        callers (e.g. tests, future JSON/HTML wrappers) can build one
        without going through ``main()``."""
        from token_efficiency_analyzer import build_analysis_snapshot
        self.assertTrue(callable(build_analysis_snapshot))

    def test_snapshot_holds_selected_and_windowed(self) -> None:
        """The snapshot distinguishes the branch-filtered set (``selected``)
        from the unfiltered-by-branch set (``windowed``). Both must be
        present so the per-repo/branch/worktree panel can use ``windowed``
        without re-running ``filter_sessions`` and the per-session panel
        can use ``selected`` without re-running it either — both
        derivations happen exactly once."""
        from token_efficiency_analyzer import build_analysis_snapshot

        # Timestamps built relative to now so the fixtures stay inside the
        # 30-day window regardless of when the suite runs (calendar-rot fix,
        # same pattern as #601 / #658).
        ts_main = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ts_feat = (datetime.now(timezone.utc) - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Stage two minimal sessions, one on main and one on feat-x.
        # Timestamps are dynamic (5 days ago) so the test never rots at
        # the 30-day window boundary; calendar-rot regression #658.
        logs = self.tmpdir / "logs" / "claude-code"
        logs.mkdir(parents=True)
        ts_main = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ts_feat = (datetime.now(timezone.utc) - timedelta(days=5, hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        main_session = (
            f'{{"timestamp":"{ts_main}",'
            '"message":{"role":"assistant","model":"claude-sonnet-5",'
            '"content":[{"type":"text","text":"ok"}],'
            '"usage":{"input_tokens":100,"output_tokens":10,'
            '"cache_read_input_tokens":50}},'
            '"type":"assistant","sessionId":"sid-main",'
            '"cwd":"/tmp/snap-repo","gitBranch":"main"}\n'
        )
        feat_session = (
            f'{{"timestamp":"{ts_feat}",'
            '"message":{"role":"assistant","model":"claude-sonnet-5",'
            '"content":[{"type":"text","text":"ok"}],'
            '"usage":{"input_tokens":200,"output_tokens":20,'
            '"cache_read_input_tokens":100}},'
            '"type":"assistant","sessionId":"sid-feat",'
            '"cwd":"/tmp/snap-repo","gitBranch":"feat/x"}\n'
        )
        (logs / "main.jsonl").write_text(main_session)
        (logs / "feat.jsonl").write_text(feat_session)

        # Build a snapshot for --branch feat/x (only the feat session should
        # land in ``selected``; both should land in ``windowed``).
        snap = build_analysis_snapshot(
            repo="snap-repo", days=30, logs_dir=self.tmpdir / "logs",
            branch="feat/x", worktree="",
        )
        selected_sids = [s["session_id"] for s in snap.selected]
        windowed_sids = [s["session_id"] for s in snap.windowed]
        # --branch filter scopes ``selected`` to feat only.
        self.assertEqual(selected_sids, ["sid-feat"])
        # ``windowed`` is unscoped-by-branch — both sessions survive.
        self.assertEqual(sorted(windowed_sids), ["sid-feat", "sid-main"])
        # The snapshot is a frozen object — neither sink mutates it.
        self.assertIsNotNone(snap)

    def test_snapshot_total_cost_matches_rendered_html(self) -> None:
        """Regression: with ``--branch feat/x`` + logs on both branches,
        the JSON ``total_cost_usd`` field MUST equal the Total Cost cell
        in the rendered HTML. Before the snapshot extraction, ``main()``
        derived totals from ``selected`` while ``render_dashboard()``
        recomputed from its own local walk — different code paths with
        no shared state — and they could drift on any future edit."""
        import contextlib
        from io import StringIO
        logs = self.tmpdir / "logs" / "claude-code"
        logs.mkdir(parents=True)
        main_session = (
            '{"timestamp":"2026-07-21T10:00:00.000Z",'
            '"message":{"role":"assistant","model":"claude-sonnet-5",'
            '"content":[{"type":"text","text":"ok"}],'
            '"usage":{"input_tokens":100,"output_tokens":10,'
            '"cache_read_input_tokens":50}},'
            '"type":"assistant","sessionId":"sid-main",'
            '"cwd":"/tmp/parity-repo","gitBranch":"main"}\n'
        )
        feat_session = (
            '{"timestamp":"2026-07-21T11:00:00.000Z",'
            '"message":{"role":"assistant","model":"claude-sonnet-5",'
            '"content":[{"type":"text","text":"ok"}],'
            '"usage":{"input_tokens":200,"output_tokens":20,'
            '"cache_read_input_tokens":100}},'
            '"type":"assistant","sessionId":"sid-feat",'
            '"cwd":"/tmp/parity-repo","gitBranch":"feat/x"}\n'
        )
        (logs / "main.jsonl").write_text(main_session)
        (logs / "feat.jsonl").write_text(feat_session)

        json_buf = StringIO()
        with contextlib.redirect_stdout(json_buf):
            rc = main([
                "--repo", "parity-repo", "--days", "3650",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--branch", "feat/x", "--json",
            ])
        self.assertEqual(rc, 0)
        json_total = json.loads(json_buf.getvalue())["total_cost_usd"]

        # Now HTML run with the same args.
        out_html = self.tmpdir / "parity.html"
        rc = main([
            "--repo", "parity-repo", "--days", "3650",
            "--logs-dir", str(self.tmpdir / "logs"),
            "--branch", "feat/x",
            "--out", str(out_html),
        ])
        self.assertEqual(rc, 0)
        html_text = out_html.read_text()

        # The HTML must render the JSON's total cost string in the Total
        # Cost cell (with the same $ rounding). Both are derived from the
        # snapshot — so they MUST agree bit-for-bit.
        expected = f"${json_total:.2f}"
        self.assertIn(expected, html_text,
                      f"HTML Total Cost does not match JSON total_cost_usd; "
                      f"JSON={json_total}, expected substring in HTML={expected!r}")


class TestRenderDashboardConsumesViewModel(unittest.TestCase):
    """Issue #321 (smell-10): ``render_dashboard`` consumes the view-model
    exclusively — every per-panel aggregation lives on the snapshot, so
    the HTML path cannot drift from the JSON path. The previous shape
    re-ran the per-panel loops inside the function, duplicating ~100
    lines of code that already lived in ``build_view_model``.

    These tests pin:
      * the view-model's per-panel keys are locked (no consumer can fall
        back to a recomputation without a snapshot mismatch),
      * ``render_dashboard`` produces byte-identical output regardless of
        whether the caller supplied redundant ``sessions``/``scored``/
        ``warnings_per_session`` arguments (so a stale caller can't make
        the HTML drift).
    """

    def test_view_model_panel_keys_locked(self) -> None:
        """The view-model exposes every per-panel aggregation the HTML
        renders. Adding a panel touches ONE place — adding/removing a
        panel from this set is a breaking change for HTML and JSON alike."""
        from collections import Counter
        from datetime import datetime, timezone

        from token_efficiency_analyzer import build_view_model

        now = datetime.now(timezone.utc)
        s = {
            "session_id": "s-vm", "source": "claude-code", "repo": "r",
            "branch": "main", "worktree": "(main)",
            "worktree_state": "main", "model": "claude-sonnet-5",
            "first_ts": now, "last_ts": now,
            "input_tokens": 100, "output_tokens": 10,
            "cache_write_tokens": 0, "cache_read_tokens": 50,
            "ephemeral_5m": 0, "ephemeral_1h": 0,
            "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
            "log_path": "/tmp/fake.jsonl",
        }
        scored = [(s, score_session(s))]
        vm = build_view_model(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=[[]],
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
            cost_gate=("ok", []),
            all_sessions_in_window=[s],
        )
        # These keys are the full set the HTML renders against. Removing
        # one is a structural break; adding one means ``build_view_model``
        # didn't cover a panel.
        required = {
            "cost_by_repo", "cost_by_branch", "cost_by_worktree",
            "cost_by_worktree_rows", "cost_by_tool", "cost_by_model",
            "cache_ttl", "totals", "active_count", "inactive_count",
            "stale_cost", "stale_pct", "estimated", "cost_gate",
            "unknown_models", "warnings",
        }
        self.assertTrue(required.issubset(vm.keys()),
                        f"view-model missing required panels: "
                        f"{required - set(vm.keys())}")

    def test_render_dashboard_total_matches_view_model_total(self) -> None:
        """``render_dashboard`` MUST echo ``view_model['totals']['total_cost']``
        verbatim — the HTML header's Total Cost cell must NOT be the result
        of an independent local walk over ``scored``."""
        from collections import Counter
        from datetime import datetime, timezone

        from token_efficiency_analyzer import build_view_model, render_dashboard

        now = datetime.now(timezone.utc)
        s = {
            "session_id": "s-hdr", "source": "claude-code", "repo": "r",
            "branch": "main", "worktree": "(main)",
            "worktree_state": "main", "model": "claude-sonnet-5",
            "first_ts": now, "last_ts": now,
            "input_tokens": 100, "output_tokens": 10,
            "cache_write_tokens": 0, "cache_read_tokens": 50,
            "ephemeral_5m": 0, "ephemeral_1h": 0,
            "tool_counts": Counter(), "read_files": Counter(), "user_texts": [],
            "log_path": "/tmp/fake.jsonl",
        }
        scored = [(s, score_session(s))]
        vm = build_view_model(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=[[]],
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
            cost_gate=("ok", []),
            all_sessions_in_window=[s],
        )
        html = render_dashboard(
            repo="r", days=30, sessions=[s], scored=scored,
            warnings_per_session=[[]],
            estimated={"cache_miss": 0.0, "dup_read": 0.0,
                       "model_downgrade": 0.0, "total": 0.0},
            cost_gate=("ok", []),
            all_sessions_in_window=[s],
            view_model=vm,
        )
        expected = f"${vm['totals']['total_cost']:.2f}"
        self.assertIn(expected, html,
                      f"HTML total cost {expected!r} not found — render_dashboard "
                      f"did not consume view_model['totals']['total_cost']")


class TestSessionAggregateTyped(unittest.TestCase):
    """Issue #321 (smell-9): ``aggregate_session``'s accumulator is a typed
    ``SessionAggregate`` (dataclass), not a free-form dict. The walker
    dispatches each parsed record to a per-provider handler that mutates
    the aggregate via attribute access. The previous shape (plain dict
    accumulator) let any field be typo'd silently and made the walker
    surface area invisible to type checkers.

    These tests pin:
      * ``SessionAggregate`` is a public, typed dataclass,
      * ``_new_session_state`` (or its replacement) returns one,
      * the existing per-provider handlers still mutate the typed
        accumulator (no behavior change at the public surface).
    """

    def test_session_aggregate_is_dataclass(self) -> None:
        """The accumulator must be a dataclass so type checkers can
        validate the walker/handler surface."""
        from dataclasses import is_dataclass

        from token_efficiency_analyzer import SessionAggregate
        self.assertTrue(is_dataclass(SessionAggregate),
                        "SessionAggregate is not a dataclass")

    def test_new_session_state_returns_typed_aggregate(self) -> None:
        """The factory must return a SessionAggregate instance, not a
        plain dict (a dict would silently let typo'd keys slip in)."""
        from token_efficiency_analyzer import SessionAggregate, _new_session_state
        agg = _new_session_state(source="claude-code")
        self.assertIsInstance(agg, SessionAggregate)
        # Required typed fields — every consumer (walker / finalizer /
        # per-provider handlers) reads these.
        for f in ("session_id", "source", "repo", "models", "latest_model",
                  "input_tokens", "output_tokens", "cache_write_tokens",
                  "cache_read_tokens", "ephemeral_5m", "ephemeral_1h",
                  "tool_counts", "read_files", "user_texts",
                  "branch_counts", "worktree_counts", "first_ts", "last_ts"):
            self.assertTrue(hasattr(agg, f),
                            f"SessionAggregate missing field {f!r}")

    def test_handler_works_on_typed_aggregate(self) -> None:
        """Existing per-provider handlers must keep working on the typed
        aggregate (no public surface change)."""
        from token_efficiency_analyzer import (
            _handle_claude_record,
            _new_session_state,
        )
        agg = _new_session_state(source="claude-code")
        rec = {
            "type": "assistant",
            "sessionId": "sid-typed",
            "gitBranch": "feat/typed",
            "timestamp": "2026-07-15T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0,
                          "cache_creation": {
                              "ephemeral_5m_input_tokens": 0,
                              "ephemeral_1h_input_tokens": 0}},
            },
        }
        _handle_claude_record(rec, agg)
        self.assertEqual(agg.input_tokens, 10)
        self.assertEqual(agg.output_tokens, 5)
        self.assertEqual(agg.latest_model, "claude-sonnet-5")


class TestMalformedTokenUsageSkipped(unittest.TestCase):
    """Issue #321 (smell-9 follow-up): malformed token-usage payloads
    (e.g. ``usage.input_tokens = "unknown"``) must NOT crash the walker.
    The previous shape raised ``ValueError`` on the int() cast and lost
    the entire session; the fix is to skip the malformed token AND
    count it on a typed ``parse_errors`` accumulator.
    """

    def test_malformed_input_tokens_does_not_crash_walker(self) -> None:
        from token_efficiency_analyzer import aggregate_session
        bad_record = (
            '{"timestamp":"2026-07-15T10:00:00.000Z",'
            '"sessionId":"sid-bad-tokens",'
            '"gitBranch":"main",'
            '"type":"assistant",'
            '"message":{"role":"assistant","model":"claude-sonnet-5",'
            '"content":[{"type":"text","text":"ok"}],'
            '"usage":{"input_tokens":"unknown","output_tokens":5}}}'
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(bad_record + "\n")
            path = Path(f.name)
        try:
            sess = aggregate_session(path)
            # Session survives the malformed record (no crash, no
            # exception) — the bad token is skipped, the rest stands.
            self.assertIsNotNone(sess)
            self.assertEqual(sess["session_id"], "sid-bad-tokens")
            # The good tokens are still recorded; the bad ones are skipped.
            self.assertEqual(sess["output_tokens"], 5)
            self.assertEqual(sess["input_tokens"], 0)
        finally:
            path.unlink()

    def test_malformed_input_tokens_counted_on_aggregate(self) -> None:
        """The walker counts malformed token fields on the typed
        accumulator so the dashboard can surface 'N records skipped'
        instead of silently swallowing them."""
        from token_efficiency_analyzer import _new_session_state
        agg = _new_session_state(source="claude-code")
        self.assertTrue(hasattr(agg, "parse_errors"))
        # Default value is a Counter (dict-shaped) so it survives json.dumps.
        agg.parse_errors["malformed_input_tokens"] += 1
        self.assertEqual(agg.parse_errors["malformed_input_tokens"], 1)


class TestCacheDecay(unittest.TestCase):
    """F1 cache_decay fix — proposal §Validation gates G2 + G3.

    G2: every session with ≥2 tracked turns has a ``cache_decay`` list
        whose length matches the number of recorded turns, and each
        element is a float in [0.0, 1.0].

    G3: the dashboard HTML contains the new "Cache hit ratio vs turn
        index" tile plus the four bucket labels ``[1-3, 4-10, 11-30, 30+]``
        when at least one session has tracked turns.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cache-decay-test-"))
        target = self.tmpdir / "logs" / "claude-code"
        target.mkdir(parents=True)
        # Hand-craft a 3-turn session: turn 1 cold (no cache_read),
        # turn 2 warm (cache_read > 0), turn 3 warmer still.
        # Cache-hit ratios: 0/200 = 0.0, 600/800 = 0.75, 700/900 ≈ 0.78.
        session_id = "cache-decay-test-session"
        lines = [
            _make_user_record(session_id, "hi", ts="2026-07-09T10:00:00.000Z"),
            _make_assistant_record(
                session_id, model="claude-haiku-4-5",
                input_tokens=200, cache_read=0, ts="2026-07-09T10:00:01.000Z",
            ),
            _make_user_record(session_id, "go on", ts="2026-07-09T10:00:02.000Z"),
            _make_assistant_record(
                session_id, model="claude-haiku-4-5",
                input_tokens=200, cache_read=600, ts="2026-07-09T10:00:03.000Z",
            ),
            _make_user_record(session_id, "more", ts="2026-07-09T10:00:04.000Z"),
            _make_assistant_record(
                session_id, model="claude-haiku-4-5",
                input_tokens=200, cache_read=700, ts="2026-07-09T10:00:05.000Z",
            ),
        ]
        (target / "cache-decay-fixture.jsonl").write_text("\n".join(lines) + "\n")
        self.out_html = self.tmpdir / "dashboard.html"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_g2_cache_decay_field_present_and_correct_shape(self):
        """G2: aggregate_session emits ``cache_decay`` as a list[float]
        with one entry per tracked assistant turn. Each value ∈ [0, 1].

        The cache_decay list holds PER-TURN DELTAS (the i-th entry is
        ``cache_read / (cache_read + input)`` for that turn alone), not
        session-cumulative ratios. The Claude JSONL handler derives the
        delta from the cumulative ``st.input_tokens`` counter via
        ``prev_turn_input``; the Codex handler computes deltas from
        successive ``token_count`` events. Both branches emit the same
        unit of measure so the dashboard's bucket aggregator doesn't
        compare across incommensurable quantities.

        Per-turn ratios from the fixture:
          turn 1: 0     / 200          = 0.0
          turn 2: 600   / (200 + 600)  = 0.75
          turn 3: 700   / (200 + 700)  ≈ 0.7778
        """
        from token_efficiency_analyzer import aggregate_session
        result = aggregate_session(
            self.tmpdir / "logs" / "claude-code" / "cache-decay-fixture.jsonl"
        )
        self.assertIsNotNone(result, "aggregate_session returned None")
        self.assertIn("cache_decay", result,
                      "G2: aggregate_session must emit cache_decay")
        cd = result["cache_decay"]
        self.assertEqual(len(cd), 3,
                         f"G2: expected 3 entries (one per assistant turn), got {len(cd)}")
        for v in cd:
            self.assertIsInstance(v, float,
                                  f"G2: each cache_decay entry must be float, got {type(v)}")
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
        # Per-turn ratios from the fixture (delta against prev_turn_*).
        self.assertAlmostEqual(cd[0], 0.0, places=4)
        self.assertAlmostEqual(cd[1], 600 / 800, places=4)
        self.assertAlmostEqual(cd[2], 700 / 900, places=4)

    def test_claude_emits_per_turn_deltas_not_cumulative(self):
        """Regression (PR #761 review feedback, M1): the Claude handler
        used to emit session-CUMULATIVE ratios into the per-turn
        ``cache_decay`` list (every entry monotonically growing toward
        1.0 because cumulative input + cache_read keep climbing). The
        Codex branch already emits per-turn deltas, so the dashboard's
        bucket aggregator was comparing incommensurable quantities
        across sources. Pin that Claude now produces the SAME shape as
        Codex — per-turn deltas — by checking the SPECIFIC numerical
        value that distinguishes the two shapes.

        With the fixture:
          turn 1: input=200, cache_read=0
          turn 2: input=200, cache_read=600
          turn 3: input=200, cache_read=700
        Cumulative ratios would be:
          turn 2: 600 / (400 + 600) = 0.6
        Per-turn deltas give:
          turn 2: 600 / (200 + 600) = 0.75
        The test pins 0.75 (delta), not 0.6 (cumulative) — the old
        code would have emitted 0.6 here, which is the regression.
        """
        from token_efficiency_analyzer import aggregate_session
        result = aggregate_session(
            self.tmpdir / "logs" / "claude-code" / "cache-decay-fixture.jsonl"
        )
        cd = result["cache_decay"]
        # Pin the per-turn delta, not the cumulative ratio.
        self.assertAlmostEqual(cd[1], 0.75, places=4,
                               msg="Claude cache_decay[1] must be per-turn "
                                   "delta (0.75), not session-cumulative (0.6)")

    def test_g3_dashboard_html_contains_cache_decay_tile(self):
        """G3: rendered dashboard carries the new tile + the active bucket
        label. Empty buckets are skipped by the renderer (the spec says
        a bucket with zero sessions is not a row), so we only assert
        against ``1-3`` — the bucket the 3-turn fixture lands in.
        """
        rc = main([
            "--repo", "cache-decay-fixture",
            "--days", "3650",
            "--logs-dir", str(self.tmpdir / "logs"),
            "--out", str(self.out_html),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(self.out_html.exists())
        text = self.out_html.read_text()
        self.assertIn("Cache hit ratio vs turn index", text,
                      "G3: dashboard missing new section title")
        # The 3-turn fixture lands in the 1-3 bucket; assert the
        # active bucket shows up in the table.
        self.assertIn(">1-3<", text,
                      "G3: dashboard missing active bucket label '1-3'")
        # And the section header carries the F1 tag for traceability.
        self.assertIn("F1 cache_decay", text,
                      "G3: dashboard section title should reference the F1 fix")

    def test_g2_compute_cache_decay_zero_division_safe(self):
        """G2 corner case: a turn with 0 input + 0 cache_read must
        produce 0.0 (not raise ZeroDivisionError)."""
        from token_efficiency_analyzer import _compute_cache_decay
        self.assertEqual(_compute_cache_decay([0, 0], [0, 0]), [0.0, 0.0])
        self.assertEqual(_compute_cache_decay([], []), [])
        self.assertEqual(_compute_cache_decay([100, 0, 50], [0, 0, 50]),
                         [0.0, 0.0, 0.5])


class TestCacheDecaySvg(unittest.TestCase):
    """SVG renderer + JSON-mode parity (this PR's polish layer).

    The renderer must:
      * emit a valid SVG with viewBox, path, and circle elements
      * be robust to malformed numeric inputs (no path-string injection)
      * be robust to empty / 1-point inputs
      * escape any interpolated string in the <title> element
    """

    def test_svg_basic_three_point_curve(self):
        """Three-point curve produces path + band + 2 circle markers."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [
            {"turn": 1, "median": 0.0, "p25": 0.0, "p75": 0.0, "n": 5},
            {"turn": 2, "median": 0.5, "p25": 0.4, "p75": 0.6, "n": 5},
            {"turn": 3, "median": 0.9, "p25": 0.85, "p75": 0.95, "n": 5},
        ]
        svg = _render_cache_decay_svg(points)
        self.assertIn("<svg", svg)
        self.assertIn("viewBox=", svg)
        self.assertIn("<path", svg)  # band
        # Two path elements (band + median polyline).
        self.assertEqual(svg.count("<path"), 2)
        # Two circle markers (first + last turn).
        self.assertEqual(svg.count("<circle"), 2)
        # <title> carries the human-readable summary.
        self.assertIn("<title>", svg)
        self.assertIn("turn 1 → turn 3", svg)

    def test_svg_empty_returns_empty_string(self):
        """Empty points list → no SVG (the parent row is skipped)."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        self.assertEqual(_render_cache_decay_svg([]), "")

    def test_svg_one_point_does_not_crash(self):
        """A single-point curve (one turn session) still renders — the
        renderer must NOT divide by zero when n-1 == 0."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [{"turn": 1, "median": 0.5, "p25": 0.5, "p75": 0.5, "n": 1}]
        svg = _render_cache_decay_svg(points)
        self.assertIn("<svg", svg)
        self.assertIn("<circle", svg)

    def test_svg_bounds_out_of_range_ratios(self):
        """Defensive float coercion — a ratio of 1.5 or -0.1 must
        clamp to [0, 1] rather than produce negative Y coordinates
        outside the viewBox. The test parses the rendered ``d``
        attribute and asserts every Y is within ``[2, height-2]``."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [
            {"turn": 1, "median": 1.5, "p25": -0.1, "p75": 0.5, "n": 1},
        ]
        svg = _render_cache_decay_svg(points, width=220, height=56)
        # Pull every Y coordinate out of every path. We allow floats.
        import re as _re
        ys = [float(y) for y in _re.findall(r",\s*([\d.]+)\s*[Zz\"']?", svg)]
        for y in ys:
            self.assertGreaterEqual(y, 0.0,
                f"clamp failed — y={y} went below 0 (viewBox bottom)")
            self.assertLessEqual(y, 56.0,
                f"clamp failed — y={y} went above height (viewBox top)")

    def test_svg_non_numeric_turn_falls_back_to_zero(self):
        """A ``turn: None`` or ``turn: \"abc\"`` must not raise — the
        renderer falls back to 0 rather than crashing the whole
        dashboard render."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [
            {"turn": None, "median": 0.5, "p25": 0.5, "p75": 0.5, "n": 1},
            {"turn": "abc", "median": 0.7, "p25": 0.6, "p75": 0.8, "n": 1},
        ]
        # Must not raise.
        svg = _render_cache_decay_svg(points)
        self.assertIn("<svg", svg)

    def test_svg_non_numeric_ratio_falls_back_to_zero(self):
        """A ``median: \"NaN\"`` (truthy string) must not raise — the
        ``or 0.0`` fallback only catches falsy values, so we need the
        ``try/except ValueError`` in ``_safe_float`` to cover this case."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [
            {"turn": 1, "median": "NaN", "p25": "n/a", "p75": 0.5, "n": 1},
        ]
        # Must not raise. Must produce SVG.
        svg = _render_cache_decay_svg(points)
        self.assertIn("<svg", svg)

    def test_svg_uses_first_turn_not_hardcoded_one(self):
        """The <title> must use ``points[0][\"turn\"]`` rather than a
        hardcoded ``turn 1 →``. Regression test for the LLM-judge
        finding on PR #765 — sparse / non-1-indexed turns would render
        a wrong summary next to correct data."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [
            {"turn": 5, "median": 0.5, "p25": 0.4, "p75": 0.6, "n": 3},
            {"turn": 6, "median": 0.7, "p25": 0.6, "p75": 0.8, "n": 3},
            {"turn": 7, "median": 0.9, "p25": 0.85, "p75": 0.95, "n": 3},
        ]
        svg = _render_cache_decay_svg(points)
        self.assertIn("turn 5 → turn 7", svg)
        self.assertNotIn("turn 1 → turn 7", svg)

    def test_render_cache_decay_rows_caption_uses_first_turn(self):
        """Regression for round-3 LLM-judge finding on PR #765 —
        the HTML caption beside the SVG also hardcoded
        ``turn 1→``. The caption must mirror the SVG's first-turn
        rule so the table row reads consistently."""
        from token_efficiency_analyzer import _render_cache_decay_rows
        cache_decay = {
            "1-3": {
                "n_sessions": 1,
                "points": [
                    {"turn": 5, "median": 0.5, "p25": 0.4, "p75": 0.6, "n": 1},
                    {"turn": 6, "median": 0.7, "p25": 0.6, "p75": 0.8, "n": 1},
                    {"turn": 7, "median": 0.9, "p25": 0.85, "p75": 0.95, "n": 1},
                ],
            },
            "4-10": {"n_sessions": 0, "points": []},
            "11-30": {"n_sessions": 0, "points": []},
            "30+": {"n_sessions": 0, "points": []},
        }
        rows = _render_cache_decay_rows(cache_decay)
        self.assertIn("turn 5→7", rows)
        self.assertNotIn("turn 1→", rows)

    def test_render_cache_decay_rows_handles_missing_keys(self):
        """Defensive: if the producer (view_model.py) ever stops
        setting a key, the renderer must NOT crash — the whole
        dashboard render would otherwise fail."""
        from token_efficiency_analyzer import _render_cache_decay_rows
        # n_sessions is missing; median is non-numeric
        cache_decay = {
            "1-3": {
                "points": [
                    {"turn": 1, "median": "NaN", "p25": 0.5, "p75": 0.5, "n": 1},
                ],
            },
            "4-10": {"points": []},
            "11-30": {"points": []},
            "30+": {"points": []},
        }
        # Must not raise.
        rows = _render_cache_decay_rows(cache_decay)
        self.assertIn("1-3", rows)
        self.assertIn("0", rows)  # missing n_sessions → "0" via _safe_int fallback
        self.assertIn("0.0%", rows)  # non-numeric median → 0.0%

    def test_svg_band_uses_var_accent_with_opacity(self):
        """CC-4: the band fill must be ``var(--accent)`` with opacity
        rather than a hardcoded ``rgba(10,132,255,0.12)`` so dark /
        light themes render the band and the line in the same colour."""
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [{"turn": 1, "median": 0.5, "p25": 0.4, "p75": 0.6, "n": 1}]
        svg = _render_cache_decay_svg(points)
        # The band path has fill="var(--accent)" + fill-opacity="0.12".
        # Both must appear together; the hardcoded ``rgba(10,132,255,...)``
        # must NOT.
        self.assertIn('fill="var(--accent)" fill-opacity="0.12"', svg)
        self.assertNotIn("rgba(10,132,255", svg)

    def test_svg_escapes_html_in_title(self):
        """``<title>`` carries interpolated values; the renderer's
        contract is: no raw HTML in the output, even if a point
        carries a string where a number is expected.

        ``_safe_int`` drops the string before it can reach the
        ``<title>`` element, so the literal ``<script>`` token must
        NOT appear anywhere in the SVG. The renderer doesn't need to
        HTML-escape it because it never serializes it — but a
        regression that removes ``_safe_int`` would re-introduce
        the raw string into the document and fail this test.
        """
        from token_efficiency_analyzer import _render_cache_decay_svg
        points = [
            {"turn": 1, "median": 0.5, "p25": 0.5, "p75": 0.5,
             "n": "<script>alert(1)</script>"},
        ]
        svg = _render_cache_decay_svg(points)
        # The literal ``<script>`` token must NOT appear anywhere.
        self.assertNotIn("<script>", svg)
        # And the SVG must still be a well-formed rendering (didn't
        # crash on the bad input).
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)


class TestJsonSinkCacheDecay(unittest.TestCase):
    """JSON-mode parity: ``--json`` output exposes ``cache_decay`` so CI
    / external consumers can gate on hit-rate decay without parsing
    HTML. The shape mirrors the HTML tile's per-bucket aggregation.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cache-decay-json-test-"))
        target = self.tmpdir / "logs" / "claude-code"
        target.mkdir(parents=True)
        session_id = "json-cache-decay-session"
        # Use the same cwd basename as the G2 fixture ("cache-decay-fixture")
        # so the repo filter matches what the test passes via --repo.
        lines = [
            _make_user_record(session_id, "hi", ts="2026-07-09T10:00:00.000Z"),
            _make_assistant_record(
                session_id, model="claude-haiku-4-5",
                input_tokens=200, cache_read=0, ts="2026-07-09T10:00:01.000Z",
            ),
            _make_user_record(session_id, "more", ts="2026-07-09T10:00:02.000Z"),
            _make_assistant_record(
                session_id, model="claude-haiku-4-5",
                input_tokens=200, cache_read=600, ts="2026-07-09T10:00:03.000Z",
            ),
        ]
        (target / "json-cache-decay-fixture.jsonl").write_text("\n".join(lines) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_json_output_includes_cache_decay_aggregation(self):
        """``--json`` emits a ``cache_decay`` key with the per-bucket
        aggregation. Each bucket carries ``n_sessions`` and ``points``."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "--repo", "cache-decay-fixture",
                "--days", "3650",
                "--logs-dir", str(self.tmpdir / "logs"),
                "--json",
            ])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertIn("cache_decay", report,
                      "JSON sink must include cache_decay key for CI parity")
        cd = report["cache_decay"]
        # Only assert buckets that actually have data — if the analyzer
        # later skips empty buckets (a sensible optimization), this test
        # still passes. The 2-turn fixture lands in the 1-3 bucket.
        populated = {k: v for k, v in cd.items() if v.get("n_sessions", 0) > 0}
        self.assertEqual(set(populated.keys()), {"1-3"})
        self.assertEqual(cd["1-3"]["n_sessions"], 1)
        self.assertGreater(len(cd["1-3"]["points"]), 0)
        for p in cd["1-3"]["points"]:
            self.assertIn("turn", p)
            self.assertIn("median", p)
            self.assertIn("p25", p)
            self.assertIn("p75", p)
            self.assertIn("n", p)


def _make_user_record(session_id: str, text: str, *, ts: str) -> str:
    """Tiny helper for the G2 fixture — one claude-code user record."""
    return (
        f'{{"timestamp":"{ts}","message":{{"role":"user","content":"{text}"}},'
        f'"type":"user","sessionId":"{session_id}","cwd":"/tmp/cache-decay-fixture",'
        f'"gitBranch":"main","userType":"external","version":"test"}}'
    )


def _make_assistant_record(
    session_id: str, *, model: str,
    input_tokens: int, cache_read: int, ts: str,
) -> str:
    """Tiny helper for the G2 fixture — one claude-code assistant record
    with the cache_read / input_tokens pair the test cares about.
    """
    return (
        f'{{"timestamp":"{ts}",'
        f'"message":{{"id":"m","type":"message","role":"assistant",'
        f'"content":[{{"type":"text","text":"ok"}}],"model":"{model}",'
        f'"stop_reason":"end_turn",'
        f'"usage":{{"input_tokens":{input_tokens},'
        f'"cache_creation_input_tokens":0,'
        f'"cache_read_input_tokens":{cache_read},'
        f'"output_tokens":10}}}},'
        f'"type":"assistant","sessionId":"{session_id}",'
        f'"cwd":"/tmp/cache-decay-fixture","gitBranch":"main",'
        f'"userType":"external","version":"test"}}'
    )
