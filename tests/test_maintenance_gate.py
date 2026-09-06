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

    def test_cli_docs_check_fails_on_new_skill(self):
        # File-status-aware input via the CLI: a new SKILL.md with no
        # registry doc update MUST fail the docs check.
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--docs-check",
             "--changed-files", "skills/foo/SKILL.md:added",
             "--pr-body", ""],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        out = json.loads(result.stdout)
        self.assertFalse(out["docs_ok"])
        self.assertIn("skills/foo/SKILL.md", out["reason"])

    def test_cli_docs_check_passes_new_skill_with_readme(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--docs-check",
             "--changed-files", "skills/foo/SKILL.md:added",
             "--changed-files", "README.md:modified",
             "--pr-body", ""],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertTrue(out["docs_ok"])

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


class TestRegistryIndexCheck(unittest.TestCase):
    """The skill/command registry sub-gate (issue: new SKILL.md / new
    commands/*.md without a registry-doc update was silently approved).

    The deterministic check lives in `registry_index_updated_ok()`. It
    combines with the path-level `docs_updated_ok()` check via worst-wins
    in the parent `docs_updated_ok()` orchestrator.
    """

    # --- new skill detection -------------------------------------------------

    def test_fails_when_new_skill_has_no_registry_doc(self):
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["skills/foo/SKILL.md:added"],
            pr_body="",
        )
        self.assertFalse(ok, reason)
        self.assertIn("skills/foo/SKILL.md", reason)

    def test_passes_when_new_skill_updates_readme(self):
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["skills/foo/SKILL.md:added", "README.md:modified"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_passes_when_new_skill_updates_docs_skills_readme(self):
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=[
                "skills/foo/SKILL.md:added",
                "docs/skills/README.md:modified",
            ],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_passes_when_new_skill_updates_docs_skills_readme_ko(self):
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=[
                "skills/foo/SKILL.md:added",
                "docs/skills/README.ko.md:modified",
            ],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_passes_when_pr_body_carries_marker(self):
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["skills/foo/SKILL.md:added"],
            pr_body=(
                "Adds /dev-kit:foo.\n\n"
                "docs-not-required: docs/skills/README.md already lists it.\n"
            ),
        )
        self.assertTrue(ok, reason)

    def test_passes_when_only_existing_skill_modified(self):
        # status=modified (not added) — registry check should not fire.
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["skills/foo/SKILL.md:modified"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_passes_when_skill_renamed_only(self):
        # Per design decision (Q4): pure renames do NOT require a
        # registry doc update — `skills/README.md` still points to the
        # renamed skill's path. GitHub's similarity threshold makes
        # `renamed` inconsistent across PRs, so we treat renames as
        # no-op for the registry check.
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["skills/bar/SKILL.md:renamed"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    # --- new command detection -----------------------------------------------

    def test_fails_when_new_command_has_no_registry_doc(self):
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["commands/foo.md:added"],
            pr_body="",
        )
        self.assertFalse(ok, reason)
        self.assertIn("commands/foo.md", reason)

    def test_passes_when_commands_readme_added_alongside_new_command(self):
        # Bootstrap case: first PR that ever adds commands/README.md
        # AND a new command at the same time. The registry doc + the new
        # command are both `added` in the same diff.
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=[
                "commands/README.md:added",
                "commands/foo.md:added",
            ],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_modified_commands_readme_does_not_bypass_check(self):
        # A `commands/README.md` with status=modified does not by itself
        # satisfy the registry check — only a NEW skill/command does.
        # (This is more of a sanity check on the status gating.)
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=[
                "commands/README.md:modified",
                "commands/foo.md:added",
            ],
            pr_body="",
        )
        # commands/README.md IS in the registry docs set, so this passes.
        # This test documents the fact that the gate counts any
        # `commands/README.md` touch as a registry doc update, regardless
        # of status. If we ever tighten to status=added, this test pins
        # the behavior change.
        self.assertTrue(ok, reason)

    # --- backward compatibility ---------------------------------------------

    def test_legacy_bare_path_defaults_to_modified(self):
        # Pre-existing callers pass `"skills/foo/SKILL.md"` without a
        # status — they MUST keep working (status defaults to "modified",
        # so the registry check sees nothing new and passes).
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["skills/foo/SKILL.md"],
            pr_body="",
        )
        self.assertTrue(ok, reason)

    def test_mixed_entry_shapes(self):
        # One legacy bare path + one path:status — both must be parsed
        # independently. The legacy entry is treated as "modified" and
        # does not trigger the registry check; the status-aware entry
        # IS a new skill and the registry check MUST see it.
        ok, reason = maintenance_gate.registry_index_updated_ok(
            changed_files=["lib/foo.py", "skills/foo/SKILL.md:added"],
            pr_body="",
        )
        self.assertFalse(ok, reason)
        self.assertIn("skills/foo/SKILL.md", reason)

    # --- defensive parser ----------------------------------------------------

    def test_colon_in_path_rejected(self):
        # Paths in this repo never contain `:` (flat layout, no Windows
        # drive letters). The parser MUST reject ambiguous multi-colon
        # entries rather than silently splitting on the wrong one.
        with self.assertRaises(ValueError):
            maintenance_gate.registry_index_updated_ok(
                changed_files=["weird:path:added"],
                pr_body="",
            )

    def test_empty_status_rejected(self):
        # `:added` with empty path is also malformed.
        with self.assertRaises(ValueError):
            maintenance_gate.registry_index_updated_ok(
                changed_files=[":added"],
                pr_body="",
            )

    def test_empty_path_rejected(self):
        # `lib/foo.py:` (trailing colon, empty status) is malformed.
        with self.assertRaises(ValueError):
            maintenance_gate.registry_index_updated_ok(
                changed_files=["lib/foo.py:"],
                pr_body="",
            )

    # --- combined docs_updated_ok wiring ------------------------------------

    def test_docs_updated_ok_combines_registry_fail_first(self):
        # When BOTH the path-level check AND the registry check would
        # fail, the registry-specific message wins (more actionable).
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=[
                "skills/foo/SKILL.md:added",
                "lib/bar.py:modified",
            ],
            pr_body="",
        )
        self.assertFalse(ok, reason)
        # Registry reason mentions the new skill + the registry doc.
        self.assertIn("skills/foo/SKILL.md", reason)
        self.assertIn("registry", reason.lower())

    def test_docs_updated_ok_path_level_failure_when_no_registry_issue(self):
        # lib/foo.py changed (path-level fail) but no new skill/command
        # → reason names the path-level failure, not registry.
        ok, reason = maintenance_gate.docs_updated_ok(
            changed_files=["lib/foo.py:modified"],
            pr_body="",
        )
        self.assertFalse(ok, reason)
        self.assertIn("lib/foo.py", reason)


