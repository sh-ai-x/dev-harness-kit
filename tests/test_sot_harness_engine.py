"""Tests for lib/sot_harness_engine.py — pure synthesizer tests."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sot_harness_engine import (
    ROUNDS,
    SOT_HANDOFF_GENERATED_BY,
    SOT_HANDOFF_KIND,
    SOT_HANDOFF_STATUS_HELD,
    SOT_HANDOFF_STATUS_LOCKED,
    RoundDecision,
    RoundLogEntry,
    SOTDecisionSet,
    _rec_for,
    _safe_session_id,
    synthesize_sot,
    write_decision_log,
    write_sot_handout,
)


def _full_decision_set() -> SOTDecisionSet:
    """Build a complete decision set: accept the first recommendation of every round."""
    ds = SOTDecisionSet(
        project_name="test-project",
        idea_one_liner="Test idea for SOT.",
        session_id="test-session",
    )
    for round_obj in ROUNDS:
        rec = round_obj.recommendations[0]
        ds.decisions[round_obj.key] = RoundDecision(
            round_key=round_obj.key,
            recommendation_id=rec.id,
            decision="accept",
        )
    ds.open_questions = ["what about X?"]
    return ds


class TestRounds(unittest.TestCase):
    def test_five_rounds(self):
        self.assertEqual(len(ROUNDS), 5)

    def test_keys(self):
        keys = {r.key for r in ROUNDS}
        self.assertEqual(
            keys,
            {"project_context", "verification", "context", "safety", "lifecycle"},
        )

    def test_each_round_has_at_least_two_recommendations(self):
        for r in ROUNDS:
            self.assertGreaterEqual(
                len(r.recommendations), 2, f"round {r.key} has < 2 recs"
            )

    def test_every_recommendation_has_source(self):
        for r in ROUNDS:
            for rec in r.recommendations:
                self.assertTrue(
                    rec.source_url.startswith("https://"),
                    f"{r.key}/{rec.id} missing source URL",
                )
                self.assertTrue(rec.thesis, f"{r.key}/{rec.id} missing thesis")


class TestRecLookup(unittest.TestCase):
    def test_lookup_round_rec(self):
        rec = _rec_for("project_context", "long_running")
        self.assertIsNotNone(rec)
        self.assertIn("initializer", rec.thesis.lower())

    def test_lookup_missing(self):
        self.assertIsNone(_rec_for("project_context", "nonsense"))


class TestValidate(unittest.TestCase):
    def test_complete_set_validates(self):
        ds = _full_decision_set()
        self.assertEqual(ds.validate(), [])

    def test_incomplete_set_fails(self):
        ds = SOTDecisionSet(project_name="x", idea_one_liner="y")
        ds.decisions["project_context"] = RoundDecision(
            round_key="project_context",
            recommendation_id="long_running",
            decision="accept",
        )
        errs = ds.validate()
        self.assertTrue(any("missing decisions" in e for e in errs))

    def test_customize_requires_text(self):
        ds = _full_decision_set()
        ds.decisions["lifecycle"] = RoundDecision(
            round_key="lifecycle",
            recommendation_id="ralph_loop",
            decision="customize",
            customize_text="",  # missing
        )
        errs = ds.validate()
        self.assertTrue(any("customize" in e for e in errs))

    def test_cross_round_rec_id_rejected(self):
        # recommendation_id 'subagent_firewall' belongs to round 'context',
        # not 'project_context'; validate() must flag the mismatch.
        ds = _full_decision_set()
        ds.decisions["project_context"] = RoundDecision(
            round_key="project_context",
            recommendation_id="subagent_firewall",
            decision="accept",
        )
        errs = ds.validate()
        self.assertTrue(
            any("does not belong" in e for e in errs),
            f"expected cross-round ID error, got {errs!r}",
        )

    def test_reject_requires_note(self):
        ds = _full_decision_set()
        ds.decisions["context"] = RoundDecision(
            round_key="context",
            recommendation_id="subagent_firewall",
            decision="reject",
            note="",  # missing — rejects must cite a reason (VM-3)
        )
        errs = ds.validate()
        self.assertTrue(
            any("reject" in e and "reason" in e for e in errs),
            f"expected reject-reason error, got {errs!r}",
        )


class TestSynthesize(unittest.TestCase):
    def test_synthesize_contains_all_dimensions(self):
        ds = _full_decision_set()
        out = synthesize_sot(ds)
        for round_obj in ROUNDS:
            self.assertIn(
                round_obj.key.replace("_", " ").title(), out,
                f"sot doc missing dimension: {round_obj.key}",
            )

    def test_synthesize_contains_sources(self):
        ds = _full_decision_set()
        out = synthesize_sot(ds)
        # Every accepted recommendation's source URL should appear
        for round_obj in ROUNDS:
            rec = round_obj.recommendations[0]
            self.assertIn(rec.source_url, out)

    def test_synthesize_contains_implementation_phases(self):
        ds = _full_decision_set()
        out = synthesize_sot(ds)
        self.assertIn("Phase 1: Project Context", out)
        self.assertIn("Phase 2: Lifecycle", out)
        self.assertIn("Phase 3: Verification", out)
        self.assertIn("Phase 4: Context", out)
        self.assertIn("Phase 5: Safety", out)

    def test_synthesize_contains_acceptance_criteria(self):
        ds = _full_decision_set()
        out = synthesize_sot(ds)
        self.assertIn("Acceptance Criteria", out)
        self.assertIn("A1", out)
        self.assertIn("A5", out)

    def test_synthesize_includes_rejected_with_reason(self):
        ds = _full_decision_set()
        # Reject a recommendation with a reason
        ds.decisions["context"] = RoundDecision(
            round_key="context",
            recommendation_id="subagent_firewall",
            decision="reject",
            note="too much complexity for our team",
        )
        out = synthesize_sot(ds)
        self.assertIn("Rejected Patterns", out)
        self.assertIn("too much complexity", out)

    def test_synthesize_incomplete_returns_held(self):
        ds = SOTDecisionSet(project_name="x", idea_one_liner="y")
        out = synthesize_sot(ds)
        self.assertIn("INCOMPLETE", out)
        self.assertIn("held", out)


class TestSafeSessionId(unittest.TestCase):
    def test_default_passes_through(self):
        self.assertEqual(_safe_session_id("default"), "default")

    def test_traversal_collapsed(self):
        # '../../etc/passwd' becomes '.._.._etc_passwd' (slashes become underscores);
        # the path component never contains '/' so the file lands inside .dev-kit/...
        assert _safe_session_id("../../etc/passwd") == ".._.._etc_passwd"
        for ch in "/\\":
            assert ch not in _safe_session_id("../../etc/passwd")

    def test_empty_falls_back_to_default(self):
        self.assertEqual(_safe_session_id(""), "default")

    def test_long_input_truncated(self):
        self.assertEqual(len(_safe_session_id("a" * 200)), 64)


class TestWrite(unittest.TestCase):
    def test_write_sot_handout_creates_file(self):
        import tempfile
        ds = _full_decision_set()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = write_sot_handout(ds, root)
            self.assertTrue(target.exists())
            self.assertIn("SOT Harness Document", target.read_text())

    def test_write_decision_log_creates_file(self):
        import tempfile
        ds = _full_decision_set()
        log = [
            RoundLogEntry(
                round_key=r.key,
                question=r.question,
                user_choice=r.recommendations[0].id,
                note="user picked first option",
            )
            for r in ROUNDS
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = write_decision_log(ds, log, root)
            self.assertTrue(target.exists())
            self.assertIn("Decision log", target.read_text())
            self.assertIn("user picked first option", target.read_text())


class TestWriteSotHandoutFrontmatter(unittest.TestCase):
    """The SOT handoff must carry YAML frontmatter so the plan-skill consume
    gate (Phase 6, issue #898) can route it via --from-sot instead of
    misinterpreting it as an interview handoff.

    Required keys:
      handoff_kind: sot
      status: locked (when complete) | held (when incomplete)
      session_id: <session>
      generated_by: sot-harness-writer
    """

    @staticmethod
    def _extract_frontmatter(text: str) -> dict:
        """Tiny YAML-subset parser for the frontmatter tests.

        Mirrors the parser in lib/hand_off_consume.py but kept inline so
        this test file remains the canonical pin for the SOT writer
        contract (the consumer-side helper is exercised separately).
        """
        m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        if not m:
            return {}
        block = m.group(1)
        out: dict = {}
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"').strip("'")
        return out

    def test_complete_set_writes_locked_frontmatter(self):
        import tempfile
        ds = _full_decision_set()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = write_sot_handout(ds, root)
            text = target.read_text()
            fm = self._extract_frontmatter(text)
            self.assertEqual(fm.get("handoff_kind"), SOT_HANDOFF_KIND)
            self.assertEqual(fm.get("status"), SOT_HANDOFF_STATUS_LOCKED)
            self.assertEqual(fm.get("session_id"), ds.session_id)
            self.assertEqual(fm.get("generated_by"), SOT_HANDOFF_GENERATED_BY)

    def test_incomplete_set_writes_held_frontmatter(self):
        import tempfile
        # Missing decisions → validate() returns errors → synthesize_sot
        # emits the INCOMPLETE doc with status: held.
        ds = SOTDecisionSet(
            project_name="incomplete",
            idea_one_liner="partial interview",
            session_id="partial-session",
        )
        ds.decisions["project_context"] = RoundDecision(
            round_key="project_context",
            recommendation_id="long_running",
            decision="accept",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = write_sot_handout(ds, root)
            fm = self._extract_frontmatter(target.read_text())
            self.assertEqual(fm.get("handoff_kind"), SOT_HANDOFF_KIND)
            self.assertEqual(fm.get("status"), SOT_HANDOFF_STATUS_HELD)

    def test_sot_handoff_filename_matches_session(self):
        import tempfile
        ds = _full_decision_set()
        ds.session_id = "alpha-1"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = write_sot_handout(ds, root)
            self.assertEqual(target.name, "sot-harness-alpha-1.md")

    def test_sot_handoff_sits_alongside_interview_glob(self):
        # The plan-skill consume gate restricts interview discovery to
        # interview-*.md; the SOT writer must NOT use that filename pattern.
        import tempfile
        ds = _full_decision_set()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = write_sot_handout(ds, root)
            self.assertTrue(target.name.startswith("sot-harness-"))
            self.assertFalse(target.name.startswith("interview-"))


if __name__ == "__main__":
    unittest.main()
