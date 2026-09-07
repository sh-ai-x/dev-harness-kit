"""test_review_yml_l3_pr_comment.py — static regression guard for the
L3 evidence gate step in `.github/workflows/review.yml`.

Discovered live (2026-09-07): PR #797 hit the L3 evidence gate step
(severity gate job, "L3 evidence gate (PR body must quote test count)"
step, `.github/workflows/review.yml` lines 841-890) and the gate failed
with `::error::PR body lacks a quoted pytest tail line.` — but the
ONLY trace of why was in the GitHub Actions log. No PR comment was
posted explaining the failure, so a contributor reading the PR only
saw two "Approve" verdict comments + a red check with no attached
reason. They had to open the Actions log to discover the cause. Issue:
https://github.com/sh-ai-x/dev-harness-kit/issues/803

These tests are deliberately static (grep the YAML text directly)
rather than spinning up an actual workflow run — GH-Actions jobs
aren't unit-testable in this repo's suite, so a content-level
regression guard is the practical alternative. Mirrors the style of
`tests/test_review_yml_touch_probe.py::TestReviewYmlTouchProbeRootsMatchCanonical`.

Fix shape: when the L3 evidence gate step fails on a prod-touching PR
(TOUCHES_PROD=true AND pytest-tail pattern not found in PR body), the
step MUST also post a PR comment explaining the failure + remediation,
so a contributor reading the PR's "Conversation" tab sees the same
diagnostic they'd otherwise have to dig out of the Actions log.

The comment body lives in a separate file under
`.github/review-yml/l3-evidence-fail.md` (rather than inline in the
YAML) because multi-line bash strings inside a YAML literal block trip
the YAML scanner on `**` (alias char) and `<<` (merge-key operator)
patterns. The workflow reads the file via `gh pr comment --body-file`.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
REVIEW_YML = PROJECT_ROOT / ".github" / "workflows" / "review.yml"


class TestReviewYmlL3EvidencePostsPrComment(unittest.TestCase):
    """Pin the L3 evidence gate step's PR-comment-posting behavior."""

    def setUp(self) -> None:
        self.text = REVIEW_YML.read_text(encoding="utf-8")
        # Extract the L3 evidence gate step body. The `name:` line for
        # this step is unique; grab everything from that line until
        # the next `      - name:` line at the same indentation (4
        # spaces in the gate job). Using a non-greedy match avoids
        # swallowing subsequent steps.
        m = re.search(
            r"      - name: L3 evidence gate \(PR body must quote test count\)(.*?)"
            r"(?=      - name: |\Z)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "could not find the L3 evidence gate step in review.yml",
        )
        self.step_body = m.group(1)

    def test_l3_step_has_pull_requests_write_permission(self) -> None:
        """The `severity gate (review + security)` job must already
        grant `pull-requests: write` so the new `gh pr comment` call
        has the token scope it needs. Pinned here so a future
        permissions-downgrade doesn't silently regress the new
        PR-comment call to a 403."""
        # Find the gate job block. The job key is `gate:` with
        # `name: severity gate (review + security)`, so anchor on the
        # `name:` line which is unique in the file.
        m = re.search(
            r"  gate:\s*\n\s*name: severity gate \(review \+ security\)(.*?)"
            r"(?=\n  \w|\n\w|\Z)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "could not find the 'severity gate (review + security)' job in review.yml",
        )
        job_body = "gate:\n    name: severity gate (review + security)" + m.group(1)
        self.assertRegex(
            job_body,
            r"pull-requests:\s*write",
            "the severity gate job must grant `pull-requests: write` "
            "so the L3-evidence PR-comment call has token scope",
        )

    def test_l3_step_calls_gh_pr_comment_on_fail(self) -> None:
        """When TOUCHES_PROD=true AND the pytest tail pattern is not
        found, the step MUST call `gh pr comment` so a PR-visible
        comment is posted when the gate fails (issue #803)."""
        self.assertIn(
            "gh pr comment",
            self.step_body,
            "L3 evidence gate step must call `gh pr comment` so a "
            "PR-visible comment is posted when the gate fails "
        "(issue #803)",
        )

    def test_l3_step_uses_body_file(self) -> None:
        """The comment body lives in a separate file under
        `.github/review-yml/l3-evidence-fail.md`, NOT inline in the
        YAML (multi-line bash strings inside YAML literal blocks
        trip the YAML scanner on `**` and `<<` patterns). The
        workflow MUST read that file via `--body-file` so the
        markdown body stays readable and YAML-safe."""
        self.assertIn(
            "--body-file",
            self.step_body,
            "L3 step must use `gh pr comment --body-file ...` "
            "so the markdown body stays readable (issue #803)",
        )
        # The file must live in the workflow's checkout (not be
        # synthesized at runtime) so contributors can edit it.
        self.assertIn(
            "l3-evidence-fail.md",
            self.step_body,
            "L3 step must reference the l3-evidence-fail.md "
            "body-file (the canonical comment body)",
        )

    def test_l3_step_calls_comment_only_in_fail_branch(self) -> None:
        """The `gh pr comment` call must be in the FAIL branch
        (TOUCHES_PROD=true AND pattern not found), NOT in the OK
        branch (pattern found, exit 0) and NOT in the docs/infra-only
        advisory branch (TOUCHES_PROD != 'true'). Otherwise a passing
        PR would spam a comment, or a docs-only PR would block on
        something that's meant to be advisory."""
        ok_branch_exit = self.step_body.find("exit 0")
        self.assertNotEqual(
            ok_branch_exit,
            -1,
            "L3 step must have an OK-branch `exit 0` for the pattern-found case",
        )
        comment_idx = self.step_body.find("gh pr comment")
        self.assertNotEqual(
            comment_idx,
            -1,
            "L3 step must include a `gh pr comment` call",
        )
        self.assertGreater(
            comment_idx,
            ok_branch_exit,
            "L3 step's `gh pr comment` call must live in the FAIL "
            "branch (after the OK-branch `exit 0`), not before it "
            "(a passing PR must NOT post a comment)",
        )

    def test_l3_step_advisory_branch_does_not_post_comment(self) -> None:
        """The docs/infra-only advisory branch (TOUCHES_PROD !=
        'true') must remain advisory only — no PR comment, no exit
        1. The PR-comment-on-fail change is scoped to the strict
        prod-touching PRs only."""
        m = re.search(
            r"          else\s*\n(.*?)(?=\n          fi|\Z)",
            self.step_body,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "L3 step must have an `else` branch for the docs/infra-only "
            "advisory case",
        )
        advisory_branch = m.group(1)
        self.assertNotIn(
            "gh pr comment",
            advisory_branch,
            "L3 step's docs/infra-only advisory branch must NOT "
            "post a PR comment — the comment-on-fail change is "
            "scoped to TOUCHES_PROD=true only",
        )


