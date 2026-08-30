#!/usr/bin/env python3
"""Tests for lib/harness_mode_state (workflow-fast-mode-lean).

Covers:
- read/write round-trip for the session state file
- correctness gates always resolve "on" regardless of file contents (the
  critical invariant — nothing can disable stop_verify/secret_scan/
  intent_integrity/gh_ci_required)
- missing/corrupt file defaults to mode=full, all gates on
- fast mode flips every optional gate to its "off" value
- custom mode per-gate overrides win over the mode default
- write_state() silently drops correctness-gate keys even if a caller tries
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import harness_mode_state as hms  # noqa: E402


class TestReadWriteRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_defaults_to_full(self):
        state = hms.read_state(self.root)
        self.assertEqual(state["mode"], "full")
        self.assertEqual(state["gates"], {})

    def test_corrupt_file_defaults_to_full(self):
        path = hms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        state = hms.read_state(self.root)
        self.assertEqual(state["mode"], "full")

    def test_invalid_mode_in_file_defaults_to_full(self):
        path = hms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
        state = hms.read_state(self.root)
        self.assertEqual(state["mode"], "full")

    def test_write_then_read_round_trip(self):
        hms.write_state("fast", root=self.root)
        state = hms.read_state(self.root)
        self.assertEqual(state["mode"], "fast")

    def test_write_state_rejects_invalid_mode(self):
        with self.assertRaises(ValueError):
            hms.write_state("bogus", root=self.root)

    def test_write_state_drops_correctness_gate_keys(self):
        hms.write_state("custom", gates={"stop_verify": "off", "tdd_scope_judge": "off"}, root=self.root)
        state = hms.read_state(self.root)
        self.assertNotIn("stop_verify", state["gates"])
        self.assertEqual(state["gates"]["tdd_scope_judge"], "off")


class TestResolvedGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_correctness_gates_always_on_with_no_state_file(self):
        for gate in hms.CORRECTNESS_GATES:
            self.assertEqual(hms.resolved_gate(gate, self.root), "on")

    def test_correctness_gates_always_on_even_if_fast_mode(self):
        hms.write_state("fast", root=self.root)
        for gate in hms.CORRECTNESS_GATES:
            self.assertEqual(hms.resolved_gate(gate, self.root), "on")

    def test_correctness_gates_always_on_even_with_hand_edited_file(self):
        """Even a state file that directly sets a correctness gate off (bypassing
        write_state()'s filter) must not affect resolved_gate() — the hardcode
        in resolved_gate() is the actual enforcement point, not write_state()."""
        path = hms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mode": "custom", "gates": {"stop_verify": "off", "secret_scan": "off"}}),
            encoding="utf-8",
        )
        self.assertEqual(hms.resolved_gate("stop_verify", self.root), "on")
        self.assertEqual(hms.resolved_gate("secret_scan", self.root), "on")

    def test_full_mode_all_optional_gates_on(self):
        hms.write_state("full", root=self.root)
        self.assertEqual(hms.resolved_gate("tdd_scope_judge", self.root), "on")
        self.assertEqual(hms.resolved_gate("slop_detector", self.root), "on")
        self.assertEqual(hms.resolved_gate("security_owasp", self.root), "full")
        self.assertEqual(hms.resolved_gate("babysit_pr", self.root), "full")

    def test_fast_mode_all_optional_gates_off(self):
        hms.write_state("fast", root=self.root)
        self.assertEqual(hms.resolved_gate("tdd_scope_judge", self.root), "off")
        self.assertEqual(hms.resolved_gate("slop_detector", self.root), "off")
        self.assertEqual(hms.resolved_gate("pre_commit_review", self.root), "off")
        self.assertEqual(hms.resolved_gate("maintenance", self.root), "off")
        self.assertEqual(hms.resolved_gate("security_owasp", self.root), "quick")
        self.assertEqual(hms.resolved_gate("babysit_pr", self.root), "manual")

    def test_custom_mode_per_gate_override_wins(self):
        hms.write_state(
            "custom",
            gates={"tdd_scope_judge": "off", "slop_detector": "on"},
            root=self.root,
        )
        self.assertEqual(hms.resolved_gate("tdd_scope_judge", self.root), "off")
        self.assertEqual(hms.resolved_gate("slop_detector", self.root), "on")
        # Gates not explicitly overridden in custom mode fall back to "full"'s value.
        self.assertEqual(hms.resolved_gate("maintenance", self.root), "on")


if __name__ == "__main__":
    unittest.main()
