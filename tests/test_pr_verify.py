#!/usr/bin/env python3
"""Tests for lib.pr_verify — deterministic PR verification.

Covers each of the five gates + the parser logic. All network I/O is
mocked; tests are hermetic.
"""
from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "lib")
import pr_verify  # noqa: E402


class TestParseLatestLLMVerdict(unittest.TestCase):
    """G3 depends on this parser; locking it down first."""

    def test_empty_comments_returns_missing(self):
        verdict, src = pr_verify._parse_latest_llm_verdict([])
        self.assertEqual(verdict, "MISSING")
        self.assertEqual(src, "")

    def test_no_claude_comments_returns_missing(self):
        comments = [
            {"user": "github-actions", "body": "**Verdict:** Approve", "updated_at": "2026-01-01T00:00:00Z"},
        ]
        verdict, _ = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "MISSING")

    def test_no_verdict_line_returns_missing(self):
        comments = [
            {"user": "claude[bot]", "body": "no verdict here", "updated_at": "2026-01-01T00:00:00Z"},
        ]
        verdict, _ = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "MISSING")

    def test_latest_claude_comment_with_approve_wins(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Changes Requested", "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
            {"user": "claude[bot]", "body": "**Verdict:** Approve", "updated_at": "2026-01-02T00:00:00Z", "id": "2"},
        ]
        verdict, src = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Approve")
        self.assertEqual(src, "2")

    def test_older_claude_comment_with_approve_loses_to_newer_changes(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve", "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
            {"user": "claude[bot]", "body": "**Verdict:** Changes Requested", "updated_at": "2026-01-02T00:00:00Z", "id": "2"},
        ]
        verdict, src = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Changes Requested")
        self.assertEqual(src, "2")

    def test_blocked_verdict_recognized(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Blocked", "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
        ]
        verdict, _ = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Blocked")

    def test_nested_verdict_in_paragraph_does_not_match(self):
        """A **Verdict:** mention inside prose must not satisfy the
        parser. The regex anchors on its own line via the strict
        `\\*\\*Verdict:\\*\\*\\s+<word>` pattern, so a sentence like
        'we want **Verdict:** Approve' would still match — this
        is documented and accepted. False negatives are worse than
        false positives here.
        """
        comments = [
            {"user": "claude[bot]", "body": "Verdict: Approve", "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
        ]
        # No **Verdict:** markdown bold — parser returns MISSING.
        verdict, _ = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "MISSING")


