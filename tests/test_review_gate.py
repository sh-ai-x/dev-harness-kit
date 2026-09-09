#!/usr/bin/env python3
"""test_review_gate.py — Regression tests for the review.yml severity gate.

Issue #823 split the bundled review.yml into review.yml + security.yml;
each now owns its own single-judge severity gate. This file covers the
review.yml gate (3-dim correctness + reuse/simplification only). The
parallel security.yml gate has its own test_security_gate.py.

The review gate tolerates an empty R with a `::warning::` when the agent
ran (default-Approve fallback). When anthropics/claude-code-action@v1 was
SKIPPED, the previous tolerance silently defaulted to Approve — the
exact symptom this test suite must guard against: agent_ran=false is the
unambiguous signal that the verdict is meaningless, and the gate must
hard-fail in that case regardless of event mode.

Real review feedback (Changes Requested / Blocked) still exits 1.
Unparseable verdicts (e.g. "Requested" truncation) exit 0 + ::warning::.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
# Issue #823: review.yml is review-only. The gate reads R + R_AGENT +
# R_SOURCE + R_RESULT from the review job's outputs.
GATE_SNIPPET = (REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "review.yml").read_text()


def _extract_gate_bash() -> str:
    """Extract the `Review verdict gate` step's bash body.

    Looks for `      - name: Review verdict gate` followed by `        run: |`
    and captures every subsequent line indented under the run block.
    """
    lines = GATE_SNIPPET.splitlines()
    step_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "- name: Review verdict gate":
            step_idx = i
            break
    if step_idx is None:
        raise RuntimeError("Review verdict gate step not found in review.yml")
    body_start = None
    for j in range(step_idx, min(step_idx + 5, len(lines))):
        if lines[j].lstrip().startswith("run:"):
            body_start = j + 1
            break
    if body_start is None:
        raise RuntimeError("`run:` not found in gate step")
    indent = None
    for j in range(body_start, len(lines)):
        if lines[j].strip():
            indent = len(lines[j]) - len(lines[j].lstrip())
            break
    if indent is None:
        raise RuntimeError("empty gate body")
    body = []
    for j in range(body_start, len(lines)):
        if not lines[j].strip():
            body.append("")
            continue
        if len(lines[j]) - len(lines[j].lstrip()) < indent:
            break
        body.append(lines[j][indent:])
    return "\n".join(body).rstrip() + "\n"


def _run_gate(
    r: str,
    event: str = "pull_request",
    r_agent: str = "true",
    r_result: str = "success",
    r_source: str = "",
) -> subprocess.CompletedProcess:
    """Execute the review gate bash with R + EVENT_NAME + agent_ran + result.

    Defaults model the "happy CI" state (agents ran, no failures).
    """
    bash = _extract_gate_bash()
    bash = bash.replace('R="${{ needs.review.outputs.verdict }}"', 'R="${R_OVERRIDE:-}"')
    bash = bash.replace(
        'R_AGENT="${{ needs.review.outputs.agent_ran }}"',
        'R_AGENT="${R_AGENT_OVERRIDE:-true}"',
    )
    bash = bash.replace(
        'R_SOURCE="${{ needs.review.outputs.verdict_source }}"',
        'R_SOURCE="${R_SOURCE_OVERRIDE:-}"',
    )
    bash = bash.replace(
        'R_RESULT="${{ needs.review.result }}"',
        'R_RESULT="${R_RESULT_OVERRIDE:-success}"',
    )
    bash = bash.replace('EVENT="$EVENT_NAME"', 'EVENT="${EVENT_OVERRIDE:-pull_request}"')
    env = os.environ.copy()
    env["R_OVERRIDE"] = r
    env["R_AGENT_OVERRIDE"] = r_agent
    env["R_SOURCE_OVERRIDE"] = r_source
    env["R_RESULT_OVERRIDE"] = r_result
    env["EVENT_OVERRIDE"] = event
    return subprocess.run(
        ["bash", "-c", bash],
        capture_output=True, text=True, env=env, timeout=10,
    )


class TestReviewGateTolerance(unittest.TestCase):
    """Issue #823: review.yml is review-only. Empty R defaults to Approve
    + ::warning:: when the agent actually ran."""

    def test_pull_request_empty_R_defaults_to_approve(self):
        cp = _run_gate(r="", event="pull_request")
        self.assertEqual(
            cp.returncode, 0,
            f"empty R with agents_ran MUST default to Approve + ::warning::.\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        self.assertIn("::warning::review verdict missing", cp.stdout)

    def test_pull_request_approve_exits_zero(self):
        cp = _run_gate(r="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Review verdict: Approve", cp.stdout)

    def test_pull_request_changes_requested_exits_one(self):
        cp = _run_gate(r="Changes Requested", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::Changes Requested", cp.stdout)

    def test_pull_request_blocked_exits_one(self):
        cp = _run_gate(r="Blocked", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::Blocked", cp.stdout)

    def test_pull_request_unparseable_verdict_exits_zero(self):
        cp = _run_gate(r="Requested", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Unparseable review verdict", cp.stdout)

    def test_workflow_dispatch_empty_R_exits_zero(self):
        cp = _run_gate(r="", event="workflow_dispatch")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("::warning::review verdict missing", cp.stdout)

    # === Issue #212-C1-fix: agent skip detection ===

    def test_review_agent_skipped_hard_fails(self):
        cp = _run_gate(r="", event="pull_request", r_agent="false")
        self.assertEqual(
            cp.returncode, 1,
            f"agent_ran=false MUST hard-fail.\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review gate: AI agent was skipped (non-bootstrap)", combined)
        self.assertIn("anthropics/claude-code-action@", combined)
        self.assertIn("refused to run", combined)

    def test_review_agent_skipped_with_verdict_still_hard_fails(self):
        """agent_ran=false + verdict=Approve still hard-fails (stale verdict)."""
        cp = _run_gate(r="Approve", event="pull_request", r_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("AI agent was skipped", cp.stdout + cp.stderr)

    def test_review_bootstrap_falls_through(self):
        """Issue #726 single-judge: bootstrap-PR (agent_ran=false + source =
        needs-fallback-bootstrap-pr) tolerates the skip and falls through
        to the rank/case logic on the synthesized Approve."""
        cp = _run_gate(
            r="Approve",
            event="pull_request",
            r_agent="false",
            r_source="needs-fallback-bootstrap-pr",
        )
        self.assertEqual(
            cp.returncode, 0,
            f"review bootstrap must fall through (was hard-fail).\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        combined = cp.stdout + cp.stderr
        self.assertIn("::notice::bootstrap-PR fallback: review agent skipped", combined)
        self.assertNotIn("::error::", combined)

    def test_review_default_approve_no_file_does_not_emit_bootstrap_remediation(self):
        cp = _run_gate(
            r="",
            event="pull_request",
            r_agent="false",
            r_source="default-approve-no-file",
        )
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("AI agent was skipped (non-bootstrap)", combined)
        self.assertNotIn("Merge this PR's workflow changes to main", combined)

    def test_agent_skip_no_source_hard_fails(self):
        """Empty source falls through to install-broken hard-fail."""
        cp = _run_gate(r="", event="pull_request", r_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("AI agent was skipped (non-bootstrap)", combined)

    def test_agent_skip_message_action_ref_survives_sha_pin(self):
        """Issue #732: agent-skip remediation must survive action SHA pinning."""
        cp = _run_gate(r="", event="pull_request", r_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertRegex(
            combined,
            r"anthropics/claude-code-action@(?:[0-9a-f]{6,}\s+#\s+v1|v1)\b",
        )

    # === Issue #397: PARSE_FAILED sentinel hard-fail ===

    def test_review_PARSE_FAILED_hard_fails(self):
        cp = _run_gate(r="PARSE_FAILED", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review gate: verdict parser failed", combined)
        self.assertIn("review.verdict=PARSE_FAILED", combined)
        self.assertNotIn("::warning::Unparseable", combined)

    def test_PARSE_FAILED_in_workflow_dispatch_also_hard_fails(self):
        cp = _run_gate(r="PARSE_FAILED", event="workflow_dispatch")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::review gate: verdict parser failed", cp.stdout + cp.stderr)

    def test_extracted_bash_is_nonempty(self):
        bash = _extract_gate_bash()
        self.assertIn("R=", bash)
        self.assertIn("R_AGENT=", bash, "R_AGENT env var must be referenced in gate")
        bash_no_comments = "\n".join(
            ln for ln in bash.splitlines() if not ln.lstrip().startswith("#")
        )
        self.assertNotIn(
            'needs.security.outputs.verdict',
            bash_no_comments,
            "review.yml gate must NOT reference needs.security.outputs",
        )
        self.assertNotIn("run:", bash, "extractor must strip the run: | header")


class TestReviewJobOutputs(unittest.TestCase):
    """The review job MUST declare `verdict_source` in `outputs:` so the
    gate's source-split (issue #625) is reachable."""

    def test_review_job_declares_verdict_source(self) -> None:
        import yaml

        workflow_path = REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "review.yml"
        self.assertTrue(workflow_path.is_file())
        with workflow_path.open() as f:
            wf = yaml.safe_load(f)

        self.assertIn("jobs", wf)
        jobs = wf["jobs"]
        self.assertIn("review", jobs, "review job must exist")
        self.assertNotIn("security", jobs, "review.yml must NOT contain the security job (#823 split)")

        job = jobs["review"]
        self.assertIn("outputs", job)
        outputs = job["outputs"]
        self.assertIn("verdict_source", outputs)
        self.assertIn("extract_verdict.outputs.verdict_source", outputs["verdict_source"])


if __name__ == "__main__":
    unittest.main()
