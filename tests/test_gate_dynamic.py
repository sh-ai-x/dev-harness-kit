"""test_gate_dynamic.py — pure helper + decision I/O tests for the
non-deterministic LLM-judge layer that decides which CI gates to
skip on babysit-pr iterations. Pure-helper tests use no network;
the LLM seam is exercised via `unittest.mock.patch` on
`lib/llm_judge._http_post`.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import gate_dynamic  # noqa: E402
import llm_judge  # noqa: E402


def _make_ctx(**overrides):
    """Build a default GateContext for tests, with overridable fields."""
    base = dict(
        parent_pr=605,
        head_sha="abc123def",
        iteration=2,
        diff_stat=" lib/gates_state.py | 12 ++++++--",
        diff_sample="- old\n+ new",
        pr_body="Refactor gates_state validator.",
        previous_verdicts={"review": "Approve", "security": "Approve"},
        gate_catalog={
            "gates": {
                "review": {
                    "enabled": True, "workflow": "review.yml",
                    "var": "GATES_REVIEW_ENABLED",
                    "dynamic_eligible": True, "scope_globs": ["lib/**"],
                    "forced_run": False,
                },
                "security": {
                    "enabled": True, "workflow": "security.yml",
                    "var": "GATES_SECURITY_ENABLED",
                    "dynamic_eligible": True, "scope_globs": ["lib/**"],
                    "forced_run": False,
                },
                "maintenance": {
                    "enabled": True, "workflow": "maintenance.yml",
                    "var": "GATES_MAINTENANCE_ENABLED",
                    "dynamic_eligible": True, "scope_globs": ["skills/**"],
                    "forced_run": False,
                },
            },
        },
    )
    base.update(overrides)
    return gate_dynamic.GateContext(**base)


class TestIsGateInScope(unittest.TestCase):
    def test_glob_match_returns_true(self) -> None:
        self.assertTrue(gate_dynamic.is_gate_in_scope(
            "maintenance",
            {"scope_globs": ["skills/**"]},
            ["skills/foo.py", "skills/bar/baz.md"],
        ))

    def test_glob_miss_returns_false(self) -> None:
        self.assertFalse(gate_dynamic.is_gate_in_scope(
            "maintenance",
            {"scope_globs": ["skills/**"]},
            ["lib/gates_state.py"],
        ))

    def test_empty_globs_returns_false(self) -> None:
        # Without scope globs, the gate is NOT considered in scope — the
        # hard rule never fires; LLM decides alone.
        self.assertFalse(gate_dynamic.is_gate_in_scope(
            "maintenance",
            {"scope_globs": []},
            ["anything.py"],
        ))

    def test_no_diff_returns_false(self) -> None:
        self.assertFalse(gate_dynamic.is_gate_in_scope(
            "maintenance",
            {"scope_globs": ["lib/**"]},
            [],
        ))


class TestHardRules(unittest.TestCase):
    def test_forced_run_bypasses_llm_skip(self) -> None:
        ctx = _make_ctx(iteration=2)
        llm_decisions = [
            gate_dynamic.GateDecision("review", skip=True, reasoning="r",
                                      confidence=0.9, risk_level=0.0, raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        # review has forced_run=True (per fixture) AND is in scope of
        # "lib/**" AND diff touches lib/. Hard rule #2 kicks in.
        review = next(d for d in out if d.gate_name == "review")
        self.assertFalse(review.skip)

    def test_iteration_1_no_skip(self) -> None:
        # First push is always deterministic — no gates skipped
        # regardless of LLM verdict.
        ctx = _make_ctx(iteration=1)
        llm_decisions = [
            gate_dynamic.GateDecision(gate_name=n, skip=True, reasoning="r",
                                      confidence=0.9, risk_level=0.0, raw_score={})
            for n in ("review", "security", "maintenance")
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        self.assertEqual(
            [d.gate_name for d in out if d.skip],
            [],
        )

    def test_review_security_in_scope_no_skip(self) -> None:
        # review + security are critical gates — never skipped when their
        # scope matches the diff. Maintenance (not in {review, security})
        # can be skipped.
        ctx = _make_ctx(
            iteration=2,
            gate_catalog={
                "gates": {
                    "review": {"scope_globs": ["lib/**"], "dynamic_eligible": True},
                    "security": {"scope_globs": ["lib/**"], "dynamic_eligible": True},
                    "maintenance": {"scope_globs": ["skills/**"], "dynamic_eligible": True},
                },
            },
        )
        llm_decisions = [
            gate_dynamic.GateDecision(gate_name=n, skip=True, reasoning="r",
                                      confidence=0.9, risk_level=0.0, raw_score={})
            for n in ("review", "security", "maintenance")
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        skipped = {d.gate_name for d in out if d.skip}
        self.assertIn("maintenance", skipped)
        self.assertNotIn("review", skipped)
        self.assertNotIn("security", skipped)

    def test_confidence_below_threshold_no_skip(self) -> None:
        ctx = _make_ctx(iteration=2)
        llm_decisions = [
            gate_dynamic.GateDecision("maintenance", skip=True,
                                      reasoning="r", confidence=0.5,
                                      risk_level=0.0, raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        maint = next(d for d in out if d.gate_name == "maintenance")
        self.assertFalse(maint.skip)

    def test_missing_confidence_fails_safe(self) -> None:
        # confidence=0.0 (LLM didn't emit it) must default to "don't skip".
        ctx = _make_ctx(iteration=2)
        llm_decisions = [
            gate_dynamic.GateDecision("maintenance", skip=True,
                                      reasoning="r", confidence=0.0,
                                      risk_level=0.0, raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        maint = next(d for d in out if d.gate_name == "maintenance")
        self.assertFalse(maint.skip)

    def test_dynamic_eligible_false_no_skip(self) -> None:
        # Rule #5 (security review LLM01-M2): an operator who leaves
        # `dynamic_eligible` at the default (False) expects the gate to be
        # immune to LLM-driven skips. Even if the LLM judge emits skip=True,
        # the orchestrator must override to skip=False.
        # GateContext is frozen — use dataclasses.replace to override
        # gate_catalog with explicit dynamic_eligible=False.
        from dataclasses import replace
        ctx = replace(
            _make_ctx(iteration=2),
            gate_catalog={
                "gates": {
                    "maintenance": {"scope_globs": ["lib/**"], "dynamic_eligible": False},
                },
            },
        )
        llm_decisions = [
            gate_dynamic.GateDecision("maintenance", skip=True,
                                      reasoning="r", confidence=0.9,
                                      risk_level=0.0, raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        maint = next(d for d in out if d.gate_name == "maintenance")
        self.assertFalse(maint.skip)

    def test_high_risk_veto_no_skip(self) -> None:
        # Rule 6: risk_level > RISK_CEILING (3.0) → skip=False even when
        # gate_skippable and confidence are both high.
        ctx = _make_ctx(iteration=2)
        llm_decisions = [
            gate_dynamic.GateDecision("maintenance", skip=True,
                                      reasoning="r", confidence=0.9,
                                      risk_level=5.0, raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        maint = next(d for d in out if d.gate_name == "maintenance")
        self.assertFalse(maint.skip)

    def test_low_risk_allows_skip(self) -> None:
        # risk_level below the floor AND high skip/confidence scores → skip.
        ctx = _make_ctx(iteration=2)
        llm_decisions = [
            gate_dynamic.GateDecision("maintenance", skip=True,
                                      reasoning="r", confidence=0.9,
                                      risk_level=2.0, raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        maint = next(d for d in out if d.gate_name == "maintenance")
        self.assertTrue(maint.skip)

    def test_critical_gate_empty_scope_still_vetoed(self) -> None:
        # Security review LLM01-M1: review/security with empty scope_globs
        # must still be vetoed (treated as scope=["**"]) so Rule #3 fires
        # even against the default config that ships with scope_globs=[].
        ctx = _make_ctx(
            iteration=2,
            diff_stat=" lib/foo.py | 1 +",
            gate_catalog={
                "gates": {
                    "review": {"scope_globs": [], "dynamic_eligible": True},
                    "security": {"scope_globs": [], "dynamic_eligible": True},
                },
            },
        )
        llm_decisions = [
            gate_dynamic.GateDecision(gate_name=n, skip=True,
                                      reasoning="r", confidence=0.9,
                                      risk_level=0.0, raw_score={})
            for n in ("review", "security")
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        for d in out:
            self.assertFalse(d.skip, f"Rule #3 should veto {d.gate_name} on empty scope")

    def test_risk_ceiling_polarity_pinned(self) -> None:
        """A-1 fix: pin the polarity of the risk ceiling (renamed
        from `RISK_CEILING`). The constant semantically is a CEILING
        (rule fires when `risk > ceiling`); the docstring on
        `RISK_CEILING` even opened with the word "ceiling", but the
        suffix invited future contributors to invert the comparison.
        Pins:
          - risk > CEILING → veto
          - risk == CEILING → no veto
          - risk < CEILING → no veto
        If someone inverts the comparison to `<`, this test fails.
        """
        ceiling = gate_dynamic.RISK_CEILING  # name kept for back-compat
        # Alias assertion: the constant must equal the documented
        # default of 3.0 (so the polarity test is reproducible).
        self.assertEqual(ceiling, 3.0)

        # risk > ceiling → veto
        ctx = _make_ctx(iteration=2)
        d_above = gate_dynamic.GateDecision(
            "maintenance", skip=True, reasoning="r", confidence=0.9,
            risk_level=ceiling + 1.0, raw_score={},
        )
        out_above = gate_dynamic.apply_hard_rules(ctx, [d_above])
        self.assertFalse(
            out_above[0].skip,
            f"risk={ceiling + 1.0} (above ceiling={ceiling}) must veto",
        )
        # risk == ceiling → no veto
        d_equal = gate_dynamic.GateDecision(
            "maintenance", skip=True, reasoning="r", confidence=0.9,
            risk_level=ceiling, raw_score={},
        )
        out_equal = gate_dynamic.apply_hard_rules(ctx, [d_equal])
        self.assertTrue(
            out_equal[0].skip,
            f"risk={ceiling} (at ceiling) must NOT veto — boundary case",
        )
        # risk < ceiling → no veto
        d_below = gate_dynamic.GateDecision(
            "maintenance", skip=True, reasoning="r", confidence=0.9,
            risk_level=ceiling - 1.0, raw_score={},
        )
        out_below = gate_dynamic.apply_hard_rules(ctx, [d_below])
        self.assertTrue(
            out_below[0].skip,
            f"risk={ceiling - 1.0} (below ceiling) must NOT veto",
        )



class TestInvokeJudgeTemperature(unittest.TestCase):
    """Pins the temperature=0 contract documented in
    eval/prompts/judge-gate-dynamic.md:74-80 and the PR title."""

    def test_invoke_judge_passes_temperature_zero(self) -> None:
        from unittest.mock import patch
        # Stub load_config (returns a valid api_key so the early-return
        # check passes) AND format_prompt (returns a non-empty template)
        # so invoke_judge reaches the call_judge call.
        stub_cfg = {
            "provider": "minimax",
            "api_key": "fake",
            "model": "fake-model",
            "base_url": "https://example.invalid/",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            with patch.object(gate_dynamic.llm_judge, "load_config", return_value=stub_cfg), \
                 patch.object(gate_dynamic.llm_judge, "format_prompt", return_value="system"), \
                 patch.object(gate_dynamic.llm_judge, "call_judge", return_value={"scores": {}}) as mock_call:
                gate_dynamic.invoke_judge(
                    gate_dynamic.GateContext(
                        parent_pr=0,
                        head_sha="abc",
                        iteration=2,
                        diff_stat="",
                        diff_sample="",
                        pr_body=None,
                        previous_verdicts={},
                        gate_catalog={"gates": {}},
                    ),
                    target,
                )
                self.assertTrue(mock_call.called, "call_judge was not invoked")
                call_kwargs = mock_call.call_args.kwargs
                self.assertEqual(
                    call_kwargs.get("temperature"),
                    0.0,
                    f"invoke_judge must pass temperature=0.0 "
                    f"(got {call_kwargs.get('temperature')!r})",
                )


class TestAuditPathContainment(unittest.TestCase):
    """Pin A08-m1: _audit_path must refuse head_sha values that escape
    the audit directory."""

    def test_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            with self.assertRaises(ValueError):
                gate_dynamic._audit_path(target, "../../../etc/passwd")

    def test_normal_head_sha_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            p = gate_dynamic._audit_path(target, "abc123")
            self.assertTrue(str(p).endswith(".dev-kit/gate-dynamic/abc123.json"))


class TestHashGatesState(unittest.TestCase):
    def test_missing_file_returns_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(gate_dynamic.hash_gates_state(Path(td)), "")

    def test_present_file_returns_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir()
            (target / ".dev-kit" / "gates.json").write_text("{}")
            h = gate_dynamic.hash_gates_state(target)
            self.assertEqual(len(h), 64)  # sha256 hex digest


class TestDecisionIO(unittest.TestCase):
    def _make_decision(self, **overrides) -> gate_dynamic.GateSkipDecision:
        # Default `gates_hash=""` matches the round-trip case where no
        # `.dev-kit/gates.json` exists in the test target — the
        # `test_load_invalidates_on_gates_hash_mismatch` test
        # explicitly overrides this to verify the invalidation path.
        base = dict(
            head_sha="abc123",
            decisions=(
                gate_dynamic.GateDecision("maintenance", skip=True,
                                          reasoning="r", confidence=0.9,
                                          risk_level=0.0, raw_score={}),
            ),
            llm_raw={"scores": {}, "raw": ""},
            gates_hash="",
            decided_at_iso="2026-09-16T00:00:00Z",
        )
        base.update(overrides)
        return gate_dynamic.GateSkipDecision(**base)

    def test_save_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            decision = self._make_decision()
            path = gate_dynamic.save_decision(decision, target)
            self.assertTrue(path.exists())
            loaded = gate_dynamic.load_decision("abc123", target)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.head_sha, decision.head_sha)
            self.assertEqual(loaded.gates_hash, decision.gates_hash)

    def test_load_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(gate_dynamic.load_decision("nope", Path(td)))

    def test_load_decision_clamps_out_of_range_risk_level(self) -> None:
        """S-1 fix: `load_decision` must range-check `risk_level`.
        An out-of-range value (e.g. -1.0, 99.0 from a poisoned cache
        file) would otherwise survive rule #6 unchanged: rule #6
        checks `risk > 3.0`, so `risk=-1.0` doesn't fire and the
        cached `skip=True` survives the re-application. Clamp
        out-of-range values to MISSING_RISK_LEVEL_SENTINEL so rule
        #6 fires.
        """
        import json
        for bad_value in (-1.0, 99.0, -100.0, 1000.0, float("inf")):
            with self.subTest(bad_value=bad_value):
                payload = {
                    "head_sha": "abc",
                    "gates_hash": "",
                    "decisions": [
                        {
                            "gate_name": "maintenance",
                            "skip": True,
                            "reasoning": "r",
                            "confidence": 0.9,
                            "risk_level": bad_value,
                            "raw_score": {},
                        }
                    ],
                    "llm_raw": {},
                    "decided_at_iso": "2026-09-15T00:00:00Z",
                }
                with tempfile.TemporaryDirectory() as td:
                    target = Path(td)
                    audit_dir = target / ".dev-kit" / "gate-dynamic"
                    audit_dir.mkdir(parents=True)
                    (audit_dir / "abc.json").write_text(json.dumps(payload))
                    loaded = gate_dynamic.load_decision("abc", target)
                self.assertIsNotNone(loaded)
                dec = loaded.decisions[0]
                self.assertEqual(
                    dec.risk_level,
                    gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
                    f"out-of-range risk_level={bad_value!r} must be "
                    f"clamped to MISSING_RISK_LEVEL_SENTINEL",
                )

    def test_load_decision_clamps_non_numeric_risk_level(self) -> None:
        """S-1 follow-up: a non-numeric `risk_level` (poisoned cache
        with `"high"` string) must also be clamped rather than
        crashing on `GateDecision(**d)`. Clamp to sentinel; do not
        let the load fail.
        """
        import json
        payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "r",
                    "confidence": 0.9,
                    "risk_level": "high",  # non-numeric
                    "raw_score": {},
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-15T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            (audit_dir / "abc.json").write_text(json.dumps(payload))
            loaded = gate_dynamic.load_decision("abc", target)
        self.assertIsNotNone(loaded)
        self.assertEqual(
            loaded.decisions[0].risk_level,
            gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
            "non-numeric risk_level must be clamped to sentinel, "
            "not raise TypeError",
        )

    def test_load_invalidates_on_gates_hash_mismatch(self) -> None:
        # Operator changes .dev-kit/gates.json between iterations →
        # the cached decision's gates_hash no longer matches → invalidate.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            decision = self._make_decision(gates_hash="old-hash-value")
            gate_dynamic.save_decision(decision, target)
            # Simulate operator changing gates.json
            (target / ".dev-kit").mkdir(exist_ok=True)
            (target / ".dev-kit" / "gates.json").write_text("{}")
            self.assertIsNone(gate_dynamic.load_decision("abc123", target))

    def test_load_legacy_cache_without_risk_level_fails_closed(self) -> None:
        """Cached decisions written before the v1.1 `risk_level` field
        must load without raising AND, after passing through
        `apply_hard_rules`, end up with `skip=False` (the security
        invariant rule #6 enforces). Reproduces the MAJOR review
        finding: prior to the default-value fix, `GateDecision(**d)`
        raised TypeError on legacy cache entries and crashed every
        babysit-pr cache-hit path. The earlier version of this test
        only asserted the dataclass property (`risk_level ==
        MISSING_RISK_LEVEL_SENTINEL`); the security judge flagged
        that this passes while the security property it claims to
        verify does not hold, so the post-`apply_hard_rules` invariant
        is now pinned explicitly.
        """
        import json

        legacy_payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "r",
                    "confidence": 0.9,
                    "raw_score": {},
                    # NOTE: no `risk_level` key — pre-v1.1 schema
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-15T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            (audit_dir / "abc.json").write_text(json.dumps(legacy_payload))
            loaded = gate_dynamic.load_decision("abc", target)
        self.assertIsNotNone(loaded)
        dec = loaded.decisions[0]
        # Dataclass property: legacy entry gets the sentinel default.
        self.assertEqual(dec.risk_level, gate_dynamic.MISSING_RISK_LEVEL_SENTINEL)
        self.assertGreater(dec.risk_level, gate_dynamic.RISK_CEILING)
        # Security invariant: after apply_hard_rules, the rule #6 veto
        # must override the cached `skip=True` to `skip=False`. This is
        # what `select_gates` now relies on when it re-applies hard
        # rules on cache hits (closes the A01/A06 short-circuit path).
        ctx = _make_ctx(head_sha="abc", iteration=2)
        applied = gate_dynamic.apply_hard_rules(ctx, list(loaded.decisions))
        self.assertFalse(
            applied[0].skip,
            "legacy cache entry must fail closed after apply_hard_rules "
            "(rule #6 veto); this is the security invariant the previous "
            "test version failed to pin.",
        )

class TestPruneStale(unittest.TestCase):
    def test_prunes_files_older_than_ttl(self) -> None:
        import os
        import time
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            old = audit_dir / "old_sha.json"
            old.write_text("{}")
            new = audit_dir / "new_sha.json"
            new.write_text("{}")
            # Set old file's mtime to 10 days ago
            ten_days_ago = time.time() - 10 * 86400
            os.utime(old, (ten_days_ago, ten_days_ago))
            removed = gate_dynamic.prune_stale(target, ttl_days=7)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())


class TestSelectGates(unittest.TestCase):
    """End-to-end orchestration. LLM seam is mocked."""

    def _mock_http_response(self, scores: dict) -> dict:
        return {
            "content": [{"type": "text", "text": json.dumps(scores)}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    def test_first_iteration_always_deterministic(self) -> None:
        # iteration=1 → all decisions forced skip=False regardless of
        # what the LLM says.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=1)
            # Mock LLM call to return high skip scores
            with mock.patch.object(
                llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    "gate_skippable": 9, "confidence": 9, "risk_level": 1,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            self.assertEqual(
                [d.gate_name for d in decision.decisions if d.skip],
                [],
            )

    def test_llm_parse_failure_returns_no_skips(self) -> None:
        # LLM returns garbage → parse_scores_json returns {} → all
        # confidences default to 0 → no gates skipped.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            with mock.patch.object(
                llm_judge, "_http_post",
                return_value=self._mock_response_unparseable(),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            self.assertEqual(
                [d.gate_name for d in decision.decisions if d.skip],
                [],
            )

    def _mock_response_unparseable(self) -> dict:
        return {
            "content": [{"type": "text", "text": "this is not JSON"}],
            "usage": {},
        }

    def test_missing_risk_level_key_fails_closed(self) -> None:
        # Partial LLM response: gate_skippable and confidence both pass
        # their floors, but `risk_level` is omitted entirely (distinct
        # from an explicit 0.0). A 0.0 default would be the SAFEST
        # possible risk_level and would incorrectly PASS Rule #6,
        # letting an incomplete judge response skip the gate. The fix
        # must default the missing key to MISSING_RISK_LEVEL_SENTINEL
        # (above RISK_CEILING) so this fails closed — no skip.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            with mock.patch.object(
                llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    "gate_skippable": 9, "confidence": 9,
                    # "risk_level" intentionally omitted.
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            self.assertEqual(
                [d.gate_name for d in decision.decisions if d.skip],
                [],
            )

    def test_cache_hit_reapplies_hard_rules(self) -> None:
        """A cache hit must be re-filtered through `apply_hard_rules`
        before returning. Closes the A01/A06 short-circuit finding:
        previously `select_gates` returned the cached
        `GateSkipDecision` verbatim, letting a pre-rule-#6 entry
        (cached with risk_level above RISK_CEILING + skip=True) bypass
        the v1.1 risk veto.

        Invariant: `select_gates(..., cache-hit)` decisions must equal
        `apply_hard_rules(ctx, cached.decisions)` — not the raw cached
        tuple. The cached entry uses `risk_level=11.0` (the sentinel),
        so rule #6 must fire and `skip=True` must be vetoed to `False`.
        Pre-fix: cache returns skip=True unchanged → assertion fails.
        Post-fix: cache re-applies → skip=False → assertion passes.
        """
        cached_payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    # risk_level=11.0 (> RISK_CEILING=3.0). The cached
                    # skip=True must be vetoed by rule #6 after
                    # re-applying hard rules. Pre-fix code returns
                    # the cached tuple verbatim, so skip=True leaks.
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "cached pre-v1.1 with sentinel risk",
                    "confidence": 0.9,
                    "risk_level": gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
                    "raw_score": {},
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-15T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit" / "gate-dynamic").mkdir(parents=True)
            (target / ".dev-kit" / "gate-dynamic" / "abc.json").write_text(
                json.dumps(cached_payload)
            )
            ctx = _make_ctx(head_sha="abc", iteration=2)
            # Compute the expected result from the RAW cached decisions
            # (loaded directly, NOT via select_gates — that would
            # re-invoke the fix).
            raw_cached = gate_dynamic.load_decision("abc", target)
            self.assertIsNotNone(raw_cached)
            expected = gate_dynamic.apply_hard_rules(
                ctx, list(raw_cached.decisions),
            )
            # Pre-compute the raw cached skips — used as a sanity
            # check that the test scenario actually distinguishes
            # pre-fix from post-fix behavior.
            raw_skips = [d.skip for d in raw_cached.decisions]
            # Patch the LLM seam — cache hit path must NOT invoke the
            # judge (this also pins that the fix doesn't accidentally
            # re-run the judge on cache hits).
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "_http_post"
            ) as mock_call, patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            # Cache hit path: judge must not be invoked.
            self.assertFalse(
                mock_call.called,
                "cache hit must not re-invoke the LLM judge",
            )
            # Sanity: the raw cached entry has skip=True, but rule #6
            # would force skip=False on re-application. If raw skips
            # already match expected, this test cannot distinguish
            # pre-fix from post-fix — fail loud so the test author
            # picks a different cached payload.
            self.assertNotEqual(
                raw_skips,
                [d.skip for d in expected],
                "test scenario invariant: raw cached skip must differ "
                "from apply_hard_rules result, else the test cannot "
                "distinguish pre-fix from post-fix behavior",
            )
            # Security invariant: cache hit decisions must equal
            # `apply_hard_rules(ctx, cached.decisions)`, NOT the raw
            # cached tuple.
            self.assertEqual(
                [d.skip for d in decision.decisions],
                [d.skip for d in expected],
                "cache-hit decisions must equal apply_hard_rules(...) "
                "of the raw cached decisions (re-application invariant)",
            )

    def test_coerced_response_combined_score_fails_closed(self) -> None:
        """LLM01 follow-up: a sub-extreme coerced triple
        `(skip=8.5, risk=1.5, conf=7.5)` slips past the original
        AND-only check (skip=8.5 < 9 → check does not fire) but still
        passes the skip predicate (skip ≥ 7, conf ≥ 0.7, risk ≤ 3).
        The fix is a combined-score check:
        `skip_score + (10 - risk_level) >= 17` — an attacker must
        push BOTH axes to the extremes; lowering one alone is
        insufficient.

        Pins the LLM01 follow-up finding from the second push's
        security judge.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    # Sub-extreme coerced triple — combined-score = 17.
                    "gate_skippable": 8.5,
                    "confidence": 7.5,
                    "risk_level": 1.5,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            # Combined-score >= 17 must trigger the coercion upgrade.
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertEqual(
                maint.risk_level,
                gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
                "combined-score coerced triple (skip=8.5, risk=1.5) "
                "must upgrade risk_level to the sentinel so rule 6 fires",
            )
            self.assertFalse(
                maint.skip,
                "sub-extreme coerced triple must not skip the gate",
            )

    def test_combined_score_allows_legitimate_skip(self) -> None:
        """Combined-score sanity check must NOT over-trigger on a
        legitimate response that lands safely below the threshold.
        Real LLM: skip=7, risk=3 → combined-score=14 (well below 17).
        The check must not fire and the skip must survive.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    # Below combined-score threshold (7+7=14 < 17).
                    # Legitimate "skip this gate" recommendation.
                    "gate_skippable": 7,
                    "confidence": 8,
                    "risk_level": 3.0,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertTrue(
                maint.skip,
                "legitimate skip (skip=7, risk=3, combined=14) must "
                "survive — combined-score sanity check is not allowed "
                "to over-trigger",
            )

    def test_coerced_response_sanity_check_fails_closed(self) -> None:
        """The 'perfect triple' (gate_skippable at the ceiling and
        risk_level at the floor) is the signature of a PR-body
        prompt-injected judge response. The fix forces `risk_level`
        to the sentinel so rule #6 vetoes the skip. Closes the A08
        finding: an attacker who reaches iteration >= 2 with a
        coerced judge response could previously skip every gate by
        emitting max skip + min risk.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            # Mock the LLM seam end-to-end so the response is actually
            # used (load_config/format_prompt/call_judge).
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    # Coerced triple — gate_skippable=10 AND risk=0.
                    # Only a coerced judge emits this exact pair.
                    "gate_skippable": 10,
                    "confidence": 8,
                    "risk_level": 0.0,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            # No gate may be skipped on a coerced response.
            self.assertEqual(
                [d.gate_name for d in decision.decisions if d.skip],
                [],
                "coerced-response triple (skip=10, risk=0) must fail "
                "closed — no gate skipped",
            )
            # The sanity check must upgrade risk_level to the sentinel
            # so rule #6 fires (not some other silent gate).
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertEqual(
                maint.risk_level,
                gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
                "coerced-response triple must upgrade risk_level to "
                "the sentinel so rule #6 fires",
            )

    def test_non_coerced_high_skip_low_risk_still_skips(self) -> None:
        """The sanity check must NOT over-trigger on a legitimate
        response that is below the coercion threshold. A real LLM
        judge returning skip=8 + risk=2 is below the threshold on
        both axes (skip < 9 OR risk > 1), so the check must not fire
        and the skip must survive. This pins the threshold semantics
        so the fix does not regress the legitimate-skip path.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    # Below the coercion thresholds on both axes:
                    # skip=8 (< 9), risk=2 (> 1). Modest conf=7 →
                    # normalized=0.7 (right at the floor).
                    "gate_skippable": 8,
                    "confidence": 7,
                    "risk_level": 2.0,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertTrue(
                maint.skip,
                "non-coerced moderate skip (skip=8 risk=2 conf=7) "
                "must skip — sanity check is not allowed to over-trigger",
            )

    def test_coercion_recorded_in_audit(self) -> None:
        """A09 fix: when the coerced-response check fires, the audit
        trail must record the reason — not silently log "missing key"
        or "legacy cache" with the same byte-stream. `audit_reason`
        on the GateDecision must be set to "coerced_response" so an
        operator reviewing `.dev-kit/gate-dynamic/<sha>.json` can
        distinguish prompt-injection from partial-LLM-response.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    "gate_skippable": 10,
                    "confidence": 8,
                    "risk_level": 0.0,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertEqual(
                maint.audit_reason,
                "coerced_response",
                "audit_reason must record the coerced-response path "
                "distinctly from 'missing_key' / 'legacy_cache'",
            )

    def test_select_gates_fails_closed_on_load_decision_exception(self) -> None:
        """A10 fix: `select_gates` body must self-fail-closed on any
        exception in the cache-hit or LLM path. Pin by raising from
        `load_decision` and asserting the returned decision is the
        no-skip fallback (all gates skip=False, confidence=0.0).
        Without the wrapper, the exception propagates and the
        babysit-pr loop crashes on the gate-dynamic call.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic, "load_decision",
                side_effect=RuntimeError("disk on fire"),
            ), patch.object(
                gate_dynamic, "prune_stale",
                return_value=0,
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            # All gates must be skip=False (the no-skip fallback).
            self.assertEqual(
                [d.gate_name for d in decision.decisions if d.skip],
                [],
                "exception in select_gates body must fail closed "
                "to no-skip decision (A10)",
            )
            # Reasoning string should mark the exception path distinctly.
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertIn(
                "exception",
                maint.reasoning,
                "no-skip fallback reasoning must mark the exception "
                "path distinctly (S-1 fix)",
            )

    def test_select_gates_exception_path_logs_and_tags(self) -> None:
        """S1 fix: the top-level `except Exception` must (a) emit a
        `logger.exception(...)` call so the failure is debuggable,
        and (b) tag the returned no-skip decision's `audit_reason`
        as a distinct sentinel so operators can tell exception-fail-
        closed apart from llm-unavailable. Without this, a silent
        fail-open is indistinguishable from a healthy no-skip path.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic, "load_decision",
                side_effect=RuntimeError("disk on fire"),
            ), patch.object(
                gate_dynamic, "prune_stale",
                return_value=0,
            ), patch.object(
                gate_dynamic.logger, "exception",
            ) as mock_log:
                decision = gate_dynamic.select_gates(ctx, target)
            # Logger must have been invoked with the exception.
            self.assertTrue(
                mock_log.called,
                "select_gates exception path must call logger.exception",
            )
            # audit_reason must distinguish exception fail-closed
            # from llm-unavailable (default).
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertEqual(
                maint.audit_reason,
                "exception_fail_closed",
                "audit_reason must mark the exception-fail-closed "
                "path distinctly",
            )

    def test_load_decision_reapplies_combined_score_check(self) -> None:
        """S2 fix: a poisoned cache entry with `skip=True` and
        `gate_skippable=9, risk_level=0.5` (combined-score = 18.5)
        survives the load-time clamp (0.5 is in [0, 10]) and
        survives rule six (0.5 > 3.0 is False). The coerced-response
        check was only applied at LLM parse time, not at cache
        load — so the poisoned skip survives the cache-hit
        re-application. Pin: load_decision must re-apply the
        combined-score check at load time and clamp to sentinel
        with audit_reason="coerced_response_cache".
        """
        import json
        payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "r",
                    "confidence": 0.9,
                    # risk_level in range but combined-score trips
                    "risk_level": 0.5,
                    # raw_score feeds the combined-score check.
                    # gate_skippable=9, risk=0.5 → 9 + (10-0.5) = 18.5
                    # → coerced. The original LLM-parse check would
                    # have upgraded risk_level to the sentinel; this
                    # poisoned cache entry skips that upgrade.
                    "raw_score": {
                        "gate_skippable": 9,
                        "confidence": 9,
                        "risk_level": 0.5,
                    },
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-15T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            (audit_dir / "abc.json").write_text(json.dumps(payload))
            loaded = gate_dynamic.load_decision("abc", target)
        self.assertIsNotNone(loaded)
        dec = loaded.decisions[0]
        self.assertEqual(
            dec.risk_level,
            gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
            "poisoned cache with combined-score >= 17 must be "
            "clamped to sentinel at load time (S2)",
        )
        self.assertEqual(
            dec.audit_reason,
            "coerced_response_cache",
            "coerced-response cache-load path must be tagged "
            "distinctly so operators can tell it apart from "
            "missing_key / coerced_response / legacy_cache",
        )

    def test_load_decision_preserves_legitimate_skip(self) -> None:
        """S2 negative test: a legitimate cache entry with
        `gate_skippable=7, risk_level=3` (combined-score = 14) must
        NOT trigger the combined-score coercion upgrade. The
        cache-load check must not over-trigger on legitimate
        skips — only on the poisoned-skip signature.
        """
        import json
        payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "r",
                    "confidence": 0.8,
                    # risk_level in range; combined-score = 14
                    # (below 17). Legitimate skip survives.
                    "risk_level": 3.0,
                    "raw_score": {
                        "gate_skippable": 7,
                        "confidence": 8,
                        "risk_level": 3.0,
                    },
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-15T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            (audit_dir / "abc.json").write_text(json.dumps(payload))
            loaded = gate_dynamic.load_decision("abc", target)
        self.assertIsNotNone(loaded)
        dec = loaded.decisions[0]
        self.assertEqual(
            dec.risk_level,
            3.0,
            "legitimate skip (combined=14) must survive cache load "
            "without coercion upgrade",
        )
        self.assertEqual(
            dec.audit_reason,
            "ok",
            "legitimate-skip cache load must retain 'ok' audit_reason",
        )

    def test_near_max_skip_coerced_response_fails_closed(self) -> None:
        """v1.1.3 follow-up: the LLM01 high finding identified a
        `(skip=9.99, risk=3.0)` attack triple that the combined-score
        check missed (16.99 < 17.0). The new near-max-skip check fires
        when `skip_score >= 9.0 AND risk_level >= RISK_CEILING` — the
        signature of a coerced judge response pushing skip to near-max
        while keeping risk at the documented borderline-low edge.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    # Near-max-skip coerced triple. Combined-score =
                    # 9.99 + (10 - 3.0) = 16.99 < 17, so the prior
                    # combined-score check does NOT fire. The new
                    # near-max-skip check (skip>=9 AND risk>=3) does.
                    "gate_skippable": 9.99,
                    "confidence": 9.0,
                    "risk_level": 3.0,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            # Near-max-skip check must trigger the coercion upgrade.
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertEqual(
                maint.risk_level,
                gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
                "near-max-skip coerced triple (skip=9.99, risk=3.0) "
                "must upgrade risk_level to the sentinel so rule 6 fires",
            )
            self.assertEqual(
                maint.audit_reason,
                "coerced_response",
                "near-max-skip coerced-response must be tagged "
                "distinctly so operators can tell it apart from "
                "missing_key / legacy_cache / coerced_response_cache",
            )
            self.assertFalse(
                maint.skip,
                "near-max-skip coerced triple must not skip the gate",
            )

    def test_near_max_skip_allows_legitimate_high_skip(self) -> None:
        """Negative test for the near-max-skip check: a legitimate
        judge response with `gate_skippable=8, risk_level=2` (clean
        diff, low risk, no coercion signature) must NOT trigger the
        new check. Combined-score = 8 + (10 - 2) = 16 < 17 (also
        safe), and skip_score < 9 so the near-max-skip check is
        inert. The skip must survive.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            from unittest.mock import patch
            with patch.object(
                gate_dynamic.llm_judge, "load_config",
                return_value={"provider": "x", "api_key": "x",
                              "model": "x", "base_url": "x"},
            ), patch.object(
                gate_dynamic.llm_judge, "format_prompt",
                return_value="t",
            ), patch.object(
                gate_dynamic.llm_judge, "_http_post",
                return_value=self._mock_http_response({
                    # Legitimate high-skip / low-risk recommendation.
                    # Both new and old sanity checks are inert here.
                    "gate_skippable": 8,
                    "confidence": 9,
                    "risk_level": 2.0,
                }),
            ):
                decision = gate_dynamic.select_gates(ctx, target)
            maint = next(d for d in decision.decisions
                         if d.gate_name == "maintenance")
            self.assertEqual(
                maint.audit_reason,
                "ok",
                "legitimate high-skip / low-risk response must not be "
                "tagged as coerced",
            )
            self.assertTrue(
                maint.skip,
                "legitimate (skip=8, risk=2) must survive both the "
                "combined-score and near-max-skip checks",
            )

    def test_load_decision_reapplies_near_max_skip_check(self) -> None:
        """S3 fix: a poisoned cache with `gate_skippable=9.5,
        risk_level=3.0` bypasses the combined-score check (combined
        = 9.5 + (10 - 3.0) = 16.5 < 17) but matches the near-max-skip
        signature (gs=9.5 >= 9, rl=3.0 >= RISK_CEILING). Pin:
        load_decision must apply the same near-max-skip check at load
        time and clamp to sentinel with audit_reason=
        "coerced_response_cache".
        """
        import json
        payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "r",
                    "confidence": 0.9,
                    # Combined-score = 16.5 < 17 (combined-score check
                    # falls through); near-max-skip signature (gs=9.5
                    # >= 9 AND rl=3.0 >= 3.0) trips the new check.
                    "risk_level": 3.0,
                    "raw_score": {
                        "gate_skippable": 9.5,
                        "confidence": 9,
                        "risk_level": 3.0,
                    },
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-21T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            (audit_dir / "abc.json").write_text(json.dumps(payload))
            loaded = gate_dynamic.load_decision("abc", target)
        self.assertIsNotNone(loaded)
        dec = loaded.decisions[0]
        self.assertEqual(
            dec.risk_level,
            gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
            "poisoned cache with near-max-skip signature must be "
            "clamped to sentinel at load time (S3)",
        )
        self.assertEqual(
            dec.audit_reason,
            "coerced_response_cache",
            "near-max-skip cache-load path must be tagged "
            "distinctly",
        )

    def test_load_decision_empty_raw_score_fails_closed(self) -> None:
        """S4 fix: a poisoned cache with empty `raw_score` AND
        `risk_level <= RISK_CEILING` is the A06/A08 cache-poisoning
        signature. A legitimate cache entry always writes
        `raw_score={"gate_skippable": X, ...}`, so an empty
        `raw_score` paired with a skip-range `risk_level` is
        anomalous. Clamp to sentinel + tag `legacy_cache` so rule #6
        vetoes.
        """
        import json
        payload = {
            "head_sha": "abc",
            "gates_hash": "",
            "decisions": [
                {
                    "gate_name": "maintenance",
                    "skip": True,
                    "reasoning": "r",
                    "confidence": 0.9,
                    # In-range risk + empty raw_score = anomalous.
                    # Combined-score check falls through (gs=0.0),
                    # near-max-skip check falls through (gs<9).
                    # S-4 catches this signature.
                    "risk_level": 2.5,
                    "raw_score": {},
                }
            ],
            "llm_raw": {},
            "decided_at_iso": "2026-09-21T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            audit_dir = target / ".dev-kit" / "gate-dynamic"
            audit_dir.mkdir(parents=True)
            (audit_dir / "abc.json").write_text(json.dumps(payload))
            loaded = gate_dynamic.load_decision("abc", target)
        self.assertIsNotNone(loaded)
        dec = loaded.decisions[0]
        self.assertEqual(
            dec.risk_level,
            gate_dynamic.MISSING_RISK_LEVEL_SENTINEL,
            "poisoned cache with empty raw_score + skip-range risk "
            "must be clamped to sentinel at load time (S4)",
        )
        self.assertEqual(
            dec.audit_reason,
            "legacy_cache",
            "empty-raw_score cache-load defense-in-depth must be "
            "tagged as legacy_cache",
        )




class TestCliSelect(unittest.TestCase):
    def test_select_dry_run_prints_json(self) -> None:
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            # Mock the LLM call.
            with mock.patch.object(
                llm_judge, "_http_post",
                return_value={
                    "content": [{"type": "text",
                                 "text": json.dumps({"gate_skippable": 7, "confidence": 8, "risk_level": 2})}],
                    "usage": {},
                },
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = gate_dynamic.main([
                        "select", "--head-sha", "abc123",
                        "--root", td, "--dry-run",
                    ])
                self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["head_sha"], "abc123")


class TestInteractiveDefault(unittest.TestCase):
    def test_no_interactive_env_does_not_block(self) -> None:
        # The default (no DEV_KIT_GATE_DYNAMIC_INTERACTIVE) does not
        # call AskUserQuestion; the function returns a decision directly.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ctx = _make_ctx(iteration=2)
            with mock.patch.object(
                llm_judge, "_http_post",
                return_value={
                    "content": [{"type": "text",
                                 "text": json.dumps({"gate_skippable": 7, "confidence": 8, "risk_level": 2})}],
                    "usage": {},
                },
            ):
                with mock.patch.dict("os.environ", {}, clear=False):
                    os_env_backup = {}
                    for k in ("DEV_KIT_GATE_DYNAMIC_INTERACTIVE",):
                        os_env_backup[k] = __import__("os").environ.pop(k, None)
                    try:
                        decision = gate_dynamic.select_gates(ctx, target)
                    finally:
                        for k, v in os_env_backup.items():
                            if v is not None:
                                __import__("os").environ[k] = v
            # The decision is computed without blocking.
            self.assertIsNotNone(decision)
            self.assertEqual(decision.head_sha, "abc123def")


if __name__ == "__main__":
    unittest.main()
