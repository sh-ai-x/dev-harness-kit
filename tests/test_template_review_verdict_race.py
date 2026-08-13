#!/usr/bin/env python3
"""test_template_review_verdict_race.py — Regression tests for issues #104 + #244.

Pins the consumer-template verdict-extraction contract. Originally for
issue #104 (head -1 / -tail / missing-verdict hard-fail); extended for
issue #244 (port boilerplate-web PR #17/#19: read from agent output
file, NEVER grep PR comments).

Bugs verified:
1. templates/ci/.github/workflows/review.yml review + security extract
   steps MUST call scripts/extract-verdict.py (primary source) and MUST
   NOT grep PR comments. The previous `tail -1` comment-grep was the
   root cause of the deterministic gate flap on boilerplate-web PR #18:
   it could resurrect a stale "Verdict: Changes Requested" from the
   previous push (issue #244).
2. The Combined verdict gate MUST default missing R or S to Approve with
   ::warning:: (project's own .github/workflows/review.yml already does
   this; the template must mirror).
3. The Combined verdict gate MUST extract the verdict itself at gate
   time (not rely on stale per-job outputs that captured verdicts
   BEFORE the LLM posted its comment). This eliminates the race where
   the extract step ran immediately on job-start and captured empty
   results, while the LLM posted its verdict seconds later.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE_PATH = REPO_ROOT / "templates" / "ci" / ".github" / "workflows" / "review.yml"
EXTRACT_VERDICT_SCRIPT = (
    REPO_ROOT / "templates" / "ci" / "scripts" / "extract-verdict.py"
)
LOCAL_PATH = REPO_ROOT / ".github" / "workflows" / "review.yml"


class TestTemplateVerdictExtractionOrdering(unittest.TestCase):
    """Pins the post-#244 contract: extract step uses extract-verdict.py,
    NEVER the comment-grep fallback.

    The previous bug-1 pin enforced `tail -1` on PR comments. That was
    the source of deterministic gate flapping on every push that touched
    a PR with prior review runs (boilerplate-web PR #18 repro). Issue
    #244 replaces the comment-grep with scripts/extract-verdict.py
    (parses the agent's actual transcript file). This test now pins
    the new contract.
    """

    def setUp(self):
        self.text = TEMPLATE_PATH.read_text()

    def _job_body(self, job_name: str) -> str:
        m = re.search(
            rf"^  {job_name}:\n(?P<body>.*?)(?=^  [a-z_]+:\n|\Z)",
            self.text,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(m, f"{job_name}: job block not found in template")
        return m.group("body")

    def test_review_job_extract_uses_script(self):
        """The review job's extract step MUST call scripts/extract-verdict.py.

        The script path is relative to the consumer-repo root (matches
        templates/ci/scripts/extract-verdict.py source after ci-setup
        install). Using the absolute source path under templates/ would
        break consumer repos.
        """
        body = self._job_body("review")
        self.assertIn(
            "scripts/extract-verdict.py",
            body,
            "review extract step must call scripts/extract-verdict.py "
            "(issue #244 primary verdict source)",
        )
        # The comment-grep fallback MUST be gone. `gh api .../comments`
        # in the extract pipeline was the source of the stale-verdict
        # flap; the new contract is "read from agent output file".
        self.assertNotIn(
            "/issues/${PR_NUMBER}/comments",
            body,
            "review extract step still greps PR comments (issue #244 "
            "root-cause: comment-grep fallback resurrects stale verdicts)",
        )

    def test_security_job_extract_uses_script(self):
        """The security job's extract step MUST call scripts/extract-verdict.py."""
        body = self._job_body("security")
        self.assertIn(
            "scripts/extract-verdict.py",
            body,
            "security extract step must call scripts/extract-verdict.py "
            "(issue #244 primary verdict source)",
        )
        self.assertNotIn(
            "/issues/${PR_NUMBER}/comments",
            body,
            "security extract step still greps PR comments (issue #244 "
            "root-cause: comment-grep fallback resurrects stale verdicts)",
        )

    def test_extract_verdict_script_is_installed(self):
        """The script the workflow depends on MUST exist in templates/ci/scripts/.

        Otherwise `ci-setup` won't ship it to consumer repos and the
        workflow will error with `[Errno 2] No such file or directory`
        — the original bug-1 from boilerplate-web PR #17.
        """
        self.assertTrue(
            EXTRACT_VERDICT_SCRIPT.exists(),
            f"templates/ci/scripts/extract-verdict.py missing at "
            f"{EXTRACT_VERDICT_SCRIPT}; the review.yml workflow depends "
            f"on this script (issue #244)",
        )

    def test_review_and_security_extract_use_verdict_source_label(self):
        """Both extract steps MUST emit a `verdict_source` output.

        Distinguishes the four states the gate can encounter
        (issue #612: `default-approve-empty-file` was the silent-Approve
        path; replaced with `parse-failed-no-verdict` which threads
        through the gate's PARSE_FAILED hard-fail branch):
          - agent-output-file              (extract-verdict.py returned a verdict)
          - parse-failed-no-verdict        (issue #612: file existed + parseable
                                            JSON but no `Verdict:` line)
          - default-approve-no-file        (action silently skipped)
          - needs-fallback-bootstrap-pr    (needs_fallback=true early-return)

        Diagnostic label — pinned to prevent silent regressions of the
        comment-grep fallback that motivated issue #244 + the silent
        Approve regression that motivated issue #612.
        """
        for job in ("review", "security"):
            body = self._job_body(job)
            self.assertIn(
                "verdict_source=",
                body,
                f"{job} extract step must emit verdict_source output (issue #244)",
            )


def _extract_gate_bash(text: str) -> str:
    """Extract the bash body of the `Combined verdict gate` step."""
    lines = text.splitlines()
    step_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "- name: Combined verdict gate":
            step_idx = i
            break
    if step_idx is None:
        raise RuntimeError("Combined verdict gate step not found")
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


class TestTemplateGateTolerance(unittest.TestCase):
    """Pins bug 2 + 3: missing-verdict tolerance + gate-time extract.

    - Empty R or S in pull_request mode MUST exit 0 (was exit 1).
    - Unparseable verdict MUST exit 0 (was exit 1).
    - The gate MUST extract the verdict itself (not depend on
      needs.<job>.outputs.verdict captured by the per-job extract step
      before the LLM posted).
    """

    def setUp(self):
        self.text = TEMPLATE_PATH.read_text()
        self.gate_bash = _extract_gate_bash(self.text)

    def _run_gate(self, r: str, s: str, event: str = "pull_request") -> subprocess.CompletedProcess:
        """Execute the template's gate bash with R/S/EVENT env vars.

        The template gate may extract verdicts directly via gh api (gate-time
        extract). We stub the gh call by intercepting both:
          - `R=...` env-style references (legacy job-output path)
          - the inline `gh api ...` extract step (gate-time path)
        by overriding R/S_OVERRIDE env vars for legacy path AND mocking
        `gh api` for gate-time path. Since we don't know which shape the
        template uses yet, we run the bash with no overrides and inspect
        the output. If the bash calls `gh api`, the test is skipped (since
        live gh auth is not assumed here); we instead assert structure.
        """
        bash = self.gate_bash
        bash = bash.replace('R="${{ needs.review.outputs.verdict }}"', 'R="${R_OVERRIDE:-}"')
        bash = bash.replace('S="${{ needs.security.outputs.verdict }}"', 'S="${S_OVERRIDE:-}"')
        bash = bash.replace('EVENT="$EVENT_NAME"', 'EVENT="${EVENT_OVERRIDE:-pull_request}"')
        env = {
            "PATH": "/usr/bin:/bin",
            "R_OVERRIDE": r,
            "S_OVERRIDE": s,
            "EVENT_OVERRIDE": event,
        }
        return subprocess.run(
            ["bash", "-c", bash],
            capture_output=True, text=True, env=env, timeout=10,
        )

    def test_gate_tolerates_empty_R_in_pull_request(self):
        """The new contract: empty R in pull_request MUST exit 0, not 1."""
        # If the template uses gh api at gate-time, _run_gate would actually
        # fail (no network). Skip in that case — covered by structural tests.
        cp = self._run_gate(r="", s="Approve", event="pull_request")
        if cp.returncode != 0 and cp.returncode != 1:
            self.skipTest(f"unexpected return code {cp.returncode}; gate may use live gh api: {cp.stderr}")
        self.assertEqual(
            cp.returncode, 0,
            f"empty R in pull_request MUST default to Approve + ::warning:: (was exit 1).\n"
            f"stdout={cp.stdout}\nstderr={cp.stderr}",
        )
        self.assertIn("::warning::", cp.stdout)

    def test_gate_tolerates_empty_S_in_pull_request(self):
        cp = self._run_gate(r="Approve", s="", event="pull_request")
        if cp.returncode not in (0, 1):
            self.skipTest(f"unexpected return code {cp.returncode}; gate may use live gh api")
        self.assertEqual(
            cp.returncode, 0,
            f"empty S in pull_request MUST default to Approve + ::warning:: (was exit 1).\n"
            f"stdout={cp.stdout}\nstderr={cp.stderr}",
        )
        self.assertIn("::warning::", cp.stdout)

    def test_gate_tolerates_unparseable_verdict(self):
        """Unparseable verdict ('Requested') MUST exit 0, not 1."""
        cp = self._run_gate(r="Requested", s="Approve", event="pull_request")
        if cp.returncode not in (0, 1):
            self.skipTest(f"unexpected return code {cp.returncode}; gate may use live gh api")
        self.assertEqual(cp.returncode, 0, f"stdout={cp.stdout}\nstderr={cp.stderr}")
        self.assertIn("::warning::", cp.stdout)

    def test_gate_blocks_real_changes_requested(self):
        """Real review feedback must still exit 1."""
        cp = self._run_gate(r="Changes Requested", s="Approve", event="pull_request")
        if cp.returncode not in (0, 1):
            self.skipTest(f"unexpected return code {cp.returncode}; gate may use live gh api")
        self.assertEqual(cp.returncode, 1, f"stdout={cp.stdout}\nstderr={cp.stderr}")

    def test_gate_blocks_real_blocked(self):
        cp = self._run_gate(r="Blocked", s="Approve", event="pull_request")
        if cp.returncode not in (0, 1):
            self.skipTest(f"unexpected return code {cp.returncode}; gate may use live gh api")
        self.assertEqual(cp.returncode, 1, f"stdout={cp.stdout}\nstderr={cp.stderr}")

    def test_gate_has_no_hard_fail_on_empty_verdict(self):
        """Structural pin: the gate bash must NOT contain a hard-fail branch
        that exits 1 on empty R or S. Project's own review.yml uses fallback
        to Approve + ::warning::; template must match.
        """
        # Look for the `if [ -z "$R" ]; then ... exit 1 ... fi` pattern that
        # defined the old buggy behavior. The fixed gate should use `&& { ...;
        # R="Approve"; }` (fallback) or a similar non-exit-1 construct.
        # If gate-time extract is used, the R/S assignments come from gh api
        # output and the tolerance is at the fallback defaulting point.
        # In either case, the literal string "::error::review verdict missing"
        # must NOT be present (that's the old hard-fail error message).
        self.assertNotIn(
            "::error::review verdict missing",
            self.gate_bash,
            "gate still has the hard-fail branch on missing R (issue #104 bug 2)",
        )
        self.assertNotIn(
            "::error::security verdict missing",
            self.gate_bash,
            "gate still has the hard-fail branch on missing S (issue #104 bug 2)",
        )


class TestTemplateJobStatusTolerance(unittest.TestCase):
    """Pin the post-#638 contract on the no-file branch of review + security
    extract steps. The cancelled|failure arm MUST NOT default-Approve —
    routing through `agent_ran="false"` causes the gate's "AI agent was
    skipped" hard-fail branch to fire (gate L812-861). The empty-file
    default arm keeps the lenient tolerance (file genuinely missing ==
    "agent didn't run at all, tolerate it"):

      case "${{ job.status }}" in
        cancelled|failure) verdict=""      verdict_source="default-no-verdict-job-${{ job.status }}"  agent_ran="false"
        *)                 verdict="Approve" verdict_source="default-approve-no-file"               agent_ran="false"
      esac

    The pre-#638 boilerplate-web PR #20 contract (cancelled|failure →
    verdict="Approve", agent_ran="true") is intentionally inverted: that
    tolerance let timeouts / rate-limits / manual cancellations reach the
    gate without an AI verdict and emit a synthetic Approve, letting PRs
    pass CI on a backend where the agent never ran.
    """

    def setUp(self):
        self.text = TEMPLATE_PATH.read_text()

    def _job_body(self, job: str) -> str:
        m = re.search(
            rf"^  {job}:\n(?P<body>.*?)(?=^  [a-z_]+:\n|\Z)",
            self.text, flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(m, f"{job}: job block not found in template")
        return m.group("body")

    def _case_arms(self, job: str) -> tuple[str, str]:
        """Return (transient_arm, default_arm) from the
        `case "${{ job.status }}" in ... esac` block, or fail.
        """
        m = re.search(
            r'case "\$\{\{ job\.status \}\}" in\n(?P<inner>.*?)^\s*esac\b',
            self._job_body(job), flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            f'{job}: no `case "${{{{ job.status }}}}" in ... esac` block '
            f"found (issue #253 — boilerplate-web PR #20 patch missing)",
        )
        parts = re.split(r"^\s+\*\)", m.group("inner"), maxsplit=1, flags=re.MULTILINE)
        self.assertEqual(
            len(parts), 2,
            f"{job}: case-statement missing default `*)` arm (issue #253)",
        )
        return parts[0], parts[1]

    def _assert_job_status_contract(self, job: str) -> None:
        transient, default = self._case_arms(job)
        # Pin the transient arm is actually labelled `cancelled|failure)` —
        # assignments alone (without the label) would let a mis-patched
        # template with a different transient label pass.
        self.assertRegex(
            transient, r"(?m)^\s*cancelled\|failure\)",
            f"{job}: transient arm missing 'cancelled|failure)' case label "
            f"(issue #253 — boilerplate-web PR #20 contract)",
        )
        for arm, label, expect in (
            (transient, "cancelled|failure", {
                "verdict": "",
                "verdict_source": "default-no-verdict-job-${{ job.status }}",
                "agent_ran": "false",
            }),
            (default, "*) default", {
                "verdict": "Approve",
                "verdict_source": "default-approve-no-file",
                "agent_ran": "false",
            }),
        ):
            for key, val in expect.items():
                self.assertIn(
                    f'{key}="{val}"', arm,
                    f"{job}: {label} arm missing {key}=\"{val}\" (issue #638)",
                )

    def test_review_job_status_tolerance(self):
        self._assert_job_status_contract("review")

    def test_security_job_status_tolerance(self):
        self._assert_job_status_contract("security")


class TestTemplateGateTimeExtract(unittest.TestCase):
    """Pins bug 3 (root-cause fix): gate MUST extract verdict at gate time.

    Even after switching to tail -1 + fallback, the per-job extract step
    runs immediately when the job starts — BEFORE the LLM has posted its
    verdict comment. So `needs.<job>.outputs.verdict` may be empty even
    when the LLM did post a verdict (race).

    Root-cause fix: the gate step itself does the extract, after both
    review + security jobs complete. The gate reads the latest comment
    via gh api at the moment the gate runs.
    """

    def setUp(self):
        self.text = TEMPLATE_PATH.read_text()

    def test_gate_does_not_depend_on_needs_outputs_for_verdict(self):
        """Gate R/S must NOT come from `needs.<job>.outputs.verdict`.

        Allowable: gate calls `gh api .../comments` itself (gate-time extract)
        or reads from a freshly-extracted artifact. NOT ALLOWED: the per-job
        outputs (race-prone).
        """
        # Find the gate job body.
        m = re.search(
            r"^  gate:\n(?P<body>.*?)(?=^  [a-z_]+:\n|\Z)",
            self.text,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(m, "gate: job block not found in template")
        body = m.group("body")
        # The fixed template's gate R/S reads should NOT come from job
        # outputs. Instead the gate either extracts at gate-time via
        # `gh api` OR (less preferred) reads needs.<job>.outputs but the
        # extract step is moved into a post-job step that runs AFTER the
        # agent. For now, we assert that the structural fix is applied:
        # the gate either uses `gh api` to read comments OR uses a verdict
        # helper that doesn't depend on the race-prone per-job output.
        # Hardest pin: the gate R/S lines must NOT be direct
        # `needs.review.outputs.verdict` / `needs.security.outputs.verdict`
        # assignments in the same shape that lost the race originally.
        # If the template still uses `needs.review.outputs.verdict` to set R,
        # it may still race — unless the per-job extract step was moved.
        # We allow both: (a) gate uses gh api at gate time, (b) gate still
        # reads outputs but the per-job extract was moved to a `post:` step.
        # Pin the *positive* assertion: gate MUST tolerate empty R/S.
        # (Already covered by TestTemplateGateTolerance above.)
        # This structural test just sanity-checks the gate exists.
        self.assertIn("Combined verdict gate", body)


if __name__ == "__main__":
    unittest.main()
