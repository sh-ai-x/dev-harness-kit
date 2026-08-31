#!/usr/bin/env python3
"""test_maintenance_gate.py — RED-first tests for lib/maintenance_gate.py.

The maintenance gate runs in CI (`.github/workflows/maintenance.yml`)
and has two checks beyond the LLM judge's verdict:

  1. Verdict extraction — parse the `**Verdict:** ...` line from the
     claude-code-action's PR comment, matching review.yml's pattern.
  2. Docs-updated check — ensure the PR touches both a code path under
     `lib/` / `tools/` / `hooks/` / `skills/` / `.githooks/` AND at
     least one file under `docs/` (excluding auto-managed docs).

These two checks live in `lib/maintenance_gate.py` so they can be unit
tested without spinning up GitHub Actions. The workflow YAML invokes
the same module via `python3 -m lib.maintenance_gate ...`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import maintenance_gate  # noqa: E402


class TestExtractVerdict(unittest.TestCase):
    """The verdict-extraction mirrors `scripts/extract-verdict` from
    review.yml's pattern: pick the last `**Verdict:** ...` line.
    """

    def test_extract_approve(self):
        body = "lots of prose\n\n**Verdict:** Approve\n\nmore prose"
        self.assertEqual(maintenance_gate.extract_verdict(body), "Approve")

    def test_extract_changes_requested(self):
        body = "**Verdict:** Changes Requested"
        self.assertEqual(maintenance_gate.extract_verdict(body), "Changes Requested")

    def test_extract_blocked(self):
        body = "**Verdict:** Blocked"
        self.assertEqual(maintenance_gate.extract_verdict(body), "Blocked")

    def test_extract_returns_last_when_multiple(self):
        body = (
            "**Verdict:** Approve\n"
            "Some chatter...\n"
            "**Verdict:** Changes Requested\n"
        )
        # The CI gate picks the most recent verdict (last match wins)
        # so an auto-fix-updated comment supersedes an earlier one.
        self.assertEqual(maintenance_gate.extract_verdict(body), "Changes Requested")

    def test_extract_returns_empty_on_no_verdict(self):
        self.assertEqual(maintenance_gate.extract_verdict("no verdict here"), "")

    def test_extract_ignores_lowercase(self):
        # Strict format: must be `**Verdict:** <Word>`. Lowercase is
        # not a valid verdict — gate tolerates this as no-verdict.
        self.assertEqual(maintenance_gate.extract_verdict("**verdict:** approve"), "")

    def test_extract_skips_backtick_quoted_recap(self) -> None:
        """LLM judges recap prior review verdicts inside backticks when
        discussing prior iterations. A naive `re.findall` over the
        whole body lets the LAST backtick-quoted recap win and silently
        flips a structurally `Approve` comment to `Changes Requested`.

        Regression: this exact body shape appeared in the
        /dev-kit:security + /dev-kit:maintenance judge outputs on
        PR #781 and caused false-positive `Changes Requested`
        extractions.
        """
        body = (
            "**Claude finished @sh-ai-x's task in 1m 38s**\n"
            "\n"
            "---\n"
            "### Task progress\n"
            "- [x] Verify branch state\n"
            "- [x] Apply 11-category security rubric\n"
            "- [x] Map verdict per spec\n"
            "- [x] Post security summary\n"
            "\n"
            "**Verdict:** Approve\n"
            "\n"
            "### Security review -- PR #781\n"
            "\n"
            "...long prose...\n"
            "\n"
            "The earlier `**Verdict:** Blocked` (review lens) and\n"
            "`**Verdict:** Changes Requested` (maintenance lens) at\n"
            "`head_sha=d67d9b` ...\n"
        )
        # Must extract the structural Approve, not the backtick-quoted
        # Changes Requested recap at the bottom.
        self.assertEqual(maintenance_gate.extract_verdict(body), "Approve")

    def test_extract_skips_fenced_code_block_with_verdict_mention(self) -> None:
        """Verdict mentions inside triple-backtick fenced code blocks
        (e.g., a regex-pattern recap in a fenced block) must also be
        ignored.
        """
        body = (
            "**Verdict:** Approve\n"
            "\n"
            "```python\n"
            "# Pattern: r'\\*\\*Verdict:\\*\\*\\s*(Approve|Blocked|Changes Requested)\\b'\n"
            "**Verdict:** Blocked\n"  # inside the fenced block -- not the active verdict
            "```\n"
        )
        self.assertEqual(maintenance_gate.extract_verdict(body), "Approve")

    def test_extract_no_structural_verdict_returns_empty(self) -> None:
        """If the body only contains backtick-quoted mentions and no
        structural `**Verdict:**` line, extraction returns "" -- the
        gate then tolerates the missing verdict per its lenient policy
        rather than flipping to a stale recap.
        """
        body = (
            "Some prose.\n"
            "`**Verdict:** Approve` (a recap)\n"
            "`**Verdict:** Blocked` (another recap)\n"
        )
        self.assertEqual(maintenance_gate.extract_verdict(body), "")


class TestDocsUpdatedCheck(unittest.TestCase):
    """The docs-updated sub-gate logic."""

    def test_passes_when_no_prod_change(self):
        # PR only touches docs/ — no code change, no docs update needed.
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["docs/stages/STAGES.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_passes_when_prod_change_has_matching_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["lib/foo.py", "docs/foo.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_fails_when_prod_change_lacks_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["lib/foo.py"],
            pr_body="",
        )
        self.assertFalse(ok)
        self.assertIn("lib/foo.py", reason)

    def test_passes_when_prod_change_justified_in_pr_body(self):
        # PR body quotes a pre-existing doc as justification.
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["lib/foo.py"],
            pr_body=(
                "Refs #42.\n\n"
                "docs-not-required: docs/foo.md already covers this behavior.\n"
            ),
        )
        self.assertTrue(ok, reason)

    def test_passes_for_tools_change_with_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["tools/foo.py", "docs/tools.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_fails_for_skills_change_without_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["skills/foo/SKILL.md"],
            pr_body="",
        )
        # skills/ changes ARE prod changes (a skill ships with the
        # plugin and should be paired with a docs/skills/* doc).
        self.assertFalse(ok)

    def test_auto_managed_docs_dont_count(self):
        # STAGES.md and REPOSITORY-MAP.md are auto-managed and don't
        # count as "doc updates" — verify a PR that touches ONLY those
        # but no other docs file still fails when it also touches prod.
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["lib/foo.py", "docs/stages/STAGES.md"],
            pr_body="",
        )
        self.assertFalse(ok)

    def test_hooks_change_with_docs_passes(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=[".githooks/pre-push", "docs/hooks.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    # -------------------------------------------------------------------
    # _PROD_ROOTS drift regression: this list independently duplicates
    # bin/review-local.sh's touch-probe regex (which already includes
    # bin/ and commands/ per a fix noted in its own comments) and
    # review.yml's scope-job regex. All three drifted out of sync --
    # this module's list was missing bin/, commands/, .claude/,
    # .codex/, and .github/ entirely, so a PR that ONLY touches e.g.
    # bin/*.sh was never flagged as needing a docs update.
    # -------------------------------------------------------------------
    def test_fails_for_bin_change_without_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["bin/review-local.sh"],
            pr_body="",
        )
        self.assertFalse(ok, reason)

    def test_passes_for_bin_change_with_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["bin/review-local.sh", "docs/local-ci.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_fails_for_commands_change_without_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["commands/babysit-pr-local.md"],
            pr_body="",
        )
        self.assertFalse(ok, reason)

    def test_passes_for_commands_change_with_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["commands/babysit-pr-local.md", "docs/local-ci.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_fails_for_github_workflow_change_without_docs(self):
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=[".github/workflows/review.yml"],
            pr_body="",
        )
        self.assertFalse(ok, reason)


class TestCombinedVerdictDerivation(unittest.TestCase):
    """The gate combines (judge_verdict, docs_ok) into a single CI
    verdict. Pure-function logic, no IO.
    """

    def test_approve_with_docs_passes(self):
        outcome = maintenance_gate.combine_verdict(
            judge_verdict="Approve",
            docs_ok=True,
            docs_reason="ok",
        )
        self.assertEqual(outcome["verdict"], "Approve")
        self.assertTrue(outcome["docs_ok"])

    def test_approve_without_docs_fails(self):
        outcome = maintenance_gate.combine_verdict(
            judge_verdict="Approve",
            docs_ok=False,
            docs_reason="lib/foo.py missing docs",
        )
        self.assertEqual(outcome["verdict"], "Changes Requested")
        self.assertFalse(outcome["docs_ok"])
        self.assertIn("lib/foo.py", outcome["reason"])

    def test_blocked_short_circuits(self):
        # Even with perfect docs, a Blocked judge is Blocked.
        outcome = maintenance_gate.combine_verdict(
            judge_verdict="Blocked",
            docs_ok=True,
            docs_reason="ok",
        )
        self.assertEqual(outcome["verdict"], "Blocked")

    def test_missing_or_unknown_judge_blocks(self):
        for verdict in ("", "nonsense"):
            with self.subTest(verdict=verdict):
                outcome = maintenance_gate.combine_verdict(
                    judge_verdict=verdict,
                    docs_ok=True,
                    docs_reason="ok",
                )
                self.assertEqual(outcome["verdict"], "Blocked")


class TestFormatAuditBody(unittest.TestCase):
    """The audit-comment formatter renders a parseable quartet on line 1
    plus a human-readable markdown table below. The consumer in
    lib/pr_verify.py reads ONLY the parseable quartet (marker +
    run=/job=/status=/verdict=); the table is cosmetic.
    """

    def test_gh_minimal_no_extras(self):
        body = maintenance_gate.format_audit_body(
            run_id="31482962075",
            job="maintenance",
            status="success",
            verdict="Approve",
            source="lib.maintenance_gate",
        )
        # Parseable quartet is intact on line 1.
        first_line = body.splitlines()[0]
        self.assertIn("<!-- dev-kit-verdict-audit -->", first_line)
        self.assertIn("run=31482962075", first_line)
        self.assertIn("job=maintenance", first_line)
        self.assertIn("status=success", first_line)
        self.assertIn("verdict=Approve", first_line)
        self.assertIn("source=lib.maintenance_gate", first_line)
        # Human-facing header + 5-row table.
        self.assertIn("Approve", body)
        self.assertIn("| Run", body)
        self.assertIn("31482962075", body)
        self.assertIn("| Job", body)
        self.assertIn("maintenance", body)
        self.assertIn("| Status", body)
        self.assertIn("success", body)
        self.assertIn("| Verdict", body)
        self.assertIn("| Source", body)
        self.assertIn("lib.maintenance_gate", body)

    def test_local_with_extras(self):
        body = maintenance_gate.format_audit_body(
            run_id="local-12345",
            job="review-local",
            status="success",
            verdict="Changes Requested",
            source="bin_review_local",
            extras={
                "review": "Approve",
                "security": "Changes Requested",
                "maintenance": "Approve",
                "provider": "minimax",
            },
        )
        first_line = body.splitlines()[0]
        # Extras land on the parseable line (after source=).
        self.assertIn("review=Approve", first_line)
        self.assertIn("security=Changes Requested", first_line)
        self.assertIn("maintenance=Approve", first_line)
        self.assertIn("provider=minimax", first_line)
        # AND each becomes a row in the table (label is Title-Case).
        self.assertIn("| Review", body)
        self.assertIn("Approve", body)
        self.assertIn("| Security", body)
        self.assertIn("Changes Requested", body)
        self.assertIn("| Maintenance", body)
        self.assertIn("| Provider", body)
        self.assertIn("minimax", body)

    def test_emoji_for_each_verdict(self):
        cases = [
            ("Approve", "✅"),
            ("Changes Requested", "⚠️"),
            ("Blocked", "❌"),
            ("MISSING", "❓"),
        ]
        for verdict, emoji in cases:
            with self.subTest(verdict=verdict):
                body = maintenance_gate.format_audit_body(
                    run_id="1", job="review", status="success",
                    verdict=verdict, source="lib.maintenance_gate",
                )
                # Emoji appears next to the verdict in the table.
                self.assertIn(emoji, body)
                self.assertIn(verdict, body)
                self.assertIn("| Verdict", body)

    def test_empty_fields_render_gracefully(self):
        body = maintenance_gate.format_audit_body(
            run_id="1", job="", status="", verdict="", source="",
        )
        # Empty verdict renders as ❓ MISSING so the operator sees a
        # missing-verdict signal rather than a blank cell.
        self.assertIn("❓ MISSING", body)
        # Empty source still renders (no emoji decoration).
        self.assertIn("| Source", body)
        # Parseable line still well-formed.
        first_line = body.splitlines()[0]
        self.assertIn("verdict=MISSING", first_line)
        self.assertIn("source=", first_line)

    def test_parseable_line_is_first(self):
        body = maintenance_gate.format_audit_body(
            run_id="42", job="review", status="success",
            verdict="Approve", source="lib.maintenance_gate",
        )
        # The marker MUST be on line 1 — lib/pr_verify.py's regex
        # anchors on it. If a future refactor accidentally moves the
        # marker into a code block or a sub-heading, G4 stops parsing.
        first_non_empty = next(line for line in body.splitlines() if line.strip())
        self.assertTrue(
            first_non_empty.startswith("<!-- dev-kit-verdict-audit -->"),
            f"marker not on first non-empty line: {first_non_empty!r}",
        )


class TestCLISubprocess(unittest.TestCase):
    """End-to-end CLI invocation parity — the workflow calls the
    gate via `python3 -m lib.maintenance_gate ...` so we exercise that
    path here too.
    """

    def test_cli_extract_verdict(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--extract-verdict-from-stdin"],
            input="**Verdict:** Approve\n",
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Approve")

    def test_cli_docs_check_passes(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--docs-check",
             "--changed-files", "lib/foo.py",
             "--changed-files", "docs/foo.md",
             "--pr-body", ""],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["docs_ok"], True)

    def test_cli_format_audit_gh_shape(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--format-audit",
             "--run", "31482962075",
             "--job", "maintenance",
             "--status", "success",
             "--verdict", "Approve",
             "--source", "lib.maintenance_gate"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = result.stdout
        # Parseable quartet intact on line 1.
        self.assertIn("<!-- dev-kit-verdict-audit -->", body)
        self.assertIn("run=31482962075 job=maintenance status=success "
                      "verdict=Approve source=lib.maintenance_gate", body)
        # 5-row table rendered.
        self.assertIn("| Field", body)
        self.assertIn("| Run", body)
        self.assertIn("31482962075", body)
        self.assertIn("| Job", body)
        self.assertIn("maintenance", body)

    def test_cli_format_audit_local_extras(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--format-audit",
             "--run", "local-12345",
             "--job", "review-local",
             "--status", "success",
             "--verdict", "Approve",
             "--source", "bin_review_local",
             "--extra", "review=Approve",
             "--extra", "security=Approve"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = result.stdout
        # Extras land on the parseable line.
        self.assertIn("review=Approve", body)
        self.assertIn("security=Approve", body)
        # AND as table rows (Title-Case labels).
        self.assertIn("| Review", body)
        self.assertIn("| Security", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