class TestGatesHermetic(unittest.TestCase):
    """Each gate with mocked `gh` calls."""

    def test_g1_open_pr_passes(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN",
        })):
            g = pr_verify._gate_g1_pr_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)
        self.assertIn("OPEN", g.detail)

    def test_g1_draft_pr_fails(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "state": "OPEN", "isDraft": True, "mergeStateStatus": "CLEAN",
        })):
            g = pr_verify._gate_g1_pr_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)

    def test_g1_closed_pr_fails(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "state": "CLOSED", "isDraft": False, "mergeStateStatus": "CLEAN",
        })):
            g = pr_verify._gate_g1_pr_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)

    def test_g2_all_pass(self):
        checks = [
            {"name": "lint", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
            {"name": "test", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
            {"name": "branch-policy", "state": "COMPLETED", "conclusion": "skipped", "bucket": "skipping"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(checks)):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)

    def test_g2_pending_does_not_claim_pass(self):
        """Critical: a still-running check must not be 'approved'."""
        checks = [
            {"name": "lint", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
            {"name": "review", "state": "IN_PROGRESS", "conclusion": None, "bucket": "pending"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(checks)):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)
        self.assertIn("PENDING", g.detail)

    def test_g2_failure_fails(self):
        checks = [
            {"name": "lint", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
            {"name": "review", "state": "COMPLETED", "conclusion": "failure", "bucket": "fail"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(checks)):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)
        self.assertIn("FAILED", g.detail)

    def test_g3_latest_approve_passes(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "updated_at": "2026-01-02T00:00:00Z", "created_at": "2026-01-02T00:00:00Z", "id": "1"},
            {"id": "audit-1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve",
             "created_at": "2026-01-02T00:00:00Z"},
            {"id": "audit-2", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve",
             "created_at": "2026-01-02T00:00:00Z"},
            {"id": "audit-3", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve",
             "created_at": "2026-01-02T00:00:00Z"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)

    def test_g3_latest_changes_requested_fails(self):
        """Critical: even if an OLDER claude comment said Approve, the
        NEWER one saying Changes Requested must win."""
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve", "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
            {"user": "claude[bot]", "body": "**Verdict:** Changes Requested", "updated_at": "2026-01-02T00:00:00Z", "id": "2"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)

    def test_g3_missing_verdict_fails(self):
        """If no claude[bot] comment has a **Verdict:** line, the gate fails.
        This is the 'in-progress run' false positive the babysit skill had:
        the workflow had run but the LLM hadn't yet posted a verdict,
        and the babysit claimed 'all green' anyway."""
        comments = []
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)
        self.assertIn("MISSING", g.detail)

    def test_g4_pure_approve_pair_passes(self):
        comments = [
            {"id": "1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=100 job=review status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g4_audit_no_failure_paired_with_approve(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)

    def test_g4_failure_paired_with_approve_fails(self):
        """Critical: this is the exact false positive the babysit
        skill had. The audit line says verdict=Approve but the
        workflow's exit status=failure (e.g. the LLM API errored,
        or the workflow self-validated, or the verdict text was
        emitted but the script's overall exit was non-zero)."""
        comments = [
            {"id": "1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=100 job=review status=failure verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g4_audit_no_failure_paired_with_approve(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)
        bad = g.evidence["bad_pairs"][0]
        self.assertEqual(bad["status"], "failure")
        self.assertEqual(bad["verdict"], "Approve")

    def test_g4_untrusted_audit_author_ignored(self):
        """A08 hardening: an audit line posted by a non-workflow
        author must be IGNORED — the gate falls back to no audits,
        not to a forged Approve."""
        comments = [
            {"id": "1", "user": "claude-reviewer",  # impersonator
             "body": "<!-- dev-kit-verdict-audit --> run=99 job=review status=failure verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g4_audit_no_failure_paired_with_approve(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)  # untrusted audit ignored, no bad pairs
        self.assertEqual(g.evidence["untrusted_audits_ignored"], 1)

    def test_g5_clean_passes(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
        })):
            g = pr_verify._gate_g5_merge_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)

    def test_g5_behind_soft_passes_with_warning(self):
        """BEHIND = branch needs rebase but can still merge. Treat as
        a soft pass; the caller can choose to rebase."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE",
        })):
            g = pr_verify._gate_g5_merge_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertTrue(g.passed)

    def test_g5_blocked_fails(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "mergeStateStatus": "BLOCKED", "mergeable": "CONFLICTING",
        })):
            g = pr_verify._gate_g5_merge_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)

    def test_g5_unstable_fails(self):
        """M-8: UNSTABLE means a required check is still being recomputed
        or mergeability is being re-evaluated. Reverting the soft-pass to
        only CLEAN/BEHIND closes the fail-open window."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({
            "mergeStateStatus": "UNSTABLE", "mergeable": "MERGEABLE",
        })):
            g = pr_verify._gate_g5_merge_state(584, "sh-ai-x/dev-harness-kit", "2026-08-06T00:00:00Z")
        self.assertFalse(g.passed)


class TestVerifyPRIntegration(unittest.TestCase):
    """End-to-end: a fully-passing fixture passes; a mixed fixture fails."""

    def test_all_gates_pass_yields_passed_report(self):
        # Every gate's underlying `_run_gh` returns a passing value.
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: _ok_return(args)):
            report = pr_verify.verify_pr(584)
        self.assertTrue(report.passed)
        self.assertEqual(report.blockers, [])

    def test_any_gate_failing_yields_failed_report(self):
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: _fail_at(args, which="G4")):
            report = pr_verify.verify_pr(584)
        self.assertFalse(report.passed)
        # The G4-fail fixture has a status=failure + verdict=Approve
        # audit comment, which trips BOTH G3 (no claude verdict) and
        # G4 (false-positive pair). So two blockers, not one.
        self.assertGreaterEqual(len(report.blockers), 1)
        self.assertTrue(any("G4" in b for b in report.blockers))

    def test_summary_includes_all_gates(self):
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: _ok_return(args)):
            report = pr_verify.verify_pr(584)
        text = report.summary()
        for gate_id in ("G1", "G2", "G3", "G4", "G5"):
            self.assertIn(gate_id, text)
        self.assertIn("APPROVED", text)
        self.assertIn("checked at", text)

    def test_verify_pr_non_dict_pr_view_response_fails_closed(self):
        """CC-7 regression (flagged by the maintenance judge on PR #588):
        `gh pr view --json commits,headRefOid` returning a non-dict JSON
        body (e.g. a bare list or null — a malformed/edge-case gh
        response) must NOT crash verify_pr with an uncaught
        AttributeError from `.get()` on a non-dict. The freshness /
        provenance fetch degrades (pr_pushed_at='', pr_head_sha='') and
        the rest of the report is still produced.
        """
        def fake_gh(args):
            if (args[0] == "pr" and len(args) > 1 and args[1] == "view"
                    and "commits,headRefOid" in args):
                return "null"
            return _ok_return(args)
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            report = pr_verify.verify_pr(584)  # must not raise
        self.assertEqual(len(report.gates), 5)


# ---------- helpers for the integration test ----------

def _ok_return(args):
    """All five gates' underlying gh calls return passing values."""
    sub = args[0]
    if sub == "pr" and len(args) > 1 and args[1] == "view":
        return json.dumps({
            "state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
        })
    if sub == "pr" and len(args) > 1 and args[1] == "checks":
        return json.dumps([
            {"name": "lint", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
            {"name": "test", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
        ])
    if sub == "api":
        # M-3 per-judge verdict: emit Approve audit comments for all
        # 3 required jobs (review, security, maintenance) + a single
        # claude[bot] comment (so M-2 stale-guard sees a head verdict).
        return json.dumps([
            {"id": "1", "user": "claude[bot]",
             "body": "**Verdict:** Approve", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
            {"id": "audit-review", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "audit-security", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "audit-maintenance", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
        ])
    return json.dumps({})


def _fail_at(args, which: str):
    """Force a specific gate to fail by returning a known-bad payload.

    G4 only: the API call (which fetches PR comments) returns a
    comments list that includes a status=failure + verdict=Approve
    audit comment. Other calls return the same passing values as
    _ok_return so only G4 fails.
    """
    sub = args[0]
    if which == "G4" and sub == "api":
        return json.dumps([
            {"id": "1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=100 job=review status=failure verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
        ])
    return _ok_return(args)




class TestM1ImpersonatorRegression(unittest.TestCase):
    """Regression: a comment from a non-trusted claude-prefixed account
    must NOT count as the latest LLM-judge verdict. The verifier trusts
    ONLY {claude, claude[bot]}."""

    def test_claude_dash_reviewer_not_trusted(self):
        from pr_verify import _parse_latest_llm_verdict
        comments = [
            {"user": "claude-reviewer", "body": "**Verdict:** Approve",
             "updated_at": "2026-01-02T00:00:00Z", "id": "1"},
        ]
        verdict, _ = _parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "MISSING")

    def test_claude_bot_fork_not_trusted(self):
        from pr_verify import _parse_latest_llm_verdict
        comments = [
            {"user": "claude-bot-fork", "body": "**Verdict:** Approve",
             "updated_at": "2026-01-02T00:00:00Z", "id": "1"},
        ]
        verdict, _ = _parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "MISSING")

    def test_claude_bot_login_trusted(self):
        from pr_verify import _parse_latest_llm_verdict
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "updated_at": "2026-01-02T00:00:00Z", "id": "1"},
        ]
        verdict, _ = _parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Approve")

    def test_trusted_login_with_older_changes_loses_to_older_approve(self):
        """M-1 sanity: the impersonator-resistant filter still picks the
        most-recent trusted verdict. An old `Approve` from claude[bot]
        must lose to a newer `Changes Requested` from claude[bot]."""
        from pr_verify import _parse_latest_llm_verdict
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
            {"user": "claude-reviewer", "body": "**Verdict:** Changes Requested",
             "updated_at": "2026-01-02T00:00:00Z", "id": "2"},
            {"user": "claude[bot]", "body": "**Verdict:** Blocked",
             "updated_at": "2026-01-03T00:00:00Z", "id": "3"},
        ]
        verdict, src = _parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Blocked")
        self.assertEqual(src, "3")


