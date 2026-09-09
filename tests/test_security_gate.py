#!/usr/bin/env python3
"""test_security_gate.py — Regression tests for the security.yml severity gate.

Issue #823 split the bundled review.yml into review.yml + security.yml;
each now owns its own single-judge severity gate. This file covers the
security.yml gate (10-dim OWASP A01–A10 + LLM01). The parallel
review.yml gate has its own test_review_gate.py.

The contract mirrors the review gate:
  - Empty S with agent_ran=true → default to Approve + ::warning::, exit 0.
  - S=Changes Requested/Blocked → exit 1.
  - agent_ran=false → hard-fail with remediation (issue #212-C1).
  - agent_ran=false + verdict_source=needs-fallback-bootstrap-pr →
    tolerate the skip (issue #726, single-judge version).
  - S=PARSE_FAILED → hard-fail with parse-error annotation (issue #397).
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
# Issue #823: security.yml is security-only. The gate reads S + S_AGENT +
# S_SOURCE + S_RESULT from the security job's outputs.
GATE_SNIPPET = (REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "security.yml").read_text()


def _extract_gate_bash() -> str:
    """Extract the `Security verdict gate` step's bash body."""
    lines = GATE_SNIPPET.splitlines()
    step_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "- name: Security verdict gate":
            step_idx = i
            break
    if step_idx is None:
        raise RuntimeError("Security verdict gate step not found in security.yml")
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
    s: str,
    event: str = "pull_request",
    s_agent: str = "true",
    s_result: str = "success",
    s_source: str = "",
) -> subprocess.CompletedProcess:
    """Execute the security gate bash with S + EVENT_NAME + agent_ran + result."""
    bash = _extract_gate_bash()
    bash = bash.replace('S="${{ needs.security.outputs.verdict }}"', 'S="${S_OVERRIDE:-}"')
    bash = bash.replace(
        'S_AGENT="${{ needs.security.outputs.agent_ran }}"',
        'S_AGENT="${S_AGENT_OVERRIDE:-true}"',
    )
    bash = bash.replace(
        'S_SOURCE="${{ needs.security.outputs.verdict_source }}"',
        'S_SOURCE="${S_SOURCE_OVERRIDE:-}"',
    )
    bash = bash.replace(
        'S_RESULT="${{ needs.security.result }}"',
        'S_RESULT="${S_RESULT_OVERRIDE:-success}"',
    )
    bash = bash.replace('EVENT="$EVENT_NAME"', 'EVENT="${EVENT_OVERRIDE:-pull_request}"')
    env = os.environ.copy()
    env["S_OVERRIDE"] = s
    env["S_AGENT_OVERRIDE"] = s_agent
    env["S_SOURCE_OVERRIDE"] = s_source
    env["S_RESULT_OVERRIDE"] = s_result
    env["EVENT_OVERRIDE"] = event
    return subprocess.run(
        ["bash", "-c", bash],
        capture_output=True, text=True, env=env, timeout=10,
    )