class TestParseFileEntry(unittest.TestCase):
    """The `parse_file_entry` helper is exported so CLI `--help` and
    downstream callers can reference it directly.
    """

    def test_legacy_bare_path(self):
        self.assertEqual(
            maintenance_gate.parse_file_entry("lib/foo.py"),
            ("lib/foo.py", "modified"),
        )

    def test_status_aware(self):
        self.assertEqual(
            maintenance_gate.parse_file_entry("skills/foo/SKILL.md:added"),
            ("skills/foo/SKILL.md", "added"),
        )

    def test_rejects_multi_colon(self):
        with self.assertRaises(ValueError):
            maintenance_gate.parse_file_entry("weird:path:added")

    def test_rejects_empty_path(self):
        with self.assertRaises(ValueError):
            maintenance_gate.parse_file_entry(":added")

    def test_rejects_empty_status(self):
        with self.assertRaises(ValueError):
            maintenance_gate.parse_file_entry("lib/foo.py:")


class TestRegistryOnlyCLI(unittest.TestCase):
    """`--registry-only` flag runs only the registry check (skip the
    path-level docs check). Useful for triaging "did the operator
    forget to update the README" without conflating with the broader
    docs-not-required path-level check.
    """

    def test_cli_registry_only_fails_on_new_skill(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--registry-only",
             "--changed-files", "skills/foo/SKILL.md:added",
             "--pr-body", ""],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        out = json.loads(result.stdout)
        self.assertFalse(out["registry_ok"])
        self.assertIn("skills/foo/SKILL.md", out["reason"])

    def test_cli_registry_only_passes_when_registry_doc_touched(self):
        py = sys.executable
        result = subprocess.run(
            [py, "-m", "lib.maintenance_gate",
             "--project-root", tempfile.mkdtemp(),
             "--registry-only",
             "--changed-files", "skills/foo/SKILL.md:added",
             "--changed-files", "README.md:modified",
             "--pr-body", ""],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertTrue(out["registry_ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