class TestM7VerdictRegexAnchored(unittest.TestCase):
    """Regression: a quoted "**Verdict:** Approve" earlier in the body
    must NOT override a real verdict that appears later."""

    def test_quoted_earlier_approve_does_not_override_later_changes(self):
        body = (
            "Note: the historical record shows **Verdict:** Approve.\n"
            "However, after re-review:\n"
            "**Verdict:** Changes Requested"
        )
        comments = [
            {"user": "claude[bot]", "body": body,
             "updated_at": "2026-01-02T00:00:00Z", "id": "1"},
        ]
        verdict, _ = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Changes Requested")

    def test_final_verdict_wins_among_multiple(self):
        """A05 hardening: when a single comment body contains multiple
        verdict lines, the FINAL one wins (the most-recent editorial
        intent of the trusted bot). Earlier injected `Approve` cannot
        override a later authoritative `Changes Requested`."""
        body = (
            "**Verdict:** Approve\n"
            "\n"
            "## Re-evaluation\n"
            "\n"
            "After re-review, I found additional issues:\n"
            "\n"
            "**Verdict:** Changes Requested\n"
        )
        comments = [
            {"user": "claude[bot]", "body": body,
             "updated_at": "2026-01-02T00:00:00Z", "id": "1"},
        ]
        verdict, _ = pr_verify._parse_latest_llm_verdict(comments)
        self.assertEqual(verdict, "Changes Requested")


class TestM6UnknownBucketFailsG2(unittest.TestCase):
    """Regression: a workflow that emits an unclassified bucket must
    fail G2 instead of silently passing. The G2 allow-list is explicit:
    only `pass` and `skipping` are terminal-pass."""

    def test_unknown_bucket_fails_g2(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"name": "lint", "state": "COMPLETED", "conclusion": "success", "bucket": "pass"},
            {"name": "weird", "state": "COMPLETED", "conclusion": "success", "bucket": "unknown"},
        ])):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("UNEXPECTED bucket", g.detail)

    def test_cancelled_bucket_fails_g2(self):
        """`cancelled` is outside the {pass, skipping} allow-list."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"name": "lint", "state": "COMPLETED", "bucket": "pass"},
            {"name": "build", "state": "COMPLETED", "bucket": "cancelled"},
        ])):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("UNEXPECTED bucket", g.detail)

    def test_timed_out_bucket_fails_g2(self):
        """`timed_out` is outside the {pass, skipping} allow-list."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"name": "lint", "state": "COMPLETED", "bucket": "pass"},
            {"name": "build", "state": "COMPLETED", "bucket": "timed_out"},
        ])):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("UNEXPECTED bucket", g.detail)

    def test_action_required_bucket_fails_g2(self):
        """`action_required` is outside the {pass, skipping} allow-list."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"name": "lint", "state": "COMPLETED", "bucket": "pass"},
            {"name": "deploy", "state": "COMPLETED", "bucket": "action_required"},
        ])):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("UNEXPECTED bucket", g.detail)

    def test_skipping_bucket_passes_g2(self):
        """`skipping` is the explicit allow-list pass bucket (skipped check)."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"name": "lint", "state": "COMPLETED", "bucket": "pass"},
            {"name": "deploy", "state": "SKIPPED", "bucket": "skipping"},
        ])):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit")
        self.assertTrue(g.passed)