class TestSecurityGateTolerance(unittest.TestCase):

    def test_pull_request_empty_S_defaults_to_approve(self):
        cp = _run_gate(s="", event="pull_request")
        self.assertEqual(
            cp.returncode, 0,
            f"empty S with agents_ran MUST default to Approve + ::warning::.\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        self.assertIn("::warning::security verdict missing", cp.stdout)

    def test_pull_request_approve_exits_zero(self):
        cp = _run_gate(s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Security verdict: Approve", cp.stdout)

    def test_pull_request_changes_requested_exits_one(self):
        cp = _run_gate(s="Changes Requested", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::Changes Requested", cp.stdout)

    def test_pull_request_blocked_exits_one(self):
        cp = _run_gate(s="Blocked", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::Blocked", cp.stdout)

    def test_pull_request_unparseable_verdict_exits_zero(self):
        cp = _run_gate(s="Requested", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Unparseable security verdict", cp.stdout)

    def test_workflow_dispatch_empty_S_exits_zero(self):
        cp = _run_gate(s="", event="workflow_dispatch")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("::warning::security verdict missing", cp.stdout)

    # === Issue #212-C1-fix: agent skip detection ===

    def test_security_agent_skipped_hard_fails(self):
        cp = _run_gate(s="", event="pull_request", s_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::security gate: AI agent was skipped (non-bootstrap)", combined)
        self.assertIn("anthropics/claude-code-action@", combined)
        self.assertIn("refused to run", combined)

    def test_security_agent_skipped_with_verdict_still_hard_fails(self):
        cp = _run_gate(s="Approve", event="pull_request", s_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("AI agent was skipped", cp.stdout + cp.stderr)

    def test_security_bootstrap_falls_through(self):
        cp = _run_gate(
            s="Approve",
            event="pull_request",
            s_agent="false",
            s_source="needs-fallback-bootstrap-pr",
        )
        self.assertEqual(
            cp.returncode, 0,
            f"security bootstrap must fall through (was hard-fail).\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        combined = cp.stdout + cp.stderr
        self.assertIn("::notice::bootstrap-PR fallback: security agent skipped", combined)
        self.assertNotIn("::error::", combined)

    def test_security_default_approve_no_file_does_not_emit_bootstrap_remediation(self):
        cp = _run_gate(
            s="",
            event="pull_request",
            s_agent="false",
            s_source="default-approve-no-file",
        )
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("AI agent was skipped (non-bootstrap)", combined)
        self.assertNotIn("Merge this PR's workflow changes to main", combined)

    def test_agent_skip_no_source_hard_fails(self):
        cp = _run_gate(s="", event="pull_request", s_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("AI agent was skipped (non-bootstrap)", combined)

    def test_agent_skip_message_action_ref_survives_sha_pin(self):
        cp = _run_gate(s="", event="pull_request", s_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertRegex(
            cp.stdout + cp.stderr,
            r"anthropics/claude-code-action@(?:[0-9a-f]{6,}\s+#\s+v1|v1)\b",
        )

    # === Issue #397: PARSE_FAILED sentinel hard-fail ===

    def test_security_PARSE_FAILED_hard_fails(self):
        cp = _run_gate(s="PARSE_FAILED", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::security gate: verdict parser failed", combined)
        self.assertIn("security.verdict=PARSE_FAILED", combined)
        self.assertNotIn("::warning::Unparseable", combined)

    def test_PARSE_FAILED_in_workflow_dispatch_also_hard_fails(self):
        cp = _run_gate(s="PARSE_FAILED", event="workflow_dispatch")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::security gate: verdict parser failed", cp.stdout + cp.stderr)

    def test_extracted_bash_is_nonempty(self):
        bash = _extract_gate_bash()
        self.assertIn("S=", bash)
        self.assertIn("S_AGENT=", bash)
        # Verify the security gate does NOT read the review verdict via
        # needs.<job>.outputs (the canonical pattern). Other 'R=' substrings
        # in prose / variable names like CI_REVIEW_PROVIDER are not signals.
        bash_no_comments = "\n".join(
            ln for ln in bash.splitlines() if not ln.lstrip().startswith("#")
        )
        self.assertNotIn(
            'needs.review.outputs.verdict',
            bash_no_comments,
            "security.yml gate must NOT reference needs.review.outputs",
        )
        self.assertNotIn("run:", bash)


class TestSecurityJobOutputs(unittest.TestCase):
    """The security job MUST declare `verdict_source` in `outputs:` so the
    gate's source-split (issue #625) is reachable."""

    def test_security_job_declares_verdict_source(self) -> None:
        import yaml

        workflow_path = REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "security.yml"
        self.assertTrue(workflow_path.is_file())
        with workflow_path.open() as f:
            wf = yaml.safe_load(f)

        self.assertIn("jobs", wf)
        jobs = wf["jobs"]
        self.assertIn("security", jobs, "security job must exist")
        self.assertNotIn("review", jobs, "security.yml must NOT contain the review job (#823 split)")

        job = jobs["security"]
        self.assertIn("outputs", job)
        outputs = job["outputs"]
        self.assertIn("verdict_source", outputs)
        self.assertIn("extract_verdict.outputs.verdict_source", outputs["verdict_source"])


if __name__ == "__main__":
    unittest.main()
