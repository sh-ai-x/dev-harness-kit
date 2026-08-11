#!/usr/bin/env python3
"""test_review_gate.py — Regression tests for the severity-gate tolerance.

The severity gate (the `Combined verdict gate` step in
.github/workflows/review.yml) used to hard-fail in pull_request mode when
either review or security verdict was empty. The fix defaults both to
Approve + ::warning:: regardless of event mode, on the theory that the
human gate (REVIEW_REQUIRED / CHANGES_REQUESTED on the PR) is what
actually blocks merge -- not a single missing agent verdict.

These tests extract the gate bash from review.yml and execute it via
subprocess with controlled R/S/EVENT env vars. They protect against:

  - Pull_request mode with empty R, empty S: must exit 0 (was exit 1)
  - Pull_request mode with empty R, non-empty S: must exit 0 (was exit 1)
  - Pull_request mode with non-empty R, empty S: must exit 0 (was exit 1)
  - Pull_request mode with both Approve: exit 0
  - Pull_request mode with Changes Requested worst-of: exit 1
  - Pull_request mode with Blocked worst-of: exit 1
  - Workflow_dispatch mode with empty R: defaults to Approve, exit 0
  - Unparseable verdict (e.g. "Requested"): ::warning:: + exit 0
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
# Source-of-truth: the consumer template SSOT (templates/ci/.github/workflows/review.yml).
# The local .github/workflows/review.yml is kept in lockstep with the template, but the
# template is what ships to consumers via /dev-kit:ci-setup, so it is the canonical source.
GATE_SNIPPET = (REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "review.yml").read_text()


def _extract_gate_bash() -> str:
    """Extract the `Combined verdict gate` step's bash body.

    Looks for `      - name: Combined verdict gate` followed by `        run: |`
    and captures every subsequent line indented under the run block.
    """
    lines = GATE_SNIPPET.splitlines()
    # Find the gate step header.
    step_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "- name: Combined verdict gate":
            step_idx = i
            break
    if step_idx is None:
        raise RuntimeError("Combined verdict gate step not found")
    # The bash body starts after the `run: |` line.
    body_start = None
    for j in range(step_idx, min(step_idx + 5, len(lines))):
        if lines[j].lstrip().startswith("run:"):
            body_start = j + 1
            break
    if body_start is None:
        raise RuntimeError("`run:` not found in gate step")
    # Find the common indent (first non-empty body line).
    indent = None
    for j in range(body_start, len(lines)):
        if lines[j].strip():
            indent = len(lines[j]) - len(lines[j].lstrip())
            break
    if indent is None:
        raise RuntimeError("empty gate body")
    # Collect lines while they stay at or beyond `indent`.
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
    s: str,
    event: str = "pull_request",
    r_agent: str = "true",
    s_agent: str = "true",
    r_result: str = "success",
    s_result: str = "success",
) -> subprocess.CompletedProcess:
    """Execute the gate bash with R, S, EVENT_NAME, agent_ran, result env vars.

    Defaults model the "happy CI" state (agents ran, no failures), so existing
    tests that pre-date the agent_ran = false hard-fail exercise the original
    tolerance without false positives. New tests for the bootstrap-PR / silent-
    skip case override `r_agent` or `s_agent` to "false".
    """
    bash = _extract_gate_bash()
    # Strip the `needs.<job>.outputs.X` interpolation lines -- they're
    # GitHub Actions expressions, not real bash. Replace with env-driven values.
    bash = bash.replace('R="${{ needs.review.outputs.verdict }}"', 'R="${R_OVERRIDE:-}"')
    bash = bash.replace('S="${{ needs.security.outputs.verdict }}"', 'S="${S_OVERRIDE:-}"')
    bash = bash.replace(
        'R_AGENT="${{ needs.review.outputs.agent_ran }}"',
        'R_AGENT="${R_AGENT_OVERRIDE:-true}"',
    )
    bash = bash.replace(
        'S_AGENT="${{ needs.security.outputs.agent_ran }}"',
        'S_AGENT="${S_AGENT_OVERRIDE:-true}"',
    )
    bash = bash.replace(
        'R_RESULT="${{ needs.review.result }}"',
        'R_RESULT="${R_RESULT_OVERRIDE:-success}"',
    )
    bash = bash.replace(
        'S_RESULT="${{ needs.security.result }}"',
        'S_RESULT="${S_RESULT_OVERRIDE:-success}"',
    )
    bash = bash.replace('EVENT="$EVENT_NAME"', 'EVENT="${EVENT_OVERRIDE:-pull_request}"')
    env = os.environ.copy()
    env["R_OVERRIDE"] = r
    env["S_OVERRIDE"] = s
    env["R_AGENT_OVERRIDE"] = r_agent
    env["S_AGENT_OVERRIDE"] = s_agent
    env["R_RESULT_OVERRIDE"] = r_result
    env["S_RESULT_OVERRIDE"] = s_result
    env["EVENT_OVERRIDE"] = event
    return subprocess.run(
        ["bash", "-c", bash],
        capture_output=True, text=True, env=env, timeout=10,
    )


class TestSeverityGateTolerance(unittest.TestCase):
    """The contract: empty R or S defaults to Approve + ::warning:: in both
    event modes WHEN agents actually ran. Pre-#44 the gate hard-failed on
    missing verdicts, which broke any PR whose agent skipped (workflow-
    validation skip on the very PR that ADDS .github/workflows/review.yml,
    action rate-limit, transient network error). The fix mirrors the
    project's own .github/workflows/review.yml (5d6c53e): the human gate
    (REVIEW_REQUIRED / CHANGES_REQUESTED on the PR) is what actually blocks
    merge, not a single missing agent verdict.

    BUT (issue #212-C1-fix): when anthropics/claude-code-action@v1 was
    SKIPPED (a 0 claude-comment count means the action never ran, even
    though the job's `result` is "success"), the previous tolerance
    silently defaulted to Approve. That's the exact symptom this test
    suite must guard against: agent_ran=false is the unambiguous signal
    that the verdict is meaningless, and the gate must hard-fail in that
    case regardless of event mode.

    Real review feedback (Changes Requested / Blocked) still exits 1.
    Unparseable verdicts (e.g. "Requested" truncation) exit 0 + ::warning::.
    """

    def test_pull_request_empty_R_empty_S_defaults_to_approve(self):
        """Empty R AND empty S (with agents ran): default both to Approve + ::warning::, exit 0."""
        cp = _run_gate(r="", s="", event="pull_request")
        self.assertEqual(
            cp.returncode, 0,
            f"empty R/S with agents_ran MUST default to Approve + ::warning:: (was hard-fail).\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        self.assertIn("::warning::review verdict missing", cp.stdout)
        self.assertIn("::warning::security verdict missing", cp.stdout)

    def test_pull_request_empty_R_nonempty_S_exits_zero(self):
        cp = _run_gate(r="", s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("::warning::review verdict missing", cp.stdout)

    def test_pull_request_nonempty_R_empty_S_exits_zero(self):
        cp = _run_gate(r="Approve", s="", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("::warning::security verdict missing", cp.stdout)

    def test_pull_request_both_approve_exits_zero(self):
        cp = _run_gate(r="Approve", s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Combined worst verdict: Approve", cp.stdout)

    def test_pull_request_changes_requested_exits_one(self):
        """Real review feedback still blocks: this is not a free-pass."""
        cp = _run_gate(r="Changes Requested", s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::Changes Requested", cp.stdout)

    def test_pull_request_blocked_exits_one(self):
        cp = _run_gate(r="Blocked", s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::Blocked", cp.stdout)

    def test_pull_request_unparseable_verdict_exits_zero(self):
        """Unparseable verdicts (e.g. 'Requested' truncation) are non-blocking.

        The strict-gate contract previously hard-failed on unparseable verdicts,
        which broke consumer repos whose agents emitted a non-standard verdict
        string. Treat as non-blocking + ::warning::, mirroring the
        workflow_dispatch tolerance in project's own .github/workflows/review.yml.
        """
        cp = _run_gate(r="Requested", s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Unparseable verdict", cp.stdout)

    def test_workflow_dispatch_empty_R_exits_zero(self):
        """workflow_dispatch mode: empty R defaults to Approve + ::warning::."""
        cp = _run_gate(r="", s="Approve", event="workflow_dispatch")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("::warning::review verdict missing", cp.stdout)

    # === Issue #212-C1-fix: agent skip detection ===

    def test_review_agent_skipped_hard_fails(self):
        """anthropics/claude-code-action@v1 skipped (PR touches .github/workflows/*).

        The action exits 0 with a "workflow validation skip" warning; the job's
        `result` is `success` (exit-0 mask). The previous gate saw verdict=empty
        + result=success and silently defaulted to Approve. Issue #212-C1-fix:
        the new `agent_ran=false` signal hard-fails with a remediation message.
        The remediation message is written to stderr so GH Actions UI shows it
        as an annotation -- that's why the assertion checks both streams.
        """
        cp = _run_gate(r="", s="Approve", event="pull_request", r_agent="false")
        self.assertEqual(
            cp.returncode, 1,
            f"agent_ran=false MUST hard-fail (was Approve default).\nstdout={cp.stdout}\nstderr={cp.stderr}",
        )
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review+security gate: AI agent was skipped", combined)
        self.assertIn("anthropics/claude-code-action@", combined)
        self.assertIn("refused to run", combined)

    def test_security_agent_skipped_hard_fails(self):
        cp = _run_gate(r="Approve", s="", event="pull_request", s_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review+security gate: AI agent was skipped", combined)

    def test_both_agents_skipped_hard_fails(self):
        cp = _run_gate(r="", s="", event="pull_request", r_agent="false", s_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        # Even with both verdicts empty, the "agent skipped" check fires
        # BEFORE the empty-verdict tolerance to surface the bootstrap-PR
        # scenario specifically (issue #212-C1).
        self.assertIn("AI agent was skipped", combined)
        # The empty-verdict warnings are SKIPPED in favor of the hard-fail
        # so the user gets a single, unambiguous error.
        self.assertNotIn("::warning::review verdict missing", combined)

    def test_agent_skip_with_real_verdicts_still_hard_fails(self):
        """agent_ran=false + verdict=Approve still hard-fails.

        A real verdict on a skipped run is impossible (the action never ran
        to produce a comment). But in practice a previous run's track_progress
        comment may still be on the PR — that comment is stale w.r.t. the
        current diff. Hard-fail regardless of verdict to avoid attributing
        old analysis to a new run.
        """
        cp = _run_gate(r="Approve", s="Approve", event="pull_request", r_agent="false")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("AI agent was skipped", cp.stdout + cp.stderr)

    # === Issue #397: PARSE_FAILED sentinel hard-fail ===

    def test_review_PARSE_FAILED_hard_fails(self):
        """R=PARSE_FAILED (review agent ran, parser returned parse-failure sentinel).

        The parser emits `PARSE_FAILED` when the agent's output file existed
        but contained no recognizable Verdict line. That is NOT the same as
        "no verdict at all" — the agent ran but its output shape doesn't
        match what the parser expects (e.g. a wrapper changed the JSON
        envelope, the agent posted an inline comment but never a
        `**Verdict:**` summary, the output was truncated).

        Per issue #397, this MUST hard-fail with a parse-error annotation,
        NOT fall into the unparseable-verdict tolerance (which would exit 0
        and mask the broken parser as a passing review).
        """
        cp = _run_gate(r="PARSE_FAILED", s="Approve", event="pull_request")
        self.assertEqual(
            cp.returncode, 1,
            f"PARSE_FAILED MUST hard-fail (was unparseable-verdict tolerance).\n"
            f"stdout={cp.stdout}\nstderr={cp.stderr}",
        )
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review+security gate: verdict parser failed", combined)
        self.assertIn("review.verdict=PARSE_FAILED", combined)
        self.assertIn("security.verdict=Approve", combined)
        # Must NOT be a tolerance pass
        self.assertNotIn("::warning::Unparseable verdict", combined)

    def test_security_PARSE_FAILED_hard_fails(self):
        cp = _run_gate(r="Approve", s="PARSE_FAILED", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review+security gate: verdict parser failed", combined)
        self.assertIn("review.verdict=Approve", combined)
        self.assertIn("security.verdict=PARSE_FAILED", combined)

    def test_both_PARSE_FAILED_hard_fails(self):
        cp = _run_gate(r="PARSE_FAILED", s="PARSE_FAILED", event="pull_request")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        combined = cp.stdout + cp.stderr
        self.assertIn("::error::review+security gate: verdict parser failed", combined)
        # Must mention both axes (not just one)
        self.assertIn("review.verdict=PARSE_FAILED", combined)
        self.assertIn("security.verdict=PARSE_FAILED", combined)

    def test_PARSE_FAILED_in_workflow_dispatch_also_hard_fails(self):
        """Even on manual dispatch, PARSE_FAILED is a parser bug, not a tolerance case.

        The tolerance on workflow_dispatch was meant for human-judgement
        overrides; a parse failure is a code defect that must surface
        regardless of event mode.
        """
        cp = _run_gate(r="PARSE_FAILED", s="Approve", event="workflow_dispatch")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("::error::review+security gate: verdict parser failed", cp.stdout + cp.stderr)

    def test_other_unparseable_still_tolerates(self):
        """Other unparseable values (e.g. 'Requested') still hit the existing tolerance.

        The fix for #397 is narrow: only the PARSE_FAILED sentinel (which
        means 'parser couldn't extract a verdict from the agent's output
        file') becomes a hard fail. Other unparseable values like a
        truncated 'Requested' are still tolerated as non-blocking per
        the pre-existing behavior, because they may originate from agent
        output that the human reviewer can interpret.
        """
        cp = _run_gate(r="Requested", s="Approve", event="pull_request")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Unparseable verdict", cp.stdout)

    def test_extracted_bash_is_nonempty(self):
        """Sanity: the extractor actually returns bash, not a header."""
        bash = _extract_gate_bash()
        self.assertIn("R=", bash)
        self.assertIn("S=", bash)
        self.assertIn("EVENT=", bash)
        self.assertIn("R_AGENT=", bash, "R_AGENT env var must be referenced in gate")
        self.assertIn("S_AGENT=", bash, "S_AGENT env var must be referenced in gate")
        self.assertNotIn("run:", bash, "extractor must strip the run: | header")


if __name__ == "__main__":
    unittest.main()