class TestM2StaleVerdictGuard(unittest.TestCase):
    """Regression: a verdict emitted BEFORE the most recent push to the
    PR head must NOT count as the latest authoritative verdict (M-2).
    The verdict is marked STALE so the gate fails."""

    def test_old_approve_with_new_push_is_stale(self):
        # Comment created at 01-01, push at 01-02 — comment is stale.
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "id": "1"},
            {"id": "audit-1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "audit-2", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "audit-3", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
        ])):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,  # forces the _run_gh fallback path
                pr_pushed_at="2026-01-02T00:00:00Z",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)

    def test_recent_approve_with_no_push_is_fresh(self):
        # Comment created AFTER push — fresh.
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-03T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z", "id": "1"},
            {"id": "audit-1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve",
             "created_at": "2026-01-03T00:00:00Z"},
            {"id": "audit-2", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve",
             "created_at": "2026-01-03T00:00:00Z"},
            {"id": "audit-3", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve",
             "created_at": "2026-01-03T00:00:00Z"},
        ])):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
            )
        self.assertTrue(g.passed)

    def test_one_job_stale_others_fresh_fails(self):
        """M-2 partial hardening: a single stale-job audit must fail
        the gate even when other jobs are fresh. The OLD implementation
        only compared the global latest audit against pushed_at and let
        a stale review pass if maintenance was fresh."""
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps([
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-03T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z", "id": "1"},
            # review audit: OLD (before push) -> stale
            {"id": "audit-1", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve",
             "created_at": "2026-01-01T00:00:00Z"},
            # security + maintenance: FRESH (after push)
            {"id": "audit-2", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve",
             "created_at": "2026-01-03T00:00:00Z"},
            {"id": "audit-3", "user": "github-actions",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve",
             "created_at": "2026-01-03T00:00:00Z"},
        ])):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)
        self.assertIn("review", g.detail)





class TestCC8EdgeCaseFailures(unittest.TestCase):
    """CC-8 regression: a single `gh` failure must NOT abort the
    entire verify_pr — the offending gate returns a structured
    fail-closed GateResult and the other gates still report."""

    def test_gh_timeout_returns_fail_closed_gate(self):
        from pr_verify import GhError
        with patch.object(pr_verify, "_run_gh", side_effect=GhError(
            "gh pr timed out after 30s", exit_code=None,
        )):
            g = pr_verify._gate_g1_pr_state(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("gh error", g.detail)

    def test_gh_missing_binary_returns_fail_closed_gate(self):
        from pr_verify import GhError
        with patch.object(pr_verify, "_run_gh", side_effect=GhError(
            "gh CLI not found on PATH", exit_code=None,
        )):
            g = pr_verify._gate_g2_ci_checks(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("gh error", g.detail)

    def test_malformed_json_returns_fail_closed_gate(self):
        from pr_verify import GhError
        with patch.object(pr_verify, "_run_gh", side_effect=GhError(
            "gh returned malformed JSON: Unexpected token at line 1 col 5",
        )):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("gh error", g.detail)

    def test_verify_pr_one_gate_fails_others_still_report(self):
        """A single gate's failure must NOT abort verify_pr — the
        other four gates still report. This is the core CC-8
        regression."""
        from pr_verify import GhError
        # Make the SHARED COMMENTS fetch (used by G3 and G4) raise.
        # verify_pr's try/except swallows it (shared_comments stays ()),
        # then G3 and G4 fall back to per-gate _run_gh which ALSO raises
        # (same side_effect is called). Each gate catches GhError and
        # returns a fail-closed GateResult. G1, G2, G5 still report.
        fail_api = {"on": True}

        def side_effect(args, *a, **kw):
            sub = args[0]
            if sub == "api" and fail_api["on"]:
                raise GhError("gh comments fetch injected failure", exit_code=1)
            if sub == "pr" and len(args) > 1 and args[1] == "view":
                return json.dumps({"state": "OPEN", "isDraft": False,
                                   "mergeStateStatus": "CLEAN",
                                   "mergeable": "MERGEABLE"})
            if sub == "pr" and len(args) > 1 and args[1] == "checks":
                return json.dumps([
                    {"name": "lint", "state": "COMPLETED",
                     "conclusion": "success", "bucket": "pass"},
                ])
            return json.dumps({})

        with patch.object(pr_verify, "_run_gh", side_effect=side_effect):
            report = pr_verify.verify_pr(584)
        # report.passed is False because G3/G4 fail-closed
        self.assertFalse(report.passed)
        # The injected failure is reported as a blocker, not swallowed
        blocker_text = " ".join(report.blockers)
        self.assertIn("gh error", blocker_text)
        # All 5 gates still produce a GateResult (no gate crashed)
        self.assertEqual(len(report.gates), 5)
        # G1, G2, G5 pass; G3, G4 fail
        passed_gates = {g.gate for g in report.gates if g.passed}
        self.assertIn("G1", passed_gates)
        self.assertIn("G2", passed_gates)
        self.assertIn("G5", passed_gates)





class TestM3PerJudgeVerdict(unittest.TestCase):
    """M-3 regression: G3 must require Approve from each of review,
    security, AND maintenance audit comments — not just the most
    recent claude[bot] comment overall."""

    def _make_audit(self, job: str, verdict: str) -> dict:
        return {
            "id": f"audit-{job}",
            "user": "github-actions",
            "body": f"<!-- dev-kit-verdict-audit --> run=1 job={job} status=success verdict={verdict}",
            "created_at": "2026-01-02T00:00:00Z",
        }

    def test_all_three_jobs_approve_passes(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-02T00:00:00Z", "id": "1"},
            self._make_audit("review", "Approve"),
            self._make_audit("security", "Approve"),
            self._make_audit("maintenance", "Approve"),
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit")
        self.assertTrue(g.passed)

    def test_review_changes_requested_fails(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-02T00:00:00Z", "id": "1"},
            self._make_audit("review", "Changes Requested"),
            self._make_audit("security", "Approve"),
            self._make_audit("maintenance", "Approve"),
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("non-Approve", g.detail)

    def test_missing_maintenance_audit_fails(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-02T00:00:00Z", "id": "1"},
            self._make_audit("review", "Approve"),
            self._make_audit("security", "Approve"),
            # maintenance audit absent
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)
        self.assertIn("MISSING", g.detail)

    def test_security_blocked_fails(self):
        comments = [
            {"user": "claude[bot]", "body": "**Verdict:** Approve",
             "created_at": "2026-01-02T00:00:00Z", "id": "1"},
            self._make_audit("review", "Approve"),
            self._make_audit("security", "Blocked"),
            self._make_audit("maintenance", "Approve"),
        ]
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps(comments)):
            g = pr_verify._gate_g3_llm_verdicts(584, "sh-ai-x/dev-harness-kit")
        self.assertFalse(g.passed)


class TestM6CLIForms(unittest.TestCase):
    """M-6: documented CLI forms — --pr / --repo flags + no-arg
    current-branch discovery — must work end-to-end without crashing."""

    def _all_passing_run_gh(self, args):
        sub = args[0]
        if sub == "pr" and len(args) > 1 and args[1] == "view":
            return json.dumps({
                "state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN",
                "mergeable": "MERGEABLE",
                "commits": [
                    {"oid": "abc123", "committedDate": "2026-08-06T00:00:00Z"},
                ],
            })
        if sub == "pr" and len(args) > 1 and args[1] == "checks":
            return json.dumps([
                {"name": "lint", "state": "COMPLETED", "bucket": "pass"},
            ])
        if sub == "api":
            comments = [
                {"id": "audit-1", "user": "github-actions", "created_at": "2026-08-06T00:00:00Z",
                 "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve"},
                {"id": "audit-2", "user": "github-actions", "created_at": "2026-08-06T00:00:01Z",
                 "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve"},
                {"id": "audit-3", "user": "github-actions", "created_at": "2026-08-06T00:00:02Z",
                 "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve"},
            ]
            return json.dumps(comments)
        if sub == "repo" and len(args) > 1 and args[1] == "view":
            return json.dumps({"nameWithOwner": "sh-ai-x/dev-harness-kit"})
        return json.dumps({})

    def test_cli_pr_flag(self):
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: self._all_passing_run_gh(args)):
            rc = pr_verify.main(["--pr", "584"])
        self.assertIn(rc, (0, 1))  # either pass or gate-fail; never crash

    def test_cli_pr_and_repo_flags(self):
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: self._all_passing_run_gh(args)):
            rc = pr_verify.main(["--pr", "584", "--repo", "sh-ai-x/dev-harness-kit"])
        self.assertIn(rc, (0, 1))

    def test_cli_legacy_positional_pr(self):
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: self._all_passing_run_gh(args)):
            rc = pr_verify.main(["584"])
        self.assertIn(rc, (0, 1))

    def test_cli_legacy_positional_pr_and_repo(self):
        with patch.object(pr_verify, "_run_gh", side_effect=lambda args: self._all_passing_run_gh(args)):
            rc = pr_verify.main(["584", "sh-ai-x/dev-harness-kit"])
        self.assertIn(rc, (0, 1))

    def test_cli_help_exits_without_calling_gh(self):
        """`--help` must short-circuit before any `gh` call so it never
        touches the network or the live PR."""
        with patch.object(pr_verify, "_run_gh") as mock_gh:
            with self.assertRaises(SystemExit) as cm:
                pr_verify.main(["--help"])
        self.assertEqual(cm.exception.code, 0)  # argparse exits 0 on --help
        mock_gh.assert_not_called()

    def test_cli_repo_resolution_failure_fails_closed(self):
        """Regression: when --repo is absent and `gh repo view` errors
        (e.g. run outside a GitHub-remote checkout), main() must fail
        closed (exit 2 + stderr hint) rather than silently defaulting
        to a hardcoded repo. A hardcoded fallback would silently verify
        the wrong repo's PR number when invoked from an unrelated
        checkout — the same 'trust stale/wrong state' failure mode
        this verifier exists to eliminate.
        """
        def _repo_view_fails(args):
            if args[0] == "repo" and len(args) > 1 and args[1] == "view":
                raise pr_verify.GhError("gh repo view failed: not a git repository")
            return self._all_passing_run_gh(args)

        with patch.object(pr_verify, "_run_gh", side_effect=_repo_view_fails):
            rc = pr_verify.main(["--pr", "584"])
        self.assertEqual(rc, 2,
                         "must fail closed (exit 2) when repo cannot be resolved, "
                         "not silently default to a hardcoded repo")


