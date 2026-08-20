#!/usr/bin/env python3
"""test_fork_pr_auto_review.py — pin the auto-review path for fork PRs.

Background:
  Before this change, fork PRs required a manual maintainer click in
  the `fork-pr-review` GitHub Environment before the LLM judges in
  review.yml / maintenance.yml would even run. That made the AI review
  unreliable on PRs from external contributors (e.g. PRs #682 and #687
  from `mybotagent`): the maintainer had to babysit every fork PR, and
  PRs sat in the approval queue when nobody was watching.

  The fix: review.yml and maintenance.yml now ALSO trigger on
  `pull_request_target`, and their LLM-judge jobs auto-run for fork
  PRs gated by `vars.AUTO_REVIEW_FORKS != 'false'`. The fork-trust
  model is preserved by:
    1. Workflow file is read from the base branch (not the fork).
    2. `actions/checkout` uses the safe default ref (the merge commit,
       not the PR head SHA) — no explicit `ref:` is allowed.
    3. The LLM judges fetch the diff via `gh pr diff` (GitHub API), so
       the runner's filesystem content cannot influence the judge
       beyond the visible PR contents.

  `fork-pr-review.yml` is retained as an OPT-IN manual fallback
  (active only when `vars.AUTO_REVIEW_FORKS == 'false'`); see
  tests/test_fork_pr_review_gh_api.py for that side.

This test pins the review.yml / maintenance.yml side so the
auto-review contract cannot silently regress:

  T1: review.yml triggers on pull_request_target (in addition to
      pull_request and workflow_dispatch).
  T2: maintenance.yml triggers on pull_request_target (same shape).
  T3: review.yml's LLM-judge jobs (review, security, gate) allow fork
      PRs via pull_request_target with the AUTO_REVIEW_FORKS gate.
  T4: maintenance.yml's LLM-judge jobs (maintenance_judge, gate) allow
      fork PRs via pull_request_target with the AUTO_REVIEW_FORKS gate.
  T5: review.yml and maintenance.yml have NO `actions/checkout` step
      that explicitly sets `ref:` to the PR head SHA. A `ref:` pin to
      `${{ github.event.pull_request.head.sha }}` would defeat the
      fork-trust model (the runner would check out untrusted fork code
      and any prompt-injection in the SKILL files would land).
  T6: review.yml and maintenance.yml have NO step that runs arbitrary
      code from the fork (no `run: <fork content>`, no `uses:
      actions/...@<PR SHA>`). Belt-and-suspenders against future
      contributors adding "helpful" steps that execute fork code.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).parent.parent / ".github" / "workflows"
REVIEW_YML = WORKFLOWS_DIR / "review.yml"
MAINTENANCE_YML = WORKFLOWS_DIR / "maintenance.yml"


def _yaml_doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on_dict(doc: dict) -> dict:
    """PyYAML >=1.1 coerces bare `on:` to the boolean True. Tolerate both."""
    if isinstance(doc.get(True), dict):
        return doc[True]
    if isinstance(doc.get("on"), dict):
        return doc["on"]
    raise AssertionError(
        f"workflow has no `on:` triggers; doc keys = {list(doc)}"
    )


def _job_if(doc: dict, job_name: str) -> str:
    """Return the `if:` expression for a job, or '' if absent."""
    job = doc.get("jobs", {}).get(job_name)
    if not job:
        return ""
    return job.get("if", "")


class TestForkPrAutoReview(unittest.TestCase):

    def test_01_review_yml_triggers_on_pull_request_target(self):
        """review.yml must trigger on pull_request_target so fork PRs
        (which can't authenticate on pull_request) get full secrets."""
        doc = _yaml_doc(REVIEW_YML)
        on_dict = _on_dict(doc)
        self.assertIn("pull_request_target", on_dict,
                      "review.yml must trigger on pull_request_target so "
                      "fork PRs authenticate — without it, every fork PR "
                      "auto-skips the LLM judges (the pre-PR bug)")
        self.assertIn("pull_request", on_dict,
                      "review.yml must keep the pull_request trigger — "
                      "same-repo PRs continue to auto-review on it")
        self.assertIn("workflow_dispatch", on_dict,
                      "review.yml must keep the workflow_dispatch trigger "
                      "for manual re-runs and fork-pr-review dispatch")

    def test_02_maintenance_yml_triggers_on_pull_request_target(self):
        """maintenance.yml must mirror review.yml's trigger set."""
        doc = _yaml_doc(MAINTENANCE_YML)
        on_dict = _on_dict(doc)
        self.assertIn("pull_request_target", on_dict,
                      "maintenance.yml must trigger on pull_request_target")
        self.assertIn("pull_request", on_dict,
                      "maintenance.yml must keep the pull_request trigger")
        self.assertIn("workflow_dispatch", on_dict,
                      "maintenance.yml must keep the workflow_dispatch "
                      "trigger for manual re-runs")

    def test_03_review_yml_llm_judge_jobs_allow_fork_prs(self):
        """review.yml's LLM-judge jobs (`review`, `security`) must
        explicitly allow fork PRs on `pull_request_target`, gated by
        `vars.AUTO_REVIEW_FORKS != 'false'`. Without this branch, fork
        PRs sit in `skipped` indefinitely (the pre-PR bug)."""
        doc = _yaml_doc(REVIEW_YML)
        for job_name in ("review", "security"):
            with self.subTest(job=job_name):
                if_text = _job_if(doc, job_name)
                self.assertIn("pull_request_target", if_text,
                              f"{job_name}: must allow pull_request_target "
                              "events so fork PRs run automatically")
                self.assertIn("AUTO_REVIEW_FORKS", if_text,
                              f"{job_name}: must reference AUTO_REVIEW_FORKS "
                              "to give operators a manual-gate opt-out")
                self.assertIn("'false'", if_text,
                              f"{job_name}: must require AUTO_REVIEW_FORKS "
                              "!= 'false' (literal string) — default unset "
                              "should auto-run, not opt-in to manual gate")

    def test_03b_review_yml_gate_job_runs_for_fork_prs(self):
        """review.yml's `gate` job uses a different (more permissive)
        shape — `always() && … && (workflow_dispatch || same-repo ||
        (fork && AUTO_REVIEW_FORKS != 'false'))` — so it runs for fork
        PRs under both `pull_request` (if same-repo) and
        `pull_request_target` (if fork) without naming the event type.
        Pin the AUTO_REVIEW_FORKS gate so a future refactor can't
        silently drop the operator opt-out."""
        doc = _yaml_doc(REVIEW_YML)
        if_text = _job_if(doc, "gate")
        self.assertIn("AUTO_REVIEW_FORKS", if_text,
                      "gate job must reference AUTO_REVIEW_FORKS so "
                      "operators can opt back into the manual gate")
        self.assertIn("'false'", if_text,
                      "gate job must require AUTO_REVIEW_FORKS != 'false' "
                      "(literal string) — default unset auto-runs")

    def test_04_maintenance_yml_jobs_allow_fork_prs_via_pull_request_target(self):
        """maintenance.yml's LLM-judge jobs (`maintenance_judge`) must
        mirror review.yml's explicit fork-trust branch. The `gate` job
        uses the same permissive shape as review.yml's gate (covered by
        T04b)."""
        doc = _yaml_doc(MAINTENANCE_YML)
        if_text = _job_if(doc, "maintenance_judge")
        self.assertIn("pull_request_target", if_text,
                      "maintenance_judge must allow pull_request_target")
        self.assertIn("AUTO_REVIEW_FORKS", if_text,
                      "maintenance_judge must reference AUTO_REVIEW_FORKS")
        self.assertIn("'false'", if_text,
                      "maintenance_judge must require AUTO_REVIEW_FORKS "
                      "!= 'false'")

    def test_04b_maintenance_yml_gate_job_runs_for_fork_prs(self):
        """maintenance.yml's `gate` job mirrors review.yml's gate
        shape — pin the AUTO_REVIEW_FORKS gate so the opt-out cannot
        silently regress."""
        doc = _yaml_doc(MAINTENANCE_YML)
        if_text = _job_if(doc, "gate")
        self.assertIn("AUTO_REVIEW_FORKS", if_text,
                      "gate job must reference AUTO_REVIEW_FORKS")
        self.assertIn("'false'", if_text,
                      "gate job must require AUTO_REVIEW_FORKS != 'false'")

    def test_05_no_checkout_with_fork_head_ref(self):
        """review.yml and maintenance.yml must NOT have an
        `actions/checkout` step with an explicit `ref:` pointing at the
        PR head SHA. Such a ref would check out untrusted fork code on
        pull_request_target runs, defeating the entire fork-trust
        model (the dev-kit plugin symlink would read from the fork's
        tree, and prompt-injection in the SKILL files would land).

        Pinning the absence is stricter than pinning the form: the
        safe default is `ref:` unset, and any future contributor adding
        an explicit `ref:` for "convenience" gets blocked here.
        """
        head_ref_pattern = re.compile(
            r"ref:\s*\$?\{\{\s*github\.event\.pull_request\.head(?:\.sha|\.ref)\s*\}\}"
        )
        for workflow_path in (REVIEW_YML, MAINTENANCE_YML):
            with self.subTest(workflow=workflow_path.name):
                doc = _yaml_doc(workflow_path)
                for job_name, job in doc.get("jobs", {}).items():
                    for step in job.get("steps", []):
                        uses = step.get("uses", "")
                        if not uses.startswith("actions/checkout"):
                            continue
                        with_text = step.get("with", {})
                        ref_value = with_text.get("ref", "")
                        self.assertFalse(
                            head_ref_pattern.search(ref_value),
                            f"{workflow_path.name} job {job_name!r} step "
                            f"{step.get('name', '?')!r}: `actions/checkout` "
                            "sets `ref:` to the PR head — on "
                            "pull_request_target this checks out untrusted "
                            "fork code and breaks the fork-trust model. "
                            "Leave `ref:` unset (default = GitHub-built "
                            "merge commit, which is trusted)."
                        )

    def test_06_no_step_executes_arbitrary_fork_code(self):
        """Belt-and-suspenders: review.yml and maintenance.yml must not
        have any step that runs arbitrary shell from the workspace or
        uses an action pinned to a PR SHA. The dev-kit plugin
        symlinking step is allowed (it reads from `$GITHUB_WORKSPACE`
        = merge commit) but cannot be parameterized to read fork code.

        This is a coarse check: it scans every step's `run:` block for
        the `gh pr diff` / `gh pr view` shape (we want this — fetches
        diff via API, not from the local fork tree) and rejects any
        step that runs `./<PR file>` or `bash <PR file>` patterns.
        """
        # Patterns we forbid in any `run:` block.
        forbidden_run_patterns = [
            re.compile(r"\bbash\s+\$?\{\{\s*github\.event\.pull_request\.head"),
            re.compile(r"\bbash\s+\$?GITHUB_WORKSPACE/.*\.sh"),  # arbitrary .sh from ws
            re.compile(r"uses:\s*[^@\s]+@\$?\{\{\s*github\.event\.pull_request\.head"),
        ]
        # Whitelisted patterns (these are SAFE — explicitly checked):
        #   - `gh pr diff ...` / `gh pr view ...` — reads via GitHub API
        #   - `ln -sfn "$GITHUB_WORKSPACE" ...` — symlink dev-kit plugin
        #     from merge commit (allowed; the merge commit is trusted)
        #   - `actions/checkout@...` — checked separately in T5
        for workflow_path in (REVIEW_YML, MAINTENANCE_YML):
            with self.subTest(workflow=workflow_path.name):
                doc = _yaml_doc(workflow_path)
                for job_name, job in doc.get("jobs", {}).items():
                    for step in job.get("steps", []):
                        run = step.get("run", "")
                        for pat in forbidden_run_patterns:
                            self.assertIsNone(
                                pat.search(run),
                                f"{workflow_path.name} job {job_name!r} "
                                f"step {step.get('name', '?')!r}: matched "
                                f"forbidden pattern {pat.pattern!r} — would "
                                "execute untrusted fork code. Fetch diffs "
                                "via `gh pr diff` or `gh pr view`; pin "
                                "actions to immutable SHAs, not to the "
                                "PR head."
                            )


if __name__ == "__main__":
    unittest.main()
