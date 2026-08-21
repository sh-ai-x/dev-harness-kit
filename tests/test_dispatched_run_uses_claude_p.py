#!/usr/bin/env python3
"""test_dispatched_run_uses_claude_p.py — pin the dispatched-run workaround shape.

Background:
  `anthropics/claude-code-action@v1` (pinned at
  558b1d6cab4085c7753fe402c10bef0fbb92ac7a) silently no-ops on
  `workflow_dispatch` events:

    - Agent mode writes only `claude-prompt.txt`, NOT
      `claude-user-request.txt` (src/modes/agent/index.ts:78-82), so the
      SDK treats `/dev-kit:review --diff ...` as literal text
      (base-action/src/run-claude-sdk.ts:30-37).
    - The `mcp__github_inline_comment__create_inline_comment` MCP server
      is gated on `isEntityContext()` which returns false for
      workflow_dispatch (src/mcp/install-mcp-server.ts:134).

  Net effect: dispatched runs exit with `num_turns: 0, duration_ms: 21,
  is_error: false` — green but no AI review comments posted
  (observed against PRs #682 / #687).

  The fix: for `workflow_dispatch` events only, the judge steps
  bypass `claude-code-action` and invoke `bin/ci-claude-p.sh
  <skill> <pr_number>` directly. The script installs Claude Code CLI
  if missing, then runs `claude -p` with the right prompt + flags.
  The existing `claude-code-action` step's `if:` is tightened to
  `&& github.event_name == 'pull_request'` so the broken path is
  skipped on dispatch but still runs for same-repo `pull_request`.

This test pins the workflow shape so the workaround cannot silently
regress (e.g. someone removes the dispatched steps, or someone
removes the `github.event_name == 'pull_request'` filter from
claude-code-action and dispatched runs go back to silently no-op'ing).

Pin tests (all are pure YAML parsing, no process spawning):

  T1: review.yml and maintenance.yml have steps with
      `if: ... github.event_name == 'workflow_dispatch' ...` for
      each of the three judges x three providers = nine call sites
      in review.yml (review: 3, security: 3) and three in
      maintenance.yml (maintenance: 3). Each calls `bin/ci-claude-p.sh`
      with the right skill name.
  T2: The existing `claude-code-action` steps in review.yml +
      maintenance.yml have `if:` conditions that include
      `github.event_name == 'pull_request'` (or equivalent
      `github.event_name != 'workflow_dispatch'`) so the broken
      path is skipped on dispatch.
  T3: fork-pr-review.yml still uses the `fork-pr-review` Environment
      gate (manual approval required) — the workaround lives in the
      dispatched workflows, NOT in the gate.
  T4: bin/ci-claude-p.sh exists and is executable.
  T5: bin/ci-claude-p.sh's header references the upstream issue
      numbers so a future maintainer doesn't silently revert to
      claude-code-action on workflow_dispatch.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).parent.parent / ".github" / "workflows"
REVIEW_YML = WORKFLOWS_DIR / "review.yml"
MAINTENANCE_YML = WORKFLOWS_DIR / "maintenance.yml"
FORK_PR_REVIEW_YML = WORKFLOWS_DIR / "fork-pr-review.yml"
CI_CLAUDE_P_SH = Path(__file__).parent.parent / "bin" / "ci-claude-p.sh"

# Three providers x three judges = the expected number of workaround steps.
PROVIDERS = ("minimax", "anthropic", "deepseek")
# (workflow_file, judge_name, expected_skill_name)
JUDGES = (
    (REVIEW_YML, "review", "review"),
    (REVIEW_YML, "security", "security"),
    (MAINTENANCE_YML, "maintenance_judge", "maintenance"),
)


def _yaml_doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(doc: dict, job_name: str) -> list[dict]:
    """Return the list of step dicts for a given job."""
    return doc.get("jobs", {}).get(job_name, {}).get("steps", [])


class TestDispatchedRunUsesClaudeP(unittest.TestCase):

    # -- T1: workaround steps present for every (provider, judge) combo --

    def test_01_workaround_steps_present_for_all_judges_and_providers(self):
        """For every (workflow, judge, provider) triple, there must be
        a step with
        `if: github.event_name == 'workflow_dispatch' && steps.provider.outputs.provider == '<provider>'`
        that runs `bin/ci-claude-p.sh <skill> <pr_number>`.

        Total expected: 3 (review) + 3 (security) + 3 (maintenance) = 9
        workaround steps across the two workflows. If any are
        missing, the corresponding provider's dispatched run will
        silently no-op (the original bug).
        """
        for workflow_path, judge_name, skill_name in JUDGES:
            doc = _yaml_doc(workflow_path)
            steps = _steps(doc, judge_name)
            for provider in PROVIDERS:
                with self.subTest(workflow=workflow_path.name, judge=judge_name, provider=provider):
                    matching = [
                        s for s in steps
                        if "github.event_name == 'workflow_dispatch'" in (s.get("if") or "")
                        and f"steps.provider.outputs.provider == '{provider}'" in (s.get("if") or "")
                    ]
                    self.assertGreaterEqual(
                        len(matching), 1,
                        f"{workflow_path.name}::{judge_name}: no workaround step "
                        f"for provider={provider}. Without it, dispatched runs "
                        "from fork-pr-review.yml will hit the upstream "
                        "claude-code-action no-op bug "
                        "(anthropics/claude-code-action#635 + #1644).",
                    )
                    helper_callers = [
                        s for s in matching
                        if "ci-claude-p.sh" in (s.get("run") or "")
                        and skill_name in (s.get("run") or "")
                    ]
                    self.assertGreaterEqual(
                        len(helper_callers), 1,
                        f"{workflow_path.name}::{judge_name}: workaround step "
                        f"for provider={provider} exists but doesn't call "
                        f"`bin/ci-claude-p.sh {skill_name} ...`. The helper is "
                        "the single source of truth for the claude -p "
                        "invocation shape.",
                    )

    # -- T2: claude-code-action steps still run for pull_request --

    def test_02_claude_code_action_steps_skip_on_workflow_dispatch(self):
        """Every `claude-code-action` step in review.yml +
        maintenance.yml must have an `if:` that EXCLUDES
        `workflow_dispatch` — either by including
        `github.event_name == 'pull_request'` or
        `github.event_name != 'workflow_dispatch'`. Without this,
        dispatched runs would still hit the broken path."""
        for workflow_path, judge_name, _skill_name in JUDGES:
            doc = _yaml_doc(workflow_path)
            steps = _steps(doc, judge_name)
            cca_steps = [
                s for s in steps
                if "anthropics/claude-code-action" in (s.get("uses") or "")
            ]
            self.assertGreater(
                len(cca_steps), 0,
                f"{workflow_path.name}::{judge_name}: expected at least one "
                "claude-code-action step to exist (the same-repo pull_request path).",
            )
            for cca in cca_steps:
                if_text = cca.get("if") or ""
                self.assertTrue(
                    ("github.event_name == 'pull_request'" in if_text
                     or "github.event_name != 'workflow_dispatch'" in if_text),
                    f"{workflow_path.name}::{judge_name}: claude-code-action "
                    f"step {cca.get('name', '?')!r} has no workflow_dispatch "
                    f"exclusion in its `if:` ({if_text!r}). Dispatched runs "
                    "from fork-pr-review.yml would silently no-op on this "
                    "path (the original bug).",
                )

    # -- T3: gate still manual (Environment approval) --

    def test_03_fork_pr_review_gate_unchanged(self):
        """fork-pr-review.yml must still require manual maintainer
        approval via the `fork-pr-review` Environment. The
        workaround lives in the dispatched workflows, NOT in the
        gate. If the gate is weakened (auto-run, env removed), fork
        PRs would consume API spend without human approval."""
        doc = _yaml_doc(FORK_PR_REVIEW_YML)
        gate_jobs = [
            name for name, job in doc.get("jobs", {}).items()
            if "fork-pr-review" in (job.get("environment") or "")
        ]
        self.assertGreaterEqual(
            len(gate_jobs), 1,
            "fork-pr-review.yml must have at least one job gated behind "
            "the `fork-pr-review` Environment (manual approval). If the "
            "env block is removed, the gate becomes a free auto-run on "
            "every fork PR.",
        )
        on = doc.get("on") or doc.get(True) or {}
        self.assertIn("pull_request_target", on,
                      "fork-pr-review.yml must trigger on pull_request_target "
                      "(the workflow file is read from base branch, not fork).")
        self.assertNotIn("pull_request", on,
                         "fork-pr-review.yml must NOT trigger on pull_request "
                         "(that would let the fork's workflow file run with "
                         "write permissions, defeating the trust boundary).")

    # -- T4: helper script exists + is executable --

    def test_04_helper_script_exists_and_is_executable(self):
        """bin/ci-claude-p.sh must exist and be executable. The
        workflow steps call it via
        `bin/ci-claude-p.sh <skill> <pr_number>` (no explicit
        interpreter), so non-executable permission would fail with
        `Permission denied` at runtime."""
        self.assertTrue(CI_CLAUDE_P_SH.exists(),
                        f"missing helper: {CI_CLAUDE_P_SH}")
        mode = CI_CLAUDE_P_SH.stat().st_mode
        self.assertTrue(
            mode & 0o100,
            f"{CI_CLAUDE_P_SH} is not executable (mode={oct(mode)}). The "
            "workflow invokes it as `bin/ci-claude-p.sh <skill> <pr_number>` "
            "(no `bash` prefix), so non-executable permission would fail.",
        )

    # -- T5: helper script pins the workaround contract --

    def test_05_helper_script_pins_workaround_contract(self):
        """bin/ci-claude-p.sh's header comment must explicitly
        reference the upstream issue numbers so a future maintainer
        doesn't silently revert to claude-code-action on
        workflow_dispatch."""
        text = CI_CLAUDE_P_SH.read_text(encoding="utf-8")
        self.assertTrue(
            re.search(r"anthropics/claude-code-action#(635|1644)", text),
            "bin/ci-claude-p.sh header must reference "
            "anthropics/claude-code-action#635 or #1644 — without that pin "
            "a future maintainer might revert to the broken "
            "claude-code-action path on workflow_dispatch.",
        )


if __name__ == "__main__":
    unittest.main()
