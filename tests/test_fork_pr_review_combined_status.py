"""test_fork_pr_review_combined_status.py — regression guard for the
combined `fork-pr-review/ai-judges` commit status description in
`.github/workflows/fork-pr-review.yml`.

Discovered live (2026-09-07, issue #803): PR #797 hit the L3 evidence
gate step (severity gate job in `.github/workflows/review.yml`), and the
workflow-level conclusion for `review.yml` flipped to `failure`. The
fork-pr-review gate then posted:

    fork-pr-review/ai-judges  failure
      AI judge failure (review=failure maintenance=failure)

This attributed the failure to the **review LLM judge**, but the review
judge itself returned **Approve** — the failure came from the
deterministic L3-evidence sub-check inside the `severity gate` job
(which wraps both judges + the L3 sub-check). The status description
conflated "the job that wraps the LLM judge failed" with "the LLM
judge said Changes Requested / Blocked", and the per-judge commit
statuses were less informative than the combined one because the
"judge job conclusion=$CONCL" string just echoes the workflow
conclusion without distinguishing judge vs gate.

The fix shape:
  - The combined status description must separate the JUDGE verdict
    from the GATE verdict, so a deterministic sub-check failure isn't
    reported as `review=failure` when the review LLM judge actually
    approved. Suggested format:
        `judges=(review=Approve security=Approve) gate=failure(L3-evidence)`
    or similar — what matters is that `gate=` carries a distinct label
    when the gate (vs the judge) is the source of failure.
  - The description must be derivable from the per-job conclusions
    fetched in the existing `Post per-judge commit statuses` step
    (so no extra `gh run view` calls are introduced — pin existing
    job-list fetch + add a per-job `severity gate (review + security)`
    lookup alongside the existing per-judge lookups).

These tests are deliberately static (regex on the workflow YAML +
behavioral contract assertions on the description-format logic), per
the established static-guard convention in this repo (see
`tests/test_fork_pr_review_gh_api.py` and
`tests/test_review_yml_touch_probe.py`).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

WORKFLOW_PATH = (
    Path(__file__).parent.parent / ".github" / "workflows" / "fork-pr-review.yml"
)


def _yaml_doc() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step_by_name(name: str) -> dict:
    doc = _yaml_doc()
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step
    raise AssertionError(f"no step named {name!r} in fork-pr-review.yml")


class TestForkPrReviewCombinedStatusDescription(unittest.TestCase):
    """Pin the shape of the `fork-pr-review/ai-judges` status description
    so a deterministic sub-check failure isn't reported as a judge failure."""

    def setUp(self) -> None:
        self.step = _step_by_name("Post final AI-judge status to PR commit")
        self.run = self.step.get("run", "")

    def test_step_exists(self) -> None:
        """Sanity: the named step must still exist (renames are caught)."""
        self.assertTrue(self.run, "step has no `run:` block")

    def test_step_references_judge_and_gate_in_description(self) -> None:
        """The combined description must label the judge verdict
        separately from the gate verdict, so a gate failure is NOT
        reported as a judge failure (issue #803)."""
        # The description string must contain a `judge=` segment AND a
        # `gate=` segment (or equivalent labels), and the `gate=`
        # segment must be capable of carrying a distinct value from
        # the per-judge verdicts.
        self.assertRegex(
            self.run,
            r"judge",
            "the combined status description must reference the "
            "judge verdict separately (so a deterministic gate "
            "failure isn't reported as a judge failure — issue #803)",
        )
        self.assertRegex(
            self.run,
            r"gate",
            "the combined status description must reference the "
            "gate verdict separately (so a deterministic gate "
            "failure isn't reported as a judge failure — issue #803)",
        )

    def test_step_fetches_gate_job_conclusion(self) -> None:
        """The combined step must look up the `severity gate (review
        + security)` job's conclusion alongside the workflow-level
        conclusion, so the description can distinguish judge vs gate
        source of failure. The existing `Post per-judge commit
        statuses` step already fetches job conclusions for each
        judge; the combined step either reuses that lookup or
        fetches its own — both are acceptable as long as the gate
        job's conclusion is consumed."""
        # Look for either:
        #   (a) a per-job-conclusion lookup that includes the gate
        #       job name (`severity gate (review + security)`), or
        #   (b) a reference to a previously-set env var (e.g.
        #       `$GATE_CONCL` / `$REVIEW_GATE_CONCL`) that the
        #       per-judge step sets.
        per_job_gate_lookup = re.search(
            r"severity gate \(review \+ security\)",
            self.run,
        )
        gate_env_ref = re.search(
            r"GATE_CONCL|REVIEW_GATE_CONCL",
            self.run,
        )
        self.assertTrue(
            bool(per_job_gate_lookup) or bool(gate_env_ref),
            "the combined status step must consume the "
            "`severity gate (review + security)` job's conclusion "
            "so the description can distinguish judge vs gate "
            "source of failure (issue #803)",
        )

    def test_step_description_can_report_gate_only_failure(self) -> None:
        """The description logic must be able to report `gate=failure`
        (or equivalent) WITHOUT rolling that into a `judge=failure`
        verdict — i.e. the case where the LLM judge said Approve but
        the gate (L3 sub-check) failed must produce a description
        that names the GATE as the failure source, not the JUDGE."""
        # Heuristic: the script must contain BOTH a `failure` branch
        # for the gate outcome AND a path that constructs the
        # description string from the gate conclusion (not just the
        # workflow-level conclusion). We assert the gate conclusion
        # is referenced in a description-construction context.
        # Find every line that assigns to `DESCRIPTION=` (the
        # existing convention) and assert at least one such line
        # references the gate label.
        description_lines = re.findall(
            r"DESCRIPTION=\"[^\"]*\"",
            self.run,
        )
        if not description_lines:
            # No DESCRIPTION= line found — could be a here-string
            # instead. Acceptable as long as the here-string carries
            # the gate label.
            self.assertRegex(
                self.run,
                r"gate=",
                "if DESCRIPTION= lines are not used, the description "
                "must still carry a `gate=` segment (issue #803)",
            )
            return
        gate_aware = any("gate" in line.lower() for line in description_lines)
        self.assertTrue(
            gate_aware,
            "at least one DESCRIPTION= assignment must carry the "
            "gate label, so a gate-only failure is named as the "
            "gate (not the judge) — issue #803. "
            f"Found: {description_lines}",
        )

    def test_step_no_longer_blames_judge_for_gate_failure(self) -> None:
        """The combined status must NOT claim `AI judge failure` when
        the failure is actually a gate failure with both judges
        Approve. The existing description string
        `AI judge failure (review=failure maintenance=failure)` is
        what sent us investigating the wrong verdict first — pin
        that the new description does not claim a judge failure
        when gate is the source."""
        # Find any DESCRIPTION= assignment that defaults to
        # `AI judge failure`. If found, the script must guard it
        # with a gate-vs-judge distinction; otherwise it's a
        # regression.
        blame_judge_lines = re.findall(
            r"DESCRIPTION=\"[^\"]*AI judge failure[^\"]*\"",
            self.run,
        )
        if not blame_judge_lines:
            return  # no problem — script doesn't use the old wording
        # If the old wording is still present, it must be guarded so
        # it only fires when the judge (not the gate) is the source.
        # A simple heuristic: each such line must be near a
        # judge-specific check (e.g. `JUDGE_CONCL != Approve` or
        # `[ "$REVIEW_CONCL" != "success" ]` immediately above).
        for m in re.finditer(
            r"DESCRIPTION=\"[^\"]*AI judge failure[^\"]*\"",
            self.run,
        ):
            # Look at the preceding ~10 lines for the gate-vs-judge
            # distinction guard.
            pre_start = max(0, m.start() - 800)
            pre = self.run[pre_start:m.start()]
            guard_present = (
                "judge" in pre.lower()
                and ("gate" in pre.lower() or "GATE_CONCL" in pre)
            )
            self.assertTrue(
                guard_present,
                "the 'AI judge failure' wording must be guarded by "
                "a gate-vs-judge distinction so a gate failure "
                "isn't blamed on the judge — issue #803. "
                "Preceding context:\n" + pre[-400:],
            )


class TestForkPrReviewPerJudgeStatusesIncludeGate(unittest.TestCase):
    """Pin that the `Post per-judge commit statuses` step also posts
    a commit status for the `severity gate (review + security)` job,
    so a gate failure surfaces as its own check on the PR's Checks
    tab (not only as the combined status description)."""

    def setUp(self) -> None:
        self.step = _step_by_name("Post per-judge commit statuses")
        self.run = self.step.get("run", "")

    def test_per_judge_loop_includes_gate_job(self) -> None:
        """The per-judge loop's NAME list must include `severity gate
        (review + security)` so a gate failure surfaces as its own
        check (issue #803)."""
        self.assertRegex(
            self.run,
            r"severity gate \(review \+ security\)",
            "the `Post per-judge commit statuses` step must include "
            "`severity gate (review + security)` in its NAME list "
            "so a gate failure surfaces as its own check — issue #803",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
