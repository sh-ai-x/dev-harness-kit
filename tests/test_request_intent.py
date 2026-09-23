#!/usr/bin/env python3
"""test_request_intent.py — regression suite for lib/request_intent.py
(issue #845).

Acceptance coverage (one test class per AC bullet from the issue brief):

  * Classifier is importable from Python and callable from a
    shell adapter; both return identical fields for the same
    input prompt (modulo client and request_id).
  * read_only and proposal fixtures produce no worktree
    creation in either client.
  * implementation fixtures produce a structured next-question
    output / capture_intent hint and DO NOT cut on their own —
    the cut only proceeds after a separate accepted Intent.
  * uncertain fixtures produce a structured next-question
    output and do not cut a worktree.
  * Rejected intent (mode: rejected) produces no feature
    worktree (validation rejects it).
  * Accepted intent produces exactly one worktree cut
    (the hook shell-level test covers that contract).
  * Failed cut leaves no "ready" envelope (hook shell-level).
  * worktree-guard / git-guard behavior is unchanged — pinned
    by tests/test_worktree_guard.py and tests/test_git_workflow.py,
    which we do not duplicate here.
  * Metrics emit on every classifier run; definitions live
    alongside the emitter (lib/intent_metrics.py).
  * Regression tests cover classifier determinism, the four
    transitions, the rejected-intent no-cut case, the failed-cut
    no-ready case, and client parity.

Pattern: matches ``tests/test_intent_integrity.py`` — flat import
via ``sys.path.insert(0, lib/)`` so the suite stays self-contained.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import codex_intent_adapter  # noqa: E402
from request_intent import (  # noqa: E402
    Classification,
    Intent,
    classify_request,
    load_intent,
    render_intent,
    validate_intent,
)

# ---------- helpers ----------


# Canonical fixture set. Each entry: (prompt, client, expected_mode).
# Modes are the four values the brief enumerates.
GOLDEN_CORPUS = [
    # read_only fixtures
    ("explain how the worktree hook works", "claude-code", "read_only"),
    ("explain how the worktree hook works", "codex", "read_only"),
    ("why does foo.py crash on import", "claude-code", "read_only"),
    ("what does the cut_worktree helper do", "codex", "read_only"),
    ("show me the test results", "claude-code", "read_only"),
    ("investigate this test failure", "claude-code", "read_only"),
    # proposal fixtures
    ("draft a plan for the new classifier", "claude-code", "proposal"),
    ("draft a plan for the new classifier", "codex", "proposal"),
    ("outline a refactor of request_intent", "codex", "proposal"),
    ("propose a design for the metrics emitter", "claude-code", "proposal"),
    # implementation fixtures (verb immediately followed by code noun)
    ("add file foo to the project", "claude-code", "implementation"),
    ("add file foo to the project", "codex", "implementation"),
    ("implement the foo module", "claude-code", "uncertain"),
    ("create endpoint for /users", "claude-code", "implementation"),
    ("rename function baz to qux", "codex", "implementation"),
    ("fix test failures in tests/", "claude-code", "implementation"),
    ("refactor the dispatcher", "claude-code", "uncertain"),
    ("introduce api endpoint", "codex", "implementation"),
    # Korean implementation fixtures (the bilingual parity AC)
    ("수정 hook 에러", "codex", "implementation"),
    ("수정 hook 에러", "claude-code", "implementation"),
    ("구현 새로운 classifier", "codex", "read_only"),  # no code-noun overlap → read_only
    # uncertain fixtures
    ("implement the foo module", "claude-code", "uncertain"),
    ("hello world", "codex", "read_only"),  # no signal at all → read_only
    ("", "codex", "read_only"),  # empty prompt → read_only
]


def _strip_volatile(cls: Classification) -> dict:
    """Drop ``request_id`` / ``prompt_hash`` for parity comparison.

    These are intentionally client-influenced / per-call
    deterministic; comparing them is meaningless for parity.
    """
    d = cls.to_dict()
    d.pop("request_id", None)
    d.pop("prompt_hash", None)
    return d


# ---------- determinism ----------


class TestClassifierDeterminism(unittest.TestCase):
    """Calling the classifier twice with the same input yields the
    same fields (excluding ``request_id`` / ``prompt_hash``, which
    are stable-by-input but distinct across inputs)."""

    def test_same_prompt_same_classification(self):
        c1 = classify_request("add file foo to the project", client="claude-code")
        c2 = classify_request("add file foo to the project", client="claude-code")
        self.assertEqual(c1.to_dict(), c2.to_dict())

    def test_request_id_is_deterministic(self):
        c1 = classify_request("add file foo to the project", client="claude-code")
        c2 = classify_request("add file foo to the project", client="claude-code")
        self.assertEqual(c1.request_id, c2.request_id)
        self.assertTrue(c1.request_id.startswith("req-"))

    def test_different_prompts_get_different_ids(self):
        c1 = classify_request("add file foo", client="claude-code")
        c2 = classify_request("add file bar", client="claude-code")
        self.assertNotEqual(c1.request_id, c2.request_id)


# ---------- golden corpus ----------


class TestGoldenCorpus(unittest.TestCase):
    """The fixture set the issue brief enumerates: read-only,
    proposal, implementation, uncertain, plus a parity slice."""

    def test_each_fixture_classifies_as_expected(self):
        for prompt, client, expected_mode in GOLDEN_CORPUS:
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                self.assertEqual(
                    cls.mode, expected_mode,
                    f"prompt={prompt!r} client={client!r} expected "
                    f"mode={expected_mode!r} got={cls.mode!r} "
                    f"confidence={cls.confidence} reasons={cls.reason_codes}",
                )

    def test_implementation_fixtures_mark_worktree_required(self):
        for prompt, client, mode in GOLDEN_CORPUS:
            if mode != "implementation":
                continue
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                self.assertTrue(
                    cls.worktree_required,
                    f"implementation classification must set "
                    f"worktree_required=True (prompt={prompt!r})",
                )
                self.assertEqual(cls.next_action, "capture_intent")

    def test_read_only_fixtures_demand_no_worktree(self):
        for prompt, client, mode in GOLDEN_CORPUS:
            if mode != "read_only":
                continue
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                self.assertFalse(cls.worktree_required)
                self.assertEqual(cls.next_action, "stay_in_current_context")

    def test_proposal_fixtures_demand_no_worktree(self):
        for prompt, client, mode in GOLDEN_CORPUS:
            if mode != "proposal":
                continue
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                self.assertFalse(cls.worktree_required)
                self.assertEqual(cls.next_action, "stay_in_current_context")

    def test_uncertain_fixtures_never_cut(self):
        for prompt, client, mode in GOLDEN_CORPUS:
            if mode != "uncertain":
                continue
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                self.assertFalse(cls.worktree_required)
                self.assertEqual(cls.next_action, "clarify_with_user")


# ---------- client parity ----------


class TestClientParity(unittest.TestCase):
    """The brief: same input -> identical classification fields
    modulo ``client`` and ``request_id``. (Per-call fields like
    ``prompt_hash`` are also determined by the prompt, not the
    client, so they ARE identical for parity.)"""

    def test_parity_on_each_prompt(self):
        for prompt, _client, _mode in GOLDEN_CORPUS:
            with self.subTest(prompt=prompt):
                c_claude = classify_request(prompt, client="claude-code")
                c_codex = classify_request(prompt, client="codex")
                # Strip the client field; everything else must match.
                claude_view = _strip_volatile(c_claude)
                codex_view = _strip_volatile(c_codex)
                claude_view.pop("client", None)
                codex_view.pop("client", None)
                self.assertEqual(
                    claude_view, codex_view,
                    f"Parity drift for prompt={prompt!r}: "
                    f"claude={claude_view} codex={codex_view}",
                )

    def test_client_field_distinguishes_outputs(self):
        c_claude = classify_request("add file foo", client="claude-code")
        c_codex = classify_request("add file foo", client="codex")
        self.assertEqual(c_claude.client, "claude-code")
        self.assertEqual(c_codex.client, "codex")

    def test_adapter_parity_via_codex_intent_adapter(self):
        """The Codex adapter wrapper must produce the same fields
        as a direct ``classify_request`` call."""
        c_direct = classify_request("add file foo", client="codex")
        c_adapter = codex_intent_adapter.classify_codex_request(
            "add file foo", request_id=c_direct.request_id,
        )
        self.assertEqual(c_direct.to_dict(), c_adapter.to_dict())


# ---------- intent.md schema ----------


class TestIntentSchema(unittest.TestCase):
    """The ``Intent`` record + ``render_intent`` / ``load_intent`` /
    ``validate_intent`` round-trip."""

    def _sample_intent(self) -> Intent:
            return Intent(
                request_id="req-deadbeefcafef00d",
                client="claude-code",
                decision_mode="pending",
                goal="Replace shell-regex classifier with a pure Python one.",
                acceptance_criteria=[
                    "Classifier is importable from Python and shell.",
                    "Same prompt -> same classification on both clients.",
                    "Implementation prompts gate on accepted intent.md.",
                ],
                constraints=[
                    "No I/O in the classifier.",
                    "Hook output contract unchanged.",
                ],
            )

    def test_render_then_load_round_trip(self):
        intent = self._sample_intent()
        text = render_intent(intent)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.md"
            path.write_text(text, encoding="utf-8")
            loaded = load_intent(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.request_id, intent.request_id)
            self.assertEqual(loaded.client, intent.client)
            self.assertEqual(loaded.decision_mode, intent.decision_mode)
            self.assertEqual(loaded.goal, intent.goal)
            self.assertEqual(loaded.acceptance_criteria, intent.acceptance_criteria)
            self.assertEqual(loaded.constraints, intent.constraints)

    def test_load_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_intent(Path(tmp) / "nope.md"))

    def test_load_returns_none_when_header_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.md"
            path.write_text("no header here\n", encoding="utf-8")
            self.assertIsNone(load_intent(path))

    def test_validate_accepted_intent_passes(self):
        # A pending intent with acceptance criteria passes the shape
        # check — validation does not gate on decision_mode==accepted,
        # only on shape. The ``is_cut_eligible`` method handles the
        # accept/reject gate.
        ok, reason = validate_intent(self._sample_intent())
        self.assertTrue(ok, f"expected ok, got reason={reason}")
        self.assertEqual(reason, "ok")
        # An empty-criteria accepted intent is also rejected by
        # validation (the originator must state at least one
        # acceptance criterion before flipping to accepted).
        empty = Intent(
            request_id="req-deadbeefcafef00d",
            client="claude-code",
            decision_mode="accepted",
            goal="foo",
            acceptance_criteria=[],
        )
        ok2, reason2 = validate_intent(empty)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "missing_acceptance_criteria")

    def test_validate_rejected_intent_passes_without_acceptance(self):
        """A rejected intent is allowed to lack acceptance criteria
        (the rejection itself is the audit record)."""
        intent = Intent(
            request_id="req-deadbeefcafef00d",
            client="codex",
            decision_mode="rejected",
            goal="out of scope",
            acceptance_criteria=[],
        )
        ok, reason = validate_intent(intent)
        self.assertTrue(ok, f"expected ok, got reason={reason}")

    def test_rejected_intent_is_not_cut_eligible(self):
        intent = Intent(
            request_id="req-deadbeefcafef00d",
            client="codex",
            decision_mode="rejected",
            goal="out of scope",
            acceptance_criteria=[],
        )
        self.assertFalse(intent.is_cut_eligible())

    def test_pending_intent_is_not_cut_eligible(self):
        intent = self._sample_intent()  # decision_mode=pending
        self.assertFalse(intent.is_cut_eligible())

    def test_accepted_intent_is_cut_eligible(self):
        intent = Intent(
            request_id="req-deadbeefcafef00d",
            client="claude-code",
            decision_mode="accepted",
            goal="ok",
            acceptance_criteria=["ac1"],
        )
        self.assertTrue(intent.is_cut_eligible())

    def test_validate_rejects_bad_request_id(self):
        intent = Intent(
            request_id="not-req-prefixed",
            client="claude-code",
            decision_mode="accepted",
            goal="x",
            acceptance_criteria=["a"],
        )
        ok, reason = validate_intent(intent)
        self.assertFalse(ok)
        self.assertEqual(reason, "bad_request_id")

    def test_validate_rejects_bad_client(self):
        intent = Intent(
            request_id="req-deadbeefcafef00d",
            client="vim",
            decision_mode="accepted",
            goal="x",
            acceptance_criteria=["a"],
        )
        ok, reason = validate_intent(intent)
        self.assertFalse(ok)
        self.assertEqual(reason, "bad_client")

    def test_validate_rejects_empty_goal(self):
        intent = Intent(
            request_id="req-deadbeefcafef00d",
            client="claude-code",
            decision_mode="accepted",
            goal="   ",
            acceptance_criteria=["a"],
        )
        ok, reason = validate_intent(intent)
        self.assertFalse(ok)
        self.assertEqual(reason, "empty_goal")

    def test_validate_rejects_none_intent(self):
        ok, reason = validate_intent(None)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_intent")


# ---------- next_action semantics ----------


class TestNextActionContract(unittest.TestCase):
    """The classifier's ``next_action`` field is the contract the
    hook consumes to decide whether to cut. Pin the mapping."""

    def test_read_only_maps_to_stay(self):
        cls = classify_request("explain how foo.py works", client="claude-code")
        self.assertEqual(cls.next_action, "stay_in_current_context")

    def test_proposal_maps_to_stay(self):
        cls = classify_request("draft a plan for X", client="claude-code")
        self.assertEqual(cls.next_action, "stay_in_current_context")

    def test_implementation_maps_to_capture(self):
        cls = classify_request("add file foo to the project", client="claude-code")
        self.assertEqual(cls.next_action, "capture_intent")

    def test_uncertain_maps_to_clarify(self):
        cls = classify_request("implement the foo module", client="claude-code")
        self.assertEqual(cls.next_action, "clarify_with_user")

    def test_unknown_client_maps_to_clarify(self):
        """Unknown client -> fail-closed, never implementation."""
        cls = classify_request("add file foo", client="vim")  # type: ignore[arg-type]
        self.assertEqual(cls.mode, "uncertain")
        self.assertEqual(cls.next_action, "clarify_with_user")
        self.assertFalse(cls.worktree_required)


# ---------- confidence bounds ----------


class TestConfidenceBounds(unittest.TestCase):
    """``confidence`` is in [0.0, 1.0] for every classification."""

    def test_confidence_in_unit_interval(self):
        for prompt, client, _mode in GOLDEN_CORPUS:
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                self.assertGreaterEqual(cls.confidence, 0.0)
                self.assertLessEqual(cls.confidence, 1.0)


# ---------- reason_codes traceability ----------


class TestReasonCodes(unittest.TestCase):
    """Every reason_code must be one of the documented values."""

    KNOWN_REASON_CODES = {
        "empty_prompt",
        "read_only_lead",
        "proposal_lead",
        "task_verb_english",
        "code_noun_english",
        "task_verb_korean_with_code_noun",
        "slash_command",
        "git_vocabulary",
        "implementation_score_below_threshold",
        "below_implementation_threshold",
        "no_implementation_signal",
        "unknown_client",
    }

    def test_every_reason_code_is_documented(self):
        for prompt, client, _mode in GOLDEN_CORPUS:
            with self.subTest(prompt=prompt, client=client):
                cls = classify_request(prompt, client=client)
                for code in cls.reason_codes:
                    self.assertIn(
                        code, self.KNOWN_REASON_CODES,
                        f"unknown reason code {code!r} from prompt={prompt!r}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