class TestL3EvidenceCommentBody(unittest.TestCase):
    """Pin the content of the PR-comment body file
    `.github/review-yml/l3-evidence-fail.md`. Static guard for the
    same reasons as TestReviewYmlL3EvidencePostsPrComment."""

    BODY_FILE = (
        PROJECT_ROOT / ".github" / "review-yml" / "l3-evidence-fail.md"
    )

    def test_body_file_exists(self) -> None:
        self.assertTrue(
            self.BODY_FILE.exists(),
            f"missing body file: {self.BODY_FILE}",
        )

    def test_body_file_labels_l3_gate(self) -> None:
        """Body must label itself `L3 evidence gate` so the comment
        is grep-discoverable from the PR's Conversation tab."""
        text = self.BODY_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "L3 evidence gate",
            text,
            "body file must label itself 'L3 evidence gate' so the "
            "comment is grep-discoverable from the PR's Conversation tab",
        )

    def test_body_file_includes_required_pytest_forms(self) -> None:
        """Body must enumerate the four accepted pytest summary forms
        so the contributor can copy/paste from the comment itself."""
        text = self.BODY_FILE.read_text(encoding="utf-8")
        for form in (
            "passed in",
            "passed, ",
            "failed, ",
            "failed in",
        ):
            self.assertIn(
                form,
                text,
                f"body file must include pytest-summary form "
                f"{form!r} so the contributor can see all accepted "
                f"formats in the PR-visible comment",
            )

    def test_body_file_references_pr_template(self) -> None:
        """Body must point at the PR template's Iron Law L3 section
        so the author knows where to paste the pytest tail."""
        text = self.BODY_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "pull_request_template.md",
            text,
            "body file must reference pull_request_template.md so "
            "the author knows where to paste the pytest tail",
        )

    def test_body_file_includes_issue_link(self) -> None:
        """Body must link issue #803 so a contributor who finds the
        comment can read the full bug context."""
        text = self.BODY_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "issues/803",
            text,
            "body file must link issue #803 so a contributor who "
            "finds the comment can read the full bug context",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
