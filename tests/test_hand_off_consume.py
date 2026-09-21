"""Tests for lib/hand_off_consume.py — the plan-skill hand-off consume gate.

This is the deterministic SSOT for the routing decision that issue #898
called out. Before this module existed, the plan skill's "interview
consume gate" read `.dev-kit/hand-off/<step>.md` frontmatter generically
and could pick up a `sot-harness-*.md` file (which carries
`status: locked`) as if it were an interview handoff — failing closed
with "interview held" when the real state was a successful SOT lock.

The four regression scenarios pinned by issue #898:

1. SOT `status: locked` via `--from-sot <path>` proceeds (path=from_sot,
   status=locked).
2. SOT `status: locked` without `--from-sot` is reported as a routing
   error (path=error, status=misrouted) — NOT as "interview held".
3. Phase 6 interview statuses (`ok`, `best-effort`, `user-acknowledged`)
   retain current behavior (path=interview, status=<value>).
4. Missing or held interview handoff remains fail-closed (path=error,
   status=missing or held).

Plus typed-discriminator / filename-shape contracts:
- interview discovery matches `interview-*.md` only.
- SOT discovery matches `sot-harness-*.md` only.
- interview handoff MUST carry `handoff_kind: interview`.
- SOT handoff MUST carry `handoff_kind: sot`.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import hand_off_consume as hoc  # noqa: E402

# ----- helpers -----


def _write_sot_handoff(
    root: Path,
    *,
    session_id: str = "alpha",
    status: str = "locked",
    handoff_kind: str = "sot",
) -> Path:
    """Write a minimal SOT handoff with the discriminator frontmatter."""
    target = root / ".dev-kit" / "hand-off" / f"sot-harness-{session_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        textwrap.dedent(f"""\
        ---
        handoff_kind: {handoff_kind}
        status: {status}
        session_id: {session_id}
        generated_by: sot-harness-writer
        ---

        # SOT Harness Document — alpha project

        (test fixture body)
        """)
    )
    return target


def _write_interview_handoff(
    root: Path,
    *,
    session_id: str = "alpha",
    status: str = "ok",
    handoff_kind: str = "interview",
    body_extra: str = "",
) -> Path:
    """Write a minimal interview handoff with the 5-field frontmatter."""
    target = root / ".dev-kit" / "hand-off" / f"interview-{session_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        textwrap.dedent(f"""\
        ---
        handoff_kind: {handoff_kind}
        status: {status}
        session_id: {session_id}
        value_score: 1.0
        ambiguity_score: 2
        evidence_count: 5
        ---
        {body_extra}
        """)
    )
    return target


# ----- parse_yaml_frontmatter -----


class TestParseYamlFrontmatter(unittest.TestCase):
    def test_parses_simple_frontmatter(self):
        text = "---\nfoo: bar\nbaz: qux\n---\nbody\n"
        out = hoc.parse_yaml_frontmatter(text)
        self.assertEqual(out, {"foo": "bar", "baz": "qux"})

    def test_returns_none_when_no_frontmatter(self):
        out = hoc.parse_yaml_frontmatter("just a body\n")
        self.assertIsNone(out)

    def test_returns_none_when_frontmatter_unterminated(self):
        out = hoc.parse_yaml_frontmatter("---\nfoo: bar\nbody\n")
        self.assertIsNone(out)

    def test_strips_quotes(self):
        text = "---\nfoo: \"bar\"\nbaz: 'qux'\n---\n"
        out = hoc.parse_yaml_frontmatter(text)
        self.assertEqual(out, {"foo": "bar", "baz": "qux"})

    def test_ignores_blank_and_comment_lines(self):
        text = "---\n# comment\n\nfoo: bar\n---\n"
        out = hoc.parse_yaml_frontmatter(text)
        self.assertEqual(out, {"foo": "bar"})


# ----- discover_* -----


class TestDiscover(unittest.TestCase):
    def test_discover_interview_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(hoc.discover_interview_handoff(Path(td)))

    def test_discover_sot_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(hoc.discover_sot_handoff(Path(td)))

    def test_discover_interview_skips_sot_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_sot_handoff(root, session_id="only-sot")
            self.assertIsNone(hoc.discover_interview_handoff(root))

    def test_discover_sot_skips_interview_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, session_id="only-interview")
            self.assertIsNone(hoc.discover_sot_handoff(root))

    def test_discover_interview_finds_single_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, session_id="a")
            self.assertEqual(hoc.discover_interview_handoff(root), path)

    def test_discover_sot_finds_single_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_sot_handoff(root, session_id="a")
            self.assertEqual(hoc.discover_sot_handoff(root), path)

    def test_discover_interview_raises_on_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, session_id="a")
            _write_interview_handoff(root, session_id="b")
            with self.assertRaises(ValueError):
                hoc.discover_interview_handoff(root)

    def test_discover_sot_raises_on_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_sot_handoff(root, session_id="a")
            _write_sot_handoff(root, session_id="b")
            with self.assertRaises(ValueError):
                hoc.discover_sot_handoff(root)


# ----- validate_sot_handoff -----


class TestValidateSotHandoff(unittest.TestCase):
    def test_locked_sot_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_sot_handoff(root, status="locked")
            ok, status, reason = hoc.validate_sot_handoff(path)
            self.assertTrue(ok)
            self.assertEqual(status, "locked")
            self.assertEqual(reason, "")

    def test_held_sot_is_invalid_with_held_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_sot_handoff(root, status="held")
            ok, status, reason = hoc.validate_sot_handoff(path)
            self.assertFalse(ok)
            self.assertEqual(status, "held")

    def test_missing_handoff_kind_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / ".dev-kit" / "hand-off" / "sot-harness-x.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("---\nstatus: locked\n---\nbody\n")
            ok, _, reason = hoc.validate_sot_handoff(target)
            self.assertFalse(ok)
            self.assertIn("handoff_kind", reason)

    def test_wrong_handoff_kind_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_sot_handoff(root, handoff_kind="interview")
            ok, _, reason = hoc.validate_sot_handoff(path)
            self.assertFalse(ok)
            self.assertIn("handoff_kind", reason)

    def test_missing_file_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / ".dev-kit" / "hand-off" / "sot-harness-missing.md"
            ok, _, reason = hoc.validate_sot_handoff(target)
            self.assertFalse(ok)
            self.assertIn("not found", reason.lower())


# ----- validate_interview_handoff -----


class TestValidateInterviewHandoff(unittest.TestCase):
    def test_ok_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, status="ok")
            ok, status, reason = hoc.validate_interview_handoff(path)
            self.assertTrue(ok)
            self.assertEqual(status, "ok")

    def test_best_effort_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, status="best-effort")
            ok, status, reason = hoc.validate_interview_handoff(path)
            self.assertTrue(ok)
            self.assertEqual(status, "best-effort")

    def test_user_acknowledged_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, status="user-acknowledged")
            ok, status, reason = hoc.validate_interview_handoff(path)
            self.assertTrue(ok)
            self.assertEqual(status, "user-acknowledged")

    def test_held_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, status="held")
            ok, status, reason = hoc.validate_interview_handoff(path)
            self.assertFalse(ok)
            self.assertEqual(status, "held")

    def test_unknown_status_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, status="locked")  # SOT-ish
            ok, status, reason = hoc.validate_interview_handoff(path)
            self.assertFalse(ok)
            self.assertEqual(status, "locked")
            self.assertIn("interview", reason.lower())

    def test_missing_handoff_kind_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / ".dev-kit" / "hand-off" / "interview-x.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("---\nstatus: ok\n---\nbody\n")
            ok, _, reason = hoc.validate_interview_handoff(target)
            self.assertFalse(ok)
            self.assertIn("handoff_kind", reason)


# ----- routing_decision — issue #898 regression -----


class TestRoutingDecision(unittest.TestCase):
    """The 4 scenarios pinned by issue #898.

    Routing rule summary (path -> what plan does):
      interview  | proceed to Gate 1, treat interview answers as canonical.
      from_sot   | proceed with the SOT doc as the design record.
      skip       | proceed (--skip-interview backward-compat path).
      error      | refuse to plan; surface the actionable reason.
    """

    # --- Scenario 1: SOT locked via --from-sot proceeds ---
    def test_scenario_1_sot_locked_via_from_sot_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sot_path = _write_sot_handoff(root, session_id="ok-session", status="locked")
            decision = hoc.routing_decision(root, from_sot_arg=str(sot_path))
            self.assertEqual(decision["path"], "from_sot")
            self.assertEqual(decision["status"], "locked")
            self.assertEqual(decision["handoff_path"], sot_path)
            self.assertFalse(decision["error"])

    # --- Scenario 2: SOT locked without --from-sot is a routing error ---
    def test_scenario_2_sot_locked_without_from_sot_is_routing_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_sot_handoff(root, status="locked")
            decision = hoc.routing_decision(root, from_sot_arg=None)
            self.assertEqual(decision["path"], "error")
            self.assertEqual(decision["status"], "misrouted")
            self.assertIn("--from-sot", decision["reason"])

    # --- Scenario 3: Phase 6 statuses retain current behavior ---
    def test_scenario_3a_interview_ok_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = _write_interview_handoff(root, status="ok")
            decision = hoc.routing_decision(root, from_sot_arg=None)
            self.assertEqual(decision["path"], "interview")
            self.assertEqual(decision["status"], "ok")
            self.assertEqual(decision["handoff_path"], path)
            self.assertFalse(decision["error"])

    def test_scenario_3b_interview_best_effort_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, status="best-effort")
            decision = hoc.routing_decision(root, from_sot_arg=None)
            self.assertEqual(decision["path"], "interview")
            self.assertEqual(decision["status"], "best-effort")
            self.assertFalse(decision["error"])

    def test_scenario_3c_interview_user_acknowledged_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, status="user-acknowledged")
            decision = hoc.routing_decision(root, from_sot_arg=None)
            self.assertEqual(decision["path"], "interview")
            self.assertEqual(decision["status"], "user-acknowledged")
            self.assertFalse(decision["error"])

    # --- Scenario 4: missing/held interview fails closed ---
    def test_scenario_4a_missing_interview_handoff_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            decision = hoc.routing_decision(root, from_sot_arg=None)
            self.assertEqual(decision["path"], "error")
            self.assertEqual(decision["status"], "missing")
            self.assertIn("/dev-kit:interview", decision["reason"])

    def test_scenario_4b_held_interview_handoff_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, status="held")
            decision = hoc.routing_decision(root, from_sot_arg=None)
            self.assertEqual(decision["path"], "error")
            self.assertEqual(decision["status"], "held")
            self.assertIn("/dev-kit:interview", decision["reason"])

    # --- Bonus: --skip-interview backward compat ---
    def test_skip_interview_short_circuits_to_skip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, status="held")
            decision = hoc.routing_decision(root, from_sot_arg=None, skip_interview=True)
            self.assertEqual(decision["path"], "skip")
            self.assertEqual(decision["status"], "skipped")
            self.assertFalse(decision["error"])

    def test_skip_interview_overrides_routing_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Missing interview AND a SOT present — --skip-interview still wins.
            _write_sot_handoff(root, status="locked")
            decision = hoc.routing_decision(root, from_sot_arg=None, skip_interview=True)
            self.assertEqual(decision["path"], "skip")

    # --- from_sot with interview present: --from-sot wins (explicit override) ---
    def test_from_sot_arg_overrides_interview_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, status="ok")
            sot_path = _write_sot_handoff(root, session_id="override", status="locked")
            decision = hoc.routing_decision(root, from_sot_arg=str(sot_path))
            self.assertEqual(decision["path"], "from_sot")
            self.assertEqual(decision["handoff_path"], sot_path)

    def test_from_sot_arg_with_missing_file_is_routing_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            decision = hoc.routing_decision(
                root,
                from_sot_arg=str(root / ".dev-kit/hand-off/sot-harness-ghost.md"),
            )
            self.assertEqual(decision["path"], "error")
            self.assertEqual(decision["status"], "misrouted")
            self.assertIn("not found", decision["reason"].lower())


# ----- filename glob contract (the discriminator's first line of defence) -----


class TestFilenameDiscriminator(unittest.TestCase):
    """The plan skill must NEVER pick up a SOT file as an interview handoff.

    These tests pin the filename shape that the discovery helpers use,
    so a future rename cannot silently re-introduce the bug.
    """

    def test_sot_filename_is_not_in_interview_glob(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_sot_handoff(root, session_id="x")
            self.assertIsNone(hoc.discover_interview_handoff(root))

    def test_interview_filename_is_not_in_sot_glob(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_interview_handoff(root, session_id="x")
            self.assertIsNone(hoc.discover_sot_handoff(root))

    def test_unrelated_md_is_ignored_by_both(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hand_off = root / ".dev-kit" / "hand-off"
            hand_off.mkdir(parents=True, exist_ok=True)
            (hand_off / "plan→build.md").write_text("# plan hand-off\n")
            self.assertIsNone(hoc.discover_interview_handoff(root))
            self.assertIsNone(hoc.discover_sot_handoff(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
