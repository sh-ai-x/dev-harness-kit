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
                                      confidence=0.9, raw_score={}),
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
                                      confidence=0.9, raw_score={})
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
                                      confidence=0.9, raw_score={})
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
                                      raw_score={}),
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
                                      raw_score={}),
        ]
        out = gate_dynamic.apply_hard_rules(ctx, llm_decisions)
        maint = next(d for d in out if d.gate_name == "maintenance")
        self.assertFalse(maint.skip)


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
                                          raw_score={}),
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
