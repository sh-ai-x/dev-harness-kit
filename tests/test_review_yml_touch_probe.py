"""test_review_yml_touch_probe.py — static regression guard for the
`scope` job's touch-probe regex in `.github/workflows/review.yml`.

Discovered live (2026-08-11): the touch-probe regex used to decide
`touches_prod` — which gates whether the expensive `/dev-kit:review`
and `/dev-kit:security` LLM-judge jobs even run in CI — was missing
`bin/` and `commands/` from its production-root list, even though
`bin/review-local.sh`'s OWN internal touch-probe regex (used for the
local `--auto-approve` L3-evidence gate) already includes both. A PR
that ONLY touches `bin/*.sh` or `commands/*.md` was silently
classified as "docs/infra-only", so the LLM review + security jobs
never ran in GH-Actions at all (no review comments posted -- observed
against a real PR).

These tests are deliberately static (grep the YAML text directly)
rather than spinning up an actual workflow run — GH-Actions jobs
aren't unit-testable in this repo's suite, so a content-level
regression guard is the practical alternative. Mirrors the style of
tests/test_review_local_sh.py::TestLocalAuthFallback's static
no-bashism check.

The `TestMaintenanceGateFileStatusExtraction` class pins the file-
status extraction pattern across `.github/workflows/maintenance.yml`
and `bin/review-local.sh` — three copies (`gh pr view --json files`
projection, the bash loop that consumes it, and `bin/review-local.sh`'s
`read_pr_fields` projection) must all stay in sync, or the
registry-index sub-gate silently regresses to the legacy path-only
behavior (new skill/command additions pass without a registry-doc
update).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
REVIEW_YML = PROJECT_ROOT / ".github" / "workflows" / "review.yml"
MAINTENANCE_YML = PROJECT_ROOT / ".github" / "workflows" / "maintenance.yml"
REVIEW_LOCAL_SH = PROJECT_ROOT / "bin" / "review-local.sh"

# The canonical, already-correct list lives in bin/review-local.sh's
# own touch-probe regex. review.yml's scope job MUST match it exactly
# so "does this PR touch production code" means the same thing in
# both the local and CI code paths.
CANONICAL_ROOTS = (
    "bin", "commands", "lib", "tools", "hooks", "skills",
    "\\.githooks", "\\.claude", "\\.codex", "\\.github",
)


class TestReviewYmlTouchProbeRootsMatchCanonical(unittest.TestCase):
    def setUp(self) -> None:
        self.text = REVIEW_YML.read_text(encoding="utf-8")

    def test_review_yml_exists(self) -> None:
        self.assertTrue(REVIEW_YML.exists(), f"missing: {REVIEW_YML}")

    def test_scope_job_touch_probe_regex_includes_bin(self) -> None:
        """The scope job's grep -E pattern must include `bin` -- a PR
        touching only bin/*.sh scripts must be classified as
        production code."""
        m = re.search(r"grep -E '\^\(([^)]+)\)/'", self.text)
        self.assertIsNotNone(m, "could not find the scope job's grep -E touch-probe pattern")
        roots = m.group(1).split("|")
        self.assertIn("bin", roots, f"touch-probe pattern missing 'bin': {roots}")

    def test_scope_job_touch_probe_regex_includes_commands(self) -> None:
        m = re.search(r"grep -E '\^\(([^)]+)\)/'", self.text)
        self.assertIsNotNone(m, "could not find the scope job's grep -E touch-probe pattern")
        roots = m.group(1).split("|")
        self.assertIn("commands", roots, f"touch-probe pattern missing 'commands': {roots}")

    def test_scope_job_touch_probe_regex_matches_canonical_set(self) -> None:
        """Full parity check against bin/review-local.sh's already-
        correct list -- catches ANY future drift, not just bin/commands.
        """
        m = re.search(r"grep -E '\^\(([^)]+)\)/'", self.text)
        self.assertIsNotNone(m, "could not find the scope job's grep -E touch-probe pattern")
        roots = set(m.group(1).split("|"))
        expected = set(CANONICAL_ROOTS)
        self.assertEqual(
            roots, expected,
            f"review.yml scope-job roots {sorted(roots)} != canonical "
            f"{sorted(expected)} (source of truth: bin/review-local.sh's "
            f"own touch-probe regex)",
        )

    def test_docs_infra_only_message_mentions_bin_and_commands(self) -> None:
        """The human-readable messages that ENUMERATE the production
        roots (not the short "::notice::...advisory here" summary
        line) should stay consistent with the regex they describe -- a
        silent drift here is a documentation bug, not a functional
        one, but it misleads operators debugging a skipped review job.
        """
        occurrences = [
            line for line in self.text.splitlines()
            if "echo" in line and (
                "no bin/commands/lib" in line or "did not touch" in line
            )
        ]
        self.assertTrue(
            occurrences,
            "expected at least one path-enumerating 'docs/infra-only' message",
        )
        for line in occurrences:
            self.assertIn("bin", line, f"message doesn't mention bin/: {line!r}")
            self.assertIn("commands", line, f"message doesn't mention commands/: {line!r}")


class TestMaintenanceGateFileStatusExtraction(unittest.TestCase):
    """Pins the file-status extraction pattern across the three copies
    of "extract PR files for the maintenance gate":

      1. `.github/workflows/maintenance.yml` — the CI workflow's
         ``gh pr view --json files --jq ...`` projection must emit
         ``<status>\\t<path>`` (tab-delimited) so the bash loop can
         split it via ``IFS=$'\\t'``.
      2. The same workflow's bash loop must read with
         ``IFS=$'\\t' read -r status path`` and pass
         ``"${path}:${status}"`` to ``--changed-files``.
      3. ``bin/review-local.sh``'s ``read_pr_fields`` projection must
         include ``files_with_status`` so a future local
         ``--docs-check`` call has the per-file status info it needs.

    Drift in any of these copies silently regresses the registry-
    index sub-gate to legacy path-only behavior — new skill/command
    additions pass without a registry-doc update, which is the exact
    bug this class guards against.
    """

    def setUp(self) -> None:
        self.workflow_text = MAINTENANCE_YML.read_text(encoding="utf-8")
        self.review_local_text = REVIEW_LOCAL_SH.read_text(encoding="utf-8")

    def test_workflow_files_exist(self) -> None:
        self.assertTrue(MAINTENANCE_YML.exists(), f"missing: {MAINTENANCE_YML}")
        self.assertTrue(REVIEW_LOCAL_SH.exists(), f"missing: {REVIEW_LOCAL_SH}")

    def test_workflow_jq_emits_status_and_path(self) -> None:
        """The ``gh pr view --json files --jq`` projection must emit
        the per-file ``status`` field — not just the legacy
        ``.files[].path``. Pin the jq pattern so a future refactor
        that drops ``status`` (and silently regresses the
        registry-index check) fails CI.
        """
        # Specifically the changed-files extraction — not the body
        # extraction earlier in the same workflow. The pattern is:
        #   gh pr view "$PR_NUMBER" --repo "..." \
        #     --json files --jq -r '<projection>'
        m = re.search(
            r"--json files\s+--jq\s+-r\s+'([^']+)'",
            self.workflow_text,
        )
        self.assertIsNotNone(
            m, "could not find gh pr view --json files --jq projection in maintenance.yml"
        )
        jq = m.group(1)
        self.assertIn(".status", jq, f"jq projection missing .status: {jq!r}")
        self.assertIn(".path", jq, f"jq projection missing .path: {jq!r}")
        # Tab delimiter keeps the receiving Python parser (which uses
        # `:` as separator) free from ambiguity.
        self.assertIn("\\t", jq, f"jq projection missing tab delimiter: {jq!r}")

    def test_workflow_bash_loop_splits_on_tab(self) -> None:
        """The bash loop that consumes the tab-delimited status/path
        output MUST split on tab (``IFS=$'\\t' read -r status path``).
        A line that uses a different delimiter (space, newline, `:`)
        would silently misalign the two fields, breaking the
        ``--changed-files "${path}:${status}"`` invocation.
        """
        self.assertIn(
            "IFS=$'\\t' read",
            self.workflow_text,
            "expected bash loop with 'IFS=$'\\t' read' to split status/path",
        )

    def test_workflow_passes_path_colon_status(self) -> None:
        """The bash loop MUST pass ``--changed-files "${path}:${status}"``
        (the format ``lib.maintenance_gate.parse_file_entry`` accepts).
        A plain ``"${path}"`` (legacy) silently strips the status info
        and the registry-index sub-gate regresses to vacuous pass.
        """
        self.assertIn(
            '--changed-files" "${path}:${status}"',
            self.workflow_text,
            'expected --changed-files "${path}:${status}" form in maintenance.yml',
        )

    def test_review_local_extracts_files_with_status(self) -> None:
        """``bin/review-local.sh``'s jq projection must include
        ``files_with_status`` so a future local ``--docs-check``
        call (or its local-mode parity PR) has the per-file status
        info. Pinned here so the field doesn't drift back to legacy
        path-only.
        """
        self.assertIn(
            "files_with_status",
            self.review_local_text,
            "expected bin/review-local.sh to extract files_with_status "
            "(tab-delimited '<status>\\t<path>')",
        )

    def test_review_local_jq_uses_tab_delimiter(self) -> None:
        """The ``files_with_status`` field MUST use tab as the inner
        delimiter (mirroring maintenance.yml). A different delimiter
        here (newline, space) would silently corrupt the per-line
        ``<status>\\t<path>`` rows that consumers iterate over.
        """
        m = re.search(
            r"files_with_status:\s*\[\.files\[\][^]]+\]",
            self.review_local_text,
        )
        self.assertIsNotNone(
            m, "could not find files_with_status jq projection in bin/review-local.sh"
        )
        projection = m.group(0)
        self.assertIn(
            "\\t",
            projection,
            f"files_with_status projection missing tab delimiter: {projection!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