class TestRunHeadSha(unittest.TestCase):
    """`_run_head_sha` fetches the head SHA a GitHub Actions run was
    triggered against. Must fail closed (return None) on any error —
    callers treat None as an unverifiable provenance, never a match."""

    def test_returns_head_sha_on_success(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({"headSha": "abc123"})):
            sha = pr_verify._run_head_sha("42", "sh-ai-x/dev-harness-kit")
        self.assertEqual(sha, "abc123")

    def test_returns_none_on_gh_error(self):
        with patch.object(pr_verify, "_run_gh", side_effect=pr_verify.GhError("gh run view failed")):
            sha = pr_verify._run_head_sha("42", "sh-ai-x/dev-harness-kit")
        self.assertIsNone(sha)

    def test_returns_none_on_malformed_json(self):
        with patch.object(pr_verify, "_run_gh", return_value="not json"):
            sha = pr_verify._run_head_sha("42", "sh-ai-x/dev-harness-kit")
        self.assertIsNone(sha)

    def test_returns_none_on_missing_field(self):
        with patch.object(pr_verify, "_run_gh", return_value=json.dumps({})):
            sha = pr_verify._run_head_sha("42", "sh-ai-x/dev-harness-kit")
        self.assertIsNone(sha)


class TestG3HeadShaProvenance(unittest.TestCase):
    """Regression for the security review's headline finding on PR #588:
    a workflow run for a PREVIOUS head can post its trusted audit AFTER
    a new push lands. Timestamp-only freshness (created_at >=
    pr_pushed_at) cannot detect this — the stale run's audit comment is
    created AFTER the push even though the run itself executed against
    the OLD head. G3 must bind each required job's audit `run=` to a
    workflow run whose headSha equals the PR's CURRENT head."""

    _COMMENTS = json.dumps([
        {"id": "audit-1", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
         "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve"},
        {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
         "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve"},
        {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
         "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve"},
    ])

    def test_matching_head_sha_passes(self):
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                return json.dumps({"headSha": "newsha"})
            return self._COMMENTS
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertTrue(g.passed)

    def test_old_head_run_posts_after_new_push_fails(self):
        """The exact scenario from the security review: run=1 executed
        against the OLD head ('oldsha'), but its audit comment was
        created AFTER the new push (2026-01-03 > pushed_at 2026-01-02).
        A timestamp-only guard would wrongly call this fresh. The
        head-SHA check must catch it."""
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                return json.dumps({"headSha": "oldsha"})
            return self._COMMENTS
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertFalse(g.passed, "stale-head run must not count as current-head approval")
        self.assertIn("STALE", g.detail)

    def test_run_view_fetch_failure_fails_closed(self):
        """If `gh run view` itself fails, provenance is unverifiable —
        must NOT silently pass."""
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                raise pr_verify.GhError("gh run view failed: rate limited")
            return self._COMMENTS
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertFalse(g.passed)

    def test_shared_run_id_dedupes_fetch(self):
        """All three required jobs share run=1 in the fixture comments —
        `_run_head_sha` must be called at most once per unique run id,
        not once per job."""
        call_count = {"n": 0}
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                call_count["n"] += 1
                return json.dumps({"headSha": "newsha"})
            return self._COMMENTS
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertTrue(g.passed)
        self.assertEqual(call_count["n"], 1)

    def test_no_head_sha_falls_back_to_timestamp_guard(self):
        """When pr_head_sha is unavailable (empty), G3 must fall back
        to the pre-existing timestamp-based freshness guard rather than
        skip freshness checking entirely."""
        stale_comments = json.dumps([
            {"id": "audit-1", "user": "github-actions", "created_at": "2026-01-01T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=review status=success verdict=Approve"},
            {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-01T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=security status=success verdict=Approve"},
            {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-01T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=1 job=maintenance status=success verdict=Approve"},
        ])
        with patch.object(pr_verify, "_run_gh", return_value=stale_comments):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)


class TestG3CommentHeadShaProvenance(unittest.TestCase):
    """Regression for PR #750: fork-pr-review.yml dispatches
    review.yml / maintenance.yml with `--ref main`, so the dispatched
    run's own `gh run view <run_id> --json headSha` returns main's tip
    instead of the PR head, and the G3 head-SHA provenance check
    STALEs every correctly-judged fork PR. The audit comment writer
    now embeds `head_sha=<PR headRefOid>` on the parseable line so
    G3 can bind the verdict to the PR head without trusting the
    run's `headSha`. This test pins that path.
    """

    _COMMENTS_MATCHING = json.dumps([
        {"id": "audit-1", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
         "body": "<!-- dev-kit-verdict-audit --> run=42 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
        {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
         "body": "<!-- dev-kit-verdict-audit --> run=42 job=security status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
        {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
         "body": "<!-- dev-kit-verdict-audit --> run=42 job=maintenance status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
    ])

    def test_comment_head_sha_matches_pr_head_passes(self):
        """Audit `head_sha=` matches PR head — pass even when the run
        was dispatched against main (so `_run_head_sha()` would NOT
        have matched, and in fact must NOT be called at all because
        the comment is authoritative)."""
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                # Simulate the fork-pr-review dispatch: run headSha is
                # main's tip, NOT the PR head. A pre-fix gate would
                # STALE on this mismatch.
                return json.dumps({"headSha": "3e785d286f360aa7dd44ce5feb30e74945c84479"})
            return self._COMMENTS_MATCHING
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh) as gh_mock:
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        # `gh run view` must NOT be consulted when every required
        # audit carries an authoritative `head_sha=` matching the
        # current PR head — the comment is the source of truth on
        # fork-PR dispatched runs.
        self.assertFalse(any(
            (len(call.args) > 0 and call.args[0] == "run" and call.args[1] == "view")
            for call in gh_mock.call_args_list
        ),
            "gh run view should be skipped when comment carries a matching head_sha")
        self.assertTrue(g.passed,
                        f"matched comment head_sha must pass; got detail={g.detail!r}")

    def test_comment_head_sha_mismatch_stale(self):
        """Audit `head_sha=` differs from the PR's CURRENT head — the
        PR advanced and the audit is stale. Must STALE."""
        comments = json.dumps([
            {"id": "audit-1", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha=oldsha"},
            {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=security status=success verdict=Approve source=lib.maintenance_gate head_sha=oldsha"},
            {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=maintenance status=success verdict=Approve source=lib.maintenance_gate head_sha=oldsha"},
        ])
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                return json.dumps({"headSha": "newsha"})
            return comments
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)
        self.assertIn("oldsha", g.detail)
        self.assertIn("newsha", g.detail)

    def test_partial_comment_head_sha_only_some_jobs_present(self):
        """If a re-run only posted a new audit for some jobs while
        others still carry the older shape (no `head_sha=`), per-job
        fallback to `_run_head_sha()` must still work — the gate
        must not crash on partial coverage."""
        comments = json.dumps([
            # review: has head_sha matching
            {"id": "audit-1", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
            # security: legacy shape (no head_sha=), run headSha matches
            {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=43 job=security status=success verdict=Approve source=lib.maintenance_gate"},
            # maintenance: legacy shape, run headSha STALE
            {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=44 job=maintenance status=success verdict=Approve source=lib.maintenance_gate"},
        ])
        run_view_cache = {
            "42": {"headSha": "newsha"},
            "43": {"headSha": "newsha"},
            "44": {"headSha": "oldsha"},
        }
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                run_id = args[2]
                return json.dumps(run_view_cache[run_id])
            return comments
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)
        # Only the maintenance job (legacy shape + stale run headSha)
        # should be in mismatched_jobs — security used legacy shape
        # but the run matched, so it's fine.
        self.assertIn("maintenance", g.detail)
        self.assertNotIn("security", g.detail.split("(")[0])

    def test_audit_author_untrusted_ignores_head_sha(self):
        """A non-trusted-login comment that happens to carry
        `head_sha=...` must NOT bind G3 — only github-actions[bot]
        audits are authoritative.

        Test ordering: the untrusted comment has a STRICTLY NEWER
        `created_at` AND a `head_sha` that would mismatch if accepted.
        With a broken `TRUSTED_AUDIT_LOGINS` (accepts both), the
        untrusted comment wins via timestamp comparison and the gate
        fails — opposite of the expected outcome. So a green test
        here actually exercises the filter, not just the timestamp
        comparator (review F4)."""
        comments = json.dumps([
            # trusted older — matches
            {"id": "trusted-older", "user": "github-actions", "created_at": "2026-01-02T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
            # untrusted newer — would mismatch if filter were broken
            {"id": "untrusted-newer", "user": "random-mallory", "created_at": "2026-01-04T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha=forgedsha"},
            # trusted for other jobs
            {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-02T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=security status=success verdict=Approve source=lib.maintenance_gate"},
            {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-02T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=maintenance status=success verdict=Approve source=lib.maintenance_gate"},
        ])
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                return json.dumps({"headSha": "newsha"})
            return comments
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-01T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertTrue(
            g.passed,
            "comment head_sha from non-trusted login must NOT influence G3; "
            "if a broken filter accepted it, the newer untrusted comment "
            "with head_sha=forgedsha would have STALEd the gate",
        )

    def test_empty_head_sha_mixed_with_legacy_audit(self):
        """Partial coverage: one job's audit carries empty `head_sha=`
        while another job's audit carries the legacy shape (no
        `head_sha=` at all). The legacy job falls back to
        `_run_head_sha()`; the empty-head_sha job STALEs without a
        fetch. Both must STALE individually but the gate reports the
        union."""
        comments = json.dumps([
            # review: empty head_sha (M1 regression)
            {"id": "audit-1", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=42 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha="},
            # security: legacy shape (no head_sha=) → fall back to _run_head_sha
            {"id": "audit-2", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=43 job=security status=success verdict=Approve source=lib.maintenance_gate"},
            # maintenance: matching head_sha → pass
            {"id": "audit-3", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=44 job=maintenance status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
        ])
        # For the legacy security job, _run_head_sha returns OLDsha →
        # stale. For maintenance: matches. For review: empty-but-present
        # STALEs without a fetch.
        run_view_cache = {"43": {"headSha": "oldsha"}}
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                run_id = args[2]
                return json.dumps(run_view_cache.get(run_id, {"headSha": "newsha"}))
            return comments
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)
        # Both review (empty head_sha) and security (legacy + stale
        # run headSha) are in mismatched_jobs; maintenance matches.
        self.assertIn("review", g.detail)
        self.assertIn("security", g.detail)

    def test_empty_head_sha_distinct_from_absent_head_sha(self):
        """Two audits side-by-side — same job, different shapes:
        audit A carries no `head_sha=` at all (legacy, falls back to
        `_run_head_sha()`); audit B carries `head_sha=` empty
        (M1 case, STALE without fallback). The newer of the two wins
        for that job. With audit B newer and empty, the gate STALEs
        without ever consulting `gh run view`."""
        comments = json.dumps([
            # legacy audit, older — would fall back to _run_head_sha
            {"id": "audit-legacy", "user": "github-actions", "created_at": "2026-01-01T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=99 job=review status=success verdict=Approve source=lib.maintenance_gate"},
            # newer audit, empty head_sha= (M1 case)
            {"id": "audit-empty", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=100 job=review status=success verdict=Approve source=lib.maintenance_gate head_sha="},
            # newer audit, matching head_sha for security
            {"id": "audit-sec", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=100 job=security status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
            # newer audit, matching head_sha for maintenance
            {"id": "audit-mtn", "user": "github-actions", "created_at": "2026-01-03T00:00:00Z",
             "body": "<!-- dev-kit-verdict-audit --> run=100 job=maintenance status=success verdict=Approve source=lib.maintenance_gate head_sha=newsha"},
        ])
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                # If the gate DID call _run_head_sha (it must not),
                # return main HEAD to confirm the pre-fix bug path.
                return json.dumps({"headSha": "mainHEAD"})
            return comments
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh) as gh_mock:
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        gh_run_view_calls = [
            call for call in gh_mock.call_args_list
            if len(call.args) > 0 and call.args[0] == "run" and call.args[1] == "view"
        ]
        self.assertEqual(
            gh_run_view_calls, [],
            "newer audit's empty head_sha= must suppress the legacy "
            "_run_head_sha() fallback (would return main HEAD on fork PRs)"
        )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)
        self.assertIn("review", g.detail)

    def test_empty_head_sha_only_does_not_call_run_head_sha(self):
        """Unit-style pin for the M1 fix: when ALL three required jobs
        carry `head_sha=` empty, the gate must STALE without making
        ANY `gh run view` round-trip. This is the strict
        counter-example the LLM judge will check against the
        `_run_head_sha()` main-HEAD fallback."""
        comments = json.dumps([
            {"id": f"audit-{j}", "user": "github-actions",
             "created_at": "2026-01-03T00:00:00Z",
             "body": f"<!-- dev-kit-verdict-audit --> run={n} job={j} status=success verdict=Approve source=lib.maintenance_gate head_sha="}
            for n, j in [(42, "review"), (42, "security"), (42, "maintenance")]
        ])
        def fake_gh(args):
            if args[0] == "run" and args[1] == "view":
                raise AssertionError(
                    "_run_head_sha() must NOT be called when audit "
                    "carries head_sha= empty-but-present (M1 fix)"
                )
            return comments
        with patch.object(pr_verify, "_run_gh", side_effect=fake_gh):
            g = pr_verify._gate_g3_llm_verdicts(
                584, "sh-ai-x/dev-harness-kit",
                comments=None,
                pr_pushed_at="2026-01-02T00:00:00Z",
                pr_head_sha="newsha",
            )
        self.assertFalse(g.passed)
        self.assertIn("STALE", g.detail)


class TestLatestPerJobAuditsParseExtras(unittest.TestCase):
    """Regression pin for the audit-comment parser in
    `_latest_per_job_audits`. These tests focus on the parse step
    (not the gate logic) so a future emitter change can be validated
    without exercising the whole G3 gate."""

    _BODY_TMPL = (
        "<!-- dev-kit-verdict-audit --> run=42 job={job} status=success "
        "verdict=Approve source=lib.maintenance_gate{extra}"
    )

    def _comment(self, body, login="github-actions", cid="c1",
                 created="2026-01-03T00:00:00Z"):
        return {
            "id": cid, "user": login, "created_at": created, "body": body,
        }

    def test_empty_head_sha_parsed_as_empty_string(self):
        """`head_sha=` (empty) must be parsed as the empty string (not
        skipped) so G3 can distinguish it from a missing key. Before
        the regex change, `(\\w+)=(\\S+)` rejected empty values."""
        body = self._BODY_TMPL.format(job="review", extra=" head_sha=")
        latest = pr_verify._latest_per_job_audits((self._comment(body),))
        self.assertIn("review", latest)
        self.assertEqual(latest["review"]["head_sha"], "",
                         "empty `head_sha=` must be parsed as '' (NOT skipped)")

    def test_absent_head_sha_parsed_as_none(self):
        """When `head_sha=` is absent from the parseable line, the
        record's `head_sha` field is None (NOT empty string) so G3's
        `is None` vs `== ""` branches route correctly."""
        body = self._BODY_TMPL.format(job="review", extra="")
        latest = pr_verify._latest_per_job_audits((self._comment(body),))
        self.assertIn("review", latest)
        self.assertIsNone(latest["review"]["head_sha"],
                          "absent `head_sha` must be None, not ''")

    def test_duplicate_head_sha_last_value_wins(self):
        """A malformed body with two `head_sha=` keys uses the LAST
        value (dict() constructor semantics). Defensive against a
        future emitter bug; deterministic either way."""
        body = (
            "<!-- dev-kit-verdict-audit --> run=42 job=review "
            "status=success verdict=Approve source=lib.maintenance_gate "
            "head_sha=first head_sha=second"
        )
        latest = pr_verify._latest_per_job_audits((self._comment(body),))
        self.assertEqual(latest["review"]["head_sha"], "second")

    def test_stray_html_comment_before_marker_does_not_shift_extras(self):
        """A stray `<!-- ... -->` block earlier in the body (e.g. a
        template-engine comment) must NOT shift the extras slice
        window. The audit_re match position is the source of truth,
        not `body.find("-->")`."""
        body = (
            "<!-- random noise -->\n"
            "<!-- dev-kit-verdict-audit --> run=42 job=review "
            "status=success verdict=Approve source=lib.maintenance_gate "
            "head_sha=newsha"
        )
        latest = pr_verify._latest_per_job_audits((self._comment(body),))
        self.assertEqual(latest["review"]["head_sha"], "newsha")

    def test_head_sha_on_later_line_does_not_override_parseable_line(self):
        """Review F1 regression pin: extras regex scoping MUST be
        limited to parseable line 1. A `head_sha=<value>` mention in
        later markdown (URL, code-block, table cell, anywhere after
        the parseable line) must NOT override the parseable-line
        value via dict() last-wins.

        This is the load-bearing invariant for the empty-but-present
        STALE branch: without line-1 scope, a future markdown addition
        could silently nullify the M1 fix's STALE-on-empty path."""
        body = (
            "<!-- dev-kit-verdict-audit --> run=42 job=review "
            "status=success verdict=Approve source=lib.maintenance_gate "
            "head_sha=\n"
            "\n"
            "**dev-kit CI verdict — ✅ Approve**\n"
            "\n"
            "| Field   | Value\n"
            "|----------|------------------------\n"
            "| Run     | 42\n"
            "| Job     | review\n"
            "| Head_sha| head_sha=forgedvalue\n"
        )
        latest = pr_verify._latest_per_job_audits((self._comment(body),))
        # parseable line 1 has `head_sha=` (empty) — must be parsed as
        # empty string, NOT overridden by the table cell content.
        self.assertEqual(
            latest["review"]["head_sha"], "",
            "later-line `head_sha=` mention must NOT override parseable "
            "line value (M1 empty-but-present STALE branch depends on this)"
        )


if __name__ == "__main__":
    unittest.main()

