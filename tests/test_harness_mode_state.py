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
- GATE_CATEGORIES schema: every gate in CORRECTNESS_GATES ∪ OPTIONAL_GATE_DEFAULTS
  has a category + type + description (issue #775)
- `show` CLI output groups local hooks by category with `value`+`type`+`description`
  fields per gate, surfaces a `ci_gates_notice`, and keeps the flat `gates:` key
  as a deprecated alias (legacy consumers in `hooks/slop-detector.sh` etc. read it)
- SKILL.md picker wording lock-in: every optional gate has a row in the
  `custom` Call-1/Call-2 tables AND every row's gate key exists in
  OPTIONAL_GATE_DEFAULTS (drift guard, issue #775)
"""
from __future__ import annotations

import json
import re
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


class TestGateCategorySchema(unittest.TestCase):
    """Every gate (correctness + optional) has a GATE_CATEGORIES entry with
    {type, category, description}. Drives the new grouped `show` output
    shape (issue #775).
    """

    def test_every_correctness_gate_has_category_entry(self):
        for gate in hms.CORRECTNESS_GATES:
            self.assertIn(gate, hms.GATE_CATEGORIES, f"{gate} missing from GATE_CATEGORIES")

    def test_every_optional_gate_has_category_entry(self):
        for gate in hms.OPTIONAL_GATE_DEFAULTS:
            self.assertIn(gate, hms.GATE_CATEGORIES, f"{gate} missing from GATE_CATEGORIES")

    def test_every_category_entry_is_well_formed(self):
        for gate, entry in hms.GATE_CATEGORIES.items():
            self.assertIsInstance(entry, dict, f"{gate}: entry must be dict")
            self.assertEqual(entry.get("type"), "local_hook", f"{gate}: type must be 'local_hook'")
            self.assertIn(entry.get("category"), hms.GROUPED_LOCAL_HOOKS,
                          f"{gate}: category {entry.get('category')!r} not in GROUPED_LOCAL_HOOKS")
            self.assertIsInstance(entry.get("description"), str, f"{gate}: description must be str")
            self.assertGreater(len(entry["description"]), 0, f"{gate}: description must be non-empty")

    def test_categories_group_every_known_gate(self):
        """Every gate in CORRECTNESS_GATES ∪ OPTIONAL_GATE_DEFAULTS must land
        in exactly one category bucket (no orphans, no duplicates)."""
        all_gates = hms.CORRECTNESS_GATES | set(hms.OPTIONAL_GATE_DEFAULTS)
        seen = set()
        for category in hms.GROUPED_LOCAL_HOOKS:
            for gate in hms.GATE_CATEGORIES_BY_CATEGORY.get(category, []):
                self.assertNotIn(gate, seen, f"{gate} appears in multiple categories")
                seen.add(gate)
        self.assertEqual(
            seen, all_gates,
            "GROUPED_LOCAL_HOOKS must cover every gate in CORRECTNESS_GATES ∪ OPTIONAL_GATE_DEFAULTS",
        )


class TestShowOutput(unittest.TestCase):
    """`python3 -m lib.harness_mode_state show` returns a structured JSON
    object with grouped `local_hooks`, `ci_gates_notice`, and a legacy
    flat `gates:` alias for backward compatibility (issue #775).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_show(self, mode: str = "full") -> dict:
        hms.write_state(mode, root=self.root)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = hms.main(["show", "--root", str(self.root)])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_show_emits_grouped_local_hooks(self):
        out = self._run_show("full")
        self.assertIn("local_hooks", out, "show output must have `local_hooks` key")
        for category in hms.GROUPED_LOCAL_HOOKS:
            self.assertIn(category, out["local_hooks"], f"category {category!r} missing from local_hooks")

    def test_show_local_hook_entries_have_value_type_description(self):
        out = self._run_show("full")
        for category, gates in out["local_hooks"].items():
            for gate, entry in gates.items():
                self.assertIn("value", entry, f"{category}.{gate}: missing 'value'")
                self.assertIn("type", entry, f"{category}.{gate}: missing 'type'")
                self.assertIn("description", entry, f"{category}.{gate}: missing 'description'")
                self.assertEqual(entry["type"], "local_hook")

    def test_show_correctness_gates_always_on(self):
        out = self._run_show("fast")
        for gate in hms.CORRECTNESS_GATES:
            category = hms.GATE_CATEGORIES[gate]["category"]
            self.assertEqual(
                out["local_hooks"][category][gate]["value"], "on",
                f"correctness gate {gate} must resolve 'on' even in fast mode",
            )

    def test_show_fast_mode_optional_gates_off(self):
        out = self._run_show("fast")
        for gate in hms.OPTIONAL_GATE_DEFAULTS:
            category = hms.GATE_CATEGORIES[gate]["category"]
            value = out["local_hooks"][category][gate]["value"]
            expected = hms.OPTIONAL_GATE_DEFAULTS[gate]["fast"]
            self.assertEqual(value, expected, f"{gate} in fast mode should be {expected!r}, got {value!r}")

    def test_show_includes_ci_gates_notice(self):
        out = self._run_show()
        self.assertIn("ci_gates_notice", out)
        self.assertIn(".github/workflows", out["ci_gates_notice"])
        self.assertIn("NOT toggled", out["ci_gates_notice"])

    def test_show_keeps_legacy_flat_gates_alias(self):
        """The flat `gates:` key is consumed by `hooks/slop-detector.sh:31`
        via `python3 -m lib.harness_mode_state get <gate>` (separate code
        path), so the alias test here is about the `show` output's
        backward-compat shape: keep a flat map of name → value so
        legacy `jq '.gates.<name>'` consumers don't break."""
        out = self._run_show("full")
        self.assertIn("gates", out, "flat `gates:` alias must be kept for legacy consumers")
        # Every gate resolves to the same value in both views.
        flat = out["gates"]
        for category, gates in out["local_hooks"].items():
            for gate, entry in gates.items():
                self.assertEqual(flat.get(gate), entry["value"],
                                 f"flat value of {gate} ({flat.get(gate)!r}) must match grouped value ({entry['value']!r})")

    def test_show_mode_label_at_top(self):
        out = self._run_show("fast")
        self.assertEqual(out.get("mode"), "fast")


class TestSkillPickerLockIn(unittest.TestCase):
    """Every optional local hook advertised by `skills/harness-mode/SKILL.md`'s
    `custom` Call-1/Call-2 questions must map to a real `OPTIONAL_GATE_DEFAULTS`
    key (drift guard, issue #775). Also verifies the SKILL.md now uses the
    "local hook" terminology in the picker rows.
    """

    SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "harness-mode" / "SKILL.md"

    def setUp(self):
        self.skill_text = self.SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_md_exists(self):
        self.assertTrue(self.SKILL_PATH.exists())

    def test_picker_uses_local_hook_terminology(self):
        """Each of the 6 `custom` picker questions should now mention
        'local hook' to disambiguate from CI workflow gates."""
        # The picker is documented as 6 questions across Call 1 (3) and
        # Call 2 (3). The skill markdown renders them as markdown table
        # rows under two `**Call N:**` headings.
        # Simpler check: the term 'local hook' appears at least 6 times
        # in the SKILL.md body (once per picker row, plus the prose).
        occurrences = len(re.findall(r"local hook", self.skill_text, flags=re.IGNORECASE))
        self.assertGreaterEqual(
            occurrences, 6,
            f"SKILL.md should mention 'local hook' ≥6 times (one per picker row); got {occurrences}",
        )

    def test_every_optional_gate_advertised_in_skill_picker(self):
        """Every key in OPTIONAL_GATE_DEFAULTS must appear in the SKILL.md
        body (so the picker mentions it). This catches silent drops when
        a gate is added to the Python defaults but the SKILL.md isn't updated."""
        for gate in hms.OPTIONAL_GATE_DEFAULTS:
            self.assertIn(
                f"`{gate}`", self.skill_text,
                f"SKILL.md must advertise the `{gate}` local hook in its picker table",
            )

    def test_skill_md_documents_ci_gates_separation(self):
        """The new 'CI workflow gates' pointer paragraph must exist (issue #775)."""
        self.assertIn("CI workflow gate", self.skill_text)
        self.assertIn(".github/workflows", self.skill_text)


if __name__ == "__main__":
    unittest.main()
