#!/usr/bin/env python3
"""test_ci_setup.py — Tests for the `/dev-kit:ci-setup` engine.

Covers lib/ci_setup.py:install_ci_config() and the templates/ tree it ships.
Uses the same importlib-from-path pattern as tests/test_smoke.py so it works
as both `python -m unittest tests/test_ci_setup.py` and `pytest tests/test_ci_setup.py`.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load_ci_setup():
    """Load lib/ci_setup.py by file path (mirrors test_smoke.py:64-66 pattern).

    NOTE: the module MUST be registered in sys.modules BEFORE exec_module for
    Python 3.14's @dataclass to resolve cross-module type lookups.
    """
    name = "ci_setup"
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "lib" / "ci_setup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register FIRST so @dataclass can resolve names
    spec.loader.exec_module(mod)
    return mod


class TestCiSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_bootstrap_engine_returns_typed_report(self):
        """Smoke-check the InstallReport dataclass shape."""
        r = self.ci_setup.InstallReport()
        self.assertIsInstance(r.created, list)
        self.assertIsInstance(r.overwritten, list)
        self.assertIsInstance(r.skipped, list)
        self.assertIsInstance(r.errors, list)
        self.assertEqual(r.marker_path, "")
        self.assertEqual(r.elapsed_ms, 0)
        self.assertTrue(r.ok)
        r.errors.append("forced")
        self.assertFalse(r.ok)

    def test_invalid_target_dir_raises(self):
        """Non-existent target raises FileNotFoundError; non-directory raises NotADirectoryError."""
        with self.assertRaises(FileNotFoundError):
            self.ci_setup.install_ci_config(Path("/nonexistent/ci_setup_test_xyz"))
        # File-as-target → NotADirectoryError or FileNotFoundError (depends on resolver).
        fp = tempfile_path("foo")
        try:
            with self.assertRaises((NotADirectoryError, FileNotFoundError)):
                self.ci_setup.install_ci_config(fp)
        finally:
            fp.unlink(missing_ok=True)

    def test_install_creates_expected_files_in_empty_target(self, tmpdir=None):
        """Fresh tmp dir: all EXPECTED_PATHS land; marker is written."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = self.ci_setup.install_ci_config(target)
            self.assertEqual(report.errors, [], f"errors: {report.errors}")
            for rel in self.ci_setup.EXPECTED_PATHS:
                self.assertTrue((target / rel).exists(), f"missing: {rel}")
            # 8 paths × created (target was empty)
            self.assertEqual(len(report.created), len(self.ci_setup.EXPECTED_PATHS))
            self.assertEqual(report.overwritten, [])
            self.assertEqual(report.skipped, [])
            # Marker present
            marker = target / ".dev-kit" / "ci-config.json"
            self.assertTrue(marker.exists())
            self.assertTrue(report.marker_path.endswith("ci-config.json"))

    def test_install_is_idempotent_without_force(self):
        """Second run without force skips every path; marker rewritten."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r1 = self.ci_setup.install_ci_config(target)
            self.assertEqual(r1.errors, [])
            first_mtime = (target / ".dev-kit" / "ci-config.json").stat().st_mtime
            r2 = self.ci_setup.install_ci_config(target)
            self.assertEqual(r2.created, [])
            self.assertEqual(r2.overwritten, [])
            self.assertEqual(
                len(r2.skipped), len(self.ci_setup.EXPECTED_PATHS),
                "all paths should be skipped on re-run without --force",
            )
            self.assertEqual(r2.errors, [])
            # Idempotency does NOT touch file contents, but the marker's
            # `installed_at` may update — that's documented behavior.
            second_mtime = (target / ".dev-kit" / "ci-config.json").stat().st_mtime
            self.assertGreaterEqual(second_mtime, first_mtime)

    def test_install_force_overwrites_cleanly(self):
        """Pre-seed a sentinel; --force replaces it with template content."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            sentinel_dir = target / ".github" / "workflows"
            sentinel_dir.mkdir(parents=True)
            sentinel = sentinel_dir / "ci.yml"
            sentinel.write_text("# SENTINEL: must be replaced by --force\n")
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r.errors, [])
            content = sentinel.read_text()
            self.assertNotIn("SENTINEL", content, "force=True should overwrite sentinel")
            self.assertIn("name: CI", content, "template content should land")
            overwritten = [p for p in r.overwritten if "ci.yml" in p]
            self.assertTrue(overwritten, "ci.yml should be in overwritten list")

    def test_marker_file_written_with_correct_shape(self):
        """Marker JSON has the right fields and types."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)  # default version
            marker = target / ".dev-kit" / "ci-config.json"
            data = json.loads(marker.read_text())
            for key in (
                "schema_version", "installed_at",
                "installed_by", "runners", "scripts", "githooks",
            ):
                self.assertIn(key, data, f"missing key: {key}")
            self.assertEqual(data["schema_version"], "1.0.0")
            self.assertEqual(data["installed_by"], "dev-kit:ci-setup")
            self.assertEqual(set(data["runners"]), {"ci.yml", "auto-fix-pr.yml", "review.yml"})
            self.assertEqual(set(data["scripts"]), {
                "scripts/validate.py", "scripts/test.sh",
                "scripts/branch-policy.sh", "scripts/ci-local.sh",
            })
            self.assertEqual(data["githooks"], [".githooks/pre-push"])
            # installed_at should be ISO-8601 UTC (z-suffix)
            self.assertTrue(data["installed_at"].endswith("Z"), data["installed_at"])
            # verification block intentionally removed — schema stays minimal.

    def test_presence_short_circuit(self):
        """When marker + all EXPECTED_PATHS exist, install is a no-op (no files touched)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r1 = self.ci_setup.install_ci_config(target)
            self.assertEqual(len(r1.created), len(self.ci_setup.EXPECTED_PATHS))
            # Sentinel each EXPECTED_PATH so we can detect any re-touch
            sentinels = {}
            for rel in self.ci_setup.EXPECTED_PATHS:
                p = target / rel
                sentinels[rel] = p.read_text()
            r2 = self.ci_setup.install_ci_config(target)
            self.assertEqual(r2.created, [], "short-circuit must skip create")
            self.assertEqual(r2.overwritten, [], "short-circuit must skip overwrite")
            self.assertEqual(
                len(r2.skipped), len(self.ci_setup.EXPECTED_PATHS),
                "short-circuit must list every EXPECTED_PATH in skipped",
            )
            # Confirm files on disk were not re-written (content preserved)
            for rel in self.ci_setup.EXPECTED_PATHS:
                self.assertEqual(
                    (target / rel).read_text(), sentinels[rel],
                    f"file re-touched during short-circuit: {rel}",
                )
            # Marker still present at the expected location (path may be resolved to /private/... on macOS)
            self.assertTrue((target / ".dev-kit" / "ci-config.json").exists())
            self.assertTrue(r2.marker_path.endswith("ci-config.json"))

    def test_partial_install_completes_remaining(self):
        """If marker exists but some templates are missing, install copies only the missing ones."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Delete one file + the marker so install must re-copy
            (target / self.ci_setup.EXPECTED_PATHS[0]).unlink()
            (target / ".dev-kit" / "ci-config.json").unlink()
            r = self.ci_setup.install_ci_config(target)
            self.assertTrue(
                len(r.created) + len(r.overwritten) >= 1,
                f"at least the deleted path should be re-copied; created={r.created} overwritten={r.overwritten}",
            )

    def test_executable_bit_set_on_sh_files(self):
        """All .sh + pre-push + validate.py have +x bit after install."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            for rel in self.ci_setup.EXECUTABLE_PATHS:
                p = target / rel
                self.assertTrue(p.exists(), f"missing: {rel}")
                # Read mode bit directly (POSIX st_mode)
                mode = p.stat().st_mode
                self.assertTrue(mode & 0o111, f"not executable: {rel} (mode={oct(mode)})")

    def test_validate_py_runs_against_installed_ci_dir(self):
        """The installed validate.py exits 0 against the install target."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            r = subprocess.run(
                ["python3", "scripts/validate.py"],
                cwd=str(target), capture_output=True, text=True,
            )
            self.assertEqual(
                r.returncode, 0,
                f"validate.py exited {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}",
            )
            self.assertIn("OK: CI installation valid", r.stdout)

    # === Worktree-rule rollout (PR #22 + this PR) ===

    def test_worktree_rule_files_are_in_expected_paths(self):
        """EXPECTED_PATHS includes the 7 worktree-rule files added in PR #22 +
        the shared payload-parse helper (PR #78 / issue #273), plus the
        /dev-kit:babysit-pr-local entrypoints and lib helpers added in
        PR for issue #619."""
        expected_new = {
            "hooks/worktree-guard.sh",
            "hooks/session-start-check.sh",
            "hooks/lib/worktree-detect.sh",
            "hooks/lib/payload-parse.sh",
            "hooks/hooks.json",
            ".claude/rules/git-workflow.md",
            "tests/test_worktree_guard.py",
            # /dev-kit:babysit-pr-local entrypoints (issue #619).
            "bin/babysit-pr-local.sh",
            "bin/review-local.sh",
            "bin/set-provider.sh",
            "lib/review_local_lib.sh",
            "lib/maintenance_gate.py",
            "lib/atomic.py",
            "lib/__init__.py",
        }
        actual = set(self.ci_setup.EXPECTED_PATHS)
        self.assertTrue(
            expected_new.issubset(actual),
            f"missing from EXPECTED_PATHS: {expected_new - actual}",
        )

    def test_hook_manifest_and_sources_are_installed_as_one_ssot(self):
        """Every canonical hook source reaches consumers with its manifest.

        This guards portability when a new hook/helper is added: ci-setup
        must not require a second hand-maintained template entry.
        """
        import importlib.util
        import json

        expected = set(self.ci_setup.EXPECTED_PATHS)
        source_hooks = {
            f"hooks/{p.relative_to(PROJECT_ROOT / 'hooks').as_posix()}"
            for p in (PROJECT_ROOT / "hooks").rglob("*.sh")
        }
        self.assertTrue(source_hooks.issubset(expected))
        self.assertIn("hooks/hooks.json", expected)
        manifest = json.loads((PROJECT_ROOT / "hooks" / "hooks.json").read_text())
        commands = [
            hook["command"]
            for groups in manifest["hooks"].values()
            for group in groups
            for hook in group.get("hooks", [])
            if "command" in hook
        ]
        validator_path = PROJECT_ROOT / "templates/ci/scripts/validate.py"
        spec = importlib.util.spec_from_file_location("ci_validate", validator_path)
        validator = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(validator)
        referenced = {
            f"hooks/{match}"
            for command in commands
            for match in validator.referenced_hook_scripts(command)
        }
        self.assertTrue(referenced.issubset(expected))

    def test_validator_reports_current_canonical_hook_file_count(self):
        """The validator output describes the complete installed hook tree."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            result = subprocess.run(
                [sys.executable, "scripts/validate.py"],
                cwd=target,
                capture_output=True,
                text=True,
            )
            hook_count = len(list((target / "hooks").rglob("*.sh")))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"+ {hook_count} hooks", result.stdout)

    def test_validator_handles_unreadable_hook_manifest(self):
        """A hooks/hooks.json that fails UTF-8 decode returns False (no traceback).

        Regression for the maintenance review finding that an unguarded
        ``manifest.read_text(encoding='utf-8')`` produced a Python traceback
        for non-UTF-8 manifest bytes, instead of the validator's normal FAIL
        line. The guard at templates/ci/scripts/validate.py routes the
        ``OSError``/``UnicodeError`` through ``_fail()`` so the validator's
        exit code stays 1 and CI surfaces the real culprit cleanly.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Overwrite the manifest with bytes that are not valid UTF-8
            # so ``read_text(encoding='utf-8')`` raises ``UnicodeDecodeError``.
            manifest = target / "hooks" / "hooks.json"
            manifest.write_bytes(b"\xff\xfe\x00\x01garbage")
            result = subprocess.run(
                [sys.executable, "scripts/validate.py"],
                cwd=target,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertIn("FAIL", result.stdout)

    def test_worktree_hooks_have_executable_bit_in_target(self):
        """All 6 new .sh files end up executable in the installed target."""
        import stat
        import tempfile
        new_sh = (
            "hooks/worktree-guard.sh",
            "hooks/session-start-check.sh",
            "hooks/lib/worktree-detect.sh",
            "hooks/lib/payload-parse.sh",
        )
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            for rel in new_sh:
                p = target / rel
                self.assertTrue(p.exists(), f"missing: {rel}")
                self.assertTrue(p.stat().st_mode & stat.S_IXUSR, f"not +x: {rel}")

    def test_marker_schema_version_current(self):
        """Schema is content-only (1.0.0) — no version-gate field."""
        self.assertEqual(self.ci_setup.MARKER_SCHEMA_VERSION, "1.0.0")
        self.assertTrue(hasattr(self.ci_setup, "plugin_version"),
                        "ci_setup must expose a runtime `plugin_version()` reader")

    def test_marker_records_hooks_rules_tests(self):
        """Marker JSON lists the new categories (hooks / rules / tests)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            for key in ("hooks", "rules", "tests"):
                self.assertIn(key, marker, f"marker missing key: {key}")
                self.assertTrue(len(marker[key]) > 0, f"marker.{key} should be non-empty")
            self.assertIn("hooks/worktree-guard.sh", marker["hooks"])
            self.assertIn("hooks/lib/payload-parse.sh", marker["hooks"])
            self.assertIn(".claude/rules/git-workflow.md", marker["rules"])
            self.assertIn("tests/test_worktree_guard.py", marker["tests"])

    def test_post_install_checklist_is_complete(self):
        """5 numbered items; each is a gh secret set, a gh/git config, or a
        workflow-setting note. Must be actionable."""
        items = self.ci_setup.POST_INSTALL_CHECKLIST
        self.assertGreaterEqual(
            len(items), 5,
            f"expected >=5 post-install checklist items, got {len(items)}",
        )
        seen_numbers = set()
        for n, body in items:
            self.assertTrue(
                n.isdigit() and 1 <= int(n) <= 9,
                f"checklist number {n!r} must be a digit 1..9",
            )
            self.assertNotIn(int(n), seen_numbers, f"duplicate: {n}")
            seen_numbers.add(int(n))
            joined = body.lower()
            self.assertTrue(
                any(needle in joined for needle in (
                    "gh secret set", "git config", "push a feature branch",
                    "merge that", "/dev-kit:review",
                )),
                f"checklist item {n} does not mention any actionable command",
            )

    def test_lint_installed_workflows_flags_stale_gate_pattern(self):
        """Lint pass detects pre-0.1.3 PR-mode hard-fail gate.

        The pre-0.1.3 templates/ci/.github/workflows/review.yml shipped a
        gate that hard-failed in pull_request mode on missing verdicts
        while defaulting to Approve in workflow_dispatch mode. The
        distinctive substring 'Re-run via workflow_dispatch if needed'
        is unique to that block; the lint pass is keyed on it.
        """
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            review = _P(td) / ".github" / "workflows" / "review.yml"
            review.parent.mkdir(parents=True)
            review.write_text(
                "dummy\n          Re-run via workflow_dispatch if needed\n"
            )
            findings = self.ci_setup.lint_installed_workflows(_P(td))
            self.assertTrue(
                any(".github/workflows/review.yml" in f for f in findings),
                f"expected gate-tolerance finding, got {findings!r}",
            )

    def test_lint_installed_workflows_clean_on_fresh_install(self):
        """Fresh install of the current (post-0.1.3) template yields 0 lint warnings."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            r = self.ci_setup.install_ci_config(_P(td))
            self.assertEqual(r.warnings, [], r.warnings)
            self.assertEqual(self.ci_setup.lint_installed_workflows(_P(td)), [])

    def test_lint_runs_on_no_op_idempotent_reinstall(self):
        """Idempotent re-install (no --force) still lints and surfaces drift."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            r1 = self.ci_setup.install_ci_config(_P(td))
            self.assertEqual(r1.warnings, [])
            review = _P(td) / ".github" / "workflows" / "review.yml"
            review.write_text(
                review.read_text()
                + "\n          Re-run via workflow_dispatch if needed\n"
            )
            r2 = self.ci_setup.install_ci_config(_P(td), force=False)
            self.assertTrue(
                any("stale pull_request hard-fail gate" in w for w in r2.warnings),
                f"expected stale-gate warning, got {r2.warnings!r}",
            )

    def test_lint_kwarg_can_suppress(self):
        """`lint=False` suppresses the warning-class output."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            review = _P(td) / ".github" / "workflows" / "review.yml"
            review.parent.mkdir(parents=True)
            review.write_text(
                "          Re-run via workflow_dispatch if needed\n"
            )
            r = self.ci_setup.install_ci_config(_P(td), force=False, lint=False)
            self.assertEqual(
                r.warnings,
                [],
                "lint=False must suppress findings",
            )

    def test_lint_flags_hash_inside_if_block_scalar(self):
        """Issue #219 Bug 1: `#`-prefixed lines inside `if: |` block scalars
        break GitHub Actions' expression parser. The lint pass must flag this
        pattern across all three installed workflow files. Synthesizes the
        minimal broken file rather than mutating the live template, so the
        test stays isolated from upstream template churn.
        """
        import tempfile
        from pathlib import Path as _P
        broken_yaml = (
            "name: Auto-fix on review\n"
            "on: {pull_request_review: {types: [submitted]}}\n"
            "jobs:\n"
            "  auto-fix:\n"
            "    if: |\n"
            "      # Fork-safety: this comment is INSIDE the if block.\n"
            "      github.event.pull_request.head.repo.full_name == github.repository\n"
        )
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            wf = target / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "auto-fix-pr.yml").write_text(broken_yaml)
            findings = self.ci_setup.lint_installed_workflows(target)
            matches = [f for f in findings if "issue #219 Bug 1" in f]
            self.assertTrue(
                matches,
                f"expected issue-219 hash-in-if-block finding, got {findings!r}",
            )
            self.assertTrue(
                any("auto-fix-pr.yml" in m for m in matches),
                f"expected auto-fix-pr.yml in finding path, got {matches!r}",
            )

    def test_lint_does_not_flag_run_blocks_with_shell_comments(self):
        """The lint only flags `#` inside `if:` block scalars. `#` comments
        inside `run:` shell-script blocks are normal bash comments and must
        NOT be flagged — `step.run` values go through bash, not the GitHub
        expression parser.
        """
        import tempfile
        from pathlib import Path as _P
        safe_yaml = (
            "name: CI\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: |\n"
            "          # This is a normal bash comment inside a step.\n"
            "          echo hello\n"
        )
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            wf = target / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text(safe_yaml)
            findings = self.ci_setup.lint_installed_workflows(target)
            self.assertEqual(
                findings, [],
                f"shell `#`-comments in step.run must not lint, got {findings!r}",
            )

    def test_auto_fix_pr_template_has_no_hash_inside_if_block(self):
        """Issue #219 Bug 1: the shipped auto-fix-pr.yml template must NOT
        contain `#`-prefixed lines inside its `if:` block scalar. Guards
        against regression of the bug that broke every consumer push.
        """
        import yaml
        template = (
            PROJECT_ROOT / "templates" / "ci" / ".github" / "workflows"
            / "auto-fix-pr.yml"
        )
        self.assertTrue(template.is_file(), f"template missing: {template}")
        data = yaml.safe_load(template.read_text())
        if_value = data["jobs"]["auto-fix"]["if"]
        bad = [
            ln for ln in if_value.splitlines()
            if ln.lstrip().startswith("#")
        ]
        self.assertEqual(
            bad, [],
            f"auto-fix-pr.yml if: block must not contain `#`-prefixed "
            f"lines (issue #219 Bug 1); got {bad!r}",
        )

    def test_extract_verdict_fallback_reports_agent_ran_false(self):
        """Issue #219 Bug 2: the `extract_verdict` fallback branch in
        review.yml must write `agent_ran=false`, not `agent_ran=true`,
        because the fallback's `gh pr comment` is a placeholder, NOT a
        real agent review. Without this fix, the severity gate's
        `agent_ran=false → exit 1` hard-fail is silently defeated.
        Guards both the review and security job fallback branches.
        """
        import re
        review_template = (
            PROJECT_ROOT / "templates" / "ci" / ".github" / "workflows"
            / "review.yml"
        )
        self.assertTrue(review_template.is_file(), f"missing: {review_template}")
        text = review_template.read_text()
        # Find every `if [ "${{ steps.fallback.outputs.needs_fallback }}" = "true" ]`
        # fallback branch and assert it writes agent_ran=false (not =true).
        branches = re.findall(
            r'if \[ "\$\{\{ steps\.fallback\.outputs\.needs_fallback \}\}" = "true" \]; then'
            r'.*?fi',
            text,
            re.DOTALL,
        )
        self.assertTrue(
            branches,
            "no extract_verdict fallback branches found in review.yml — "
            "test invariant changed",
        )
        bad = []
        for i, body in enumerate(branches):
            # Inside the fallback body, the literal `echo "agent_ran=true" >> "$GITHUB_OUTPUT"`
            # is the bug. `agent_ran=false` (or comments mentioning the old value) are fine.
            if re.search(r'echo "agent_ran=true" >> "\$GITHUB_OUTPUT"', body):
                bad.append(i)
        self.assertEqual(
            bad, [],
            f"extract_verdict fallback branch(es) {bad} still write "
            f"`agent_ran=true`; issue #219 Bug 2 requires `agent_ran=false`.",
        )

    def test_print_checklist_kwarg_does_not_break_existing_callers(self):
        """install_ci_config(..., print_checklist=True) writes the marker and
        returns an InstallReport. Default (no kwarg) behavior unchanged."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r_default = self.ci_setup.install_ci_config(target)
            self.assertEqual(r_default.errors, [])
            r_printing = self.ci_setup.install_ci_config(
                target, force=True, print_checklist=True,
            )
            self.assertEqual(r_printing.errors, [])
            self.assertTrue((target / ".dev-kit" / "ci-config.json").exists())

    def test_ci_template_branch_policy_has_checkout_step(self):
        """Issue #202: branch-policy job must include `actions/checkout@v4`.

        Without this step, consumer repos whose branch-policy job depends
        on working-tree state (or that previously added a local checkout
        fix like commit 445a0b7 in sh-ai-x/claude-statusline) silently
        regress on the next `--force` install. The upstream template
        itself must own the checkout, so the consumer's local customizations
        layer on top rather than getting overwritten away.
        """
        import yaml  # PyYAML — already a dev dep; see pyproject.toml
        template = (PROJECT_ROOT / "templates" / "ci" / ".github" / "workflows" / "ci.yml")
        self.assertTrue(template.is_file(), f"template missing: {template}")
        data = yaml.safe_load(template.read_text())
        jobs = data.get("jobs", {})
        self.assertIn("branch-policy", jobs, "branch-policy job missing")
        steps = jobs["branch-policy"].get("steps", [])
        checkout_steps = [
            s for s in steps
            if isinstance(s, dict) and "uses" in s
            and str(s["uses"]).startswith("actions/checkout")
        ]
        self.assertTrue(
            checkout_steps,
            f"branch-policy job has no actions/checkout step; issue #202. steps={steps!r}",
        )

    def test_auto_fix_pr_template_is_single_repair_adapter_wave(self):
        """The GitHub workflow delegates bounded repair state to the coordinator."""
        template = (PROJECT_ROOT / "templates" / "ci" / ".github" / "workflows" / "auto-fix-pr.yml")
        self.assertTrue(template.is_file(), f"template missing: {template}")
        content = template.read_text()
        self.assertIn("Original PR repair wave already ran", content)
        self.assertIn("if [ \"$COUNT\" -ge 1 ]", content)
        self.assertIn("exit 0", content)
        self.assertIn("Record repair coordinator start", content)
        self.assertIn("Record repair coordinator completion", content)
        self.assertIn("NO_PATCH_REQUIRED", content)

    def test_gitignore_fragment_created_on_fresh_install(self):
        """Issue #202: empty target gets a `.gitignore` with the dev-kit fragment."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            r = self.ci_setup.install_ci_config(target)
            self.assertEqual(r.errors, [], r.errors)
            gi = target / ".gitignore"
            self.assertTrue(gi.is_file(), ".gitignore should be created on fresh install")
            content = gi.read_text()
            # The fragment's distinctive lines must be present.
            for needle in (".dev-kit/.cost-gate/", ".dev-kit/.eval-cache/",
                           ".dev-kit/logs/", "logs/"):
                self.assertIn(needle, content,
                              f".gitignore missing dev-kit fragment line: {needle}")

    def test_gitignore_fragment_preserves_consumer_lines(self):
        """Issue #202: existing `.gitignore` is appended to, never overwritten.

        Lines outside the marked block must survive a `--force` install.
        A consumer's tracked `.env.example` and `# project header` comment
        must remain in the file after the install.
        """
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            gi = target / ".gitignore"
            gi.write_text("# project header — do not touch\nnode_modules/\n.env\n")
            r = self.ci_setup.install_ci_config(target)
            self.assertEqual(r.errors, [], r.errors)
            content = gi.read_text()
            self.assertIn("# project header — do not touch", content,
                          "consumer header line was overwritten")
            self.assertIn("node_modules/", content, "consumer line was overwritten")
            self.assertIn(".env", content, "consumer line was overwritten")
            # And the dev-kit fragment lines are now in the file too.
            self.assertIn(".dev-kit/.cost-gate/", content)

    def test_gitignore_fragment_block_is_idempotent(self):
        """Issue #202: re-running `--force` does not duplicate the dev-kit block."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            self.ci_setup.install_ci_config(target)
            first = (target / ".gitignore").read_text()
            self.ci_setup.install_ci_config(target, force=True)
            second = (target / ".gitignore").read_text()
            # The dev-kit block markers should appear exactly once each.
            self.assertEqual(
                first.count(self.ci_setup._GITIGNORE_BLOCK_START), 1,
                f"block-start marker not unique in first install:\n{first}",
            )
            self.assertEqual(
                second.count(self.ci_setup._GITIGNORE_BLOCK_START), 1,
                f"block-start marker duplicated after --force:\n{second}",
            )

    def test_marker_records_per_file_sha_after_install(self):
        """Issue #202: marker must record SHA-256 of every EXPECTED_PATHS file.

        The drift-detection pass (issue #202) compares the SHA recorded
        at install-time against the file's current SHA on the next
        `--force`. Without per-file SHAs in the marker, drift detection
        is impossible.
        """
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            self.ci_setup.install_ci_config(target)
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertIn("installed_file_shas", marker,
                          "marker missing installed_file_shas field (issue #202)")
            shas = marker["installed_file_shas"]
            self.assertIsInstance(shas, dict)
            # Every EXPECTED_PATHS file with a recordable SHA must have one.
            for rel in self.ci_setup.EXPECTED_PATHS:
                p = target / rel
                if p.is_file():
                    self.assertIn(rel, shas,
                                  f"marker missing SHA for installed file: {rel}")
                    # SHA must be a 64-char hex string (SHA-256).
                    self.assertEqual(len(shas[rel]), 64)
                    int(shas[rel], 16)  # must parse as hex

    def test_drift_detected_when_local_file_modified_before_force(self):
        """Issue #202: locally-modified file triggers a drift warning on `--force`.

        Reproduces the silent-overwrite regression: a consumer adds a
        local fix (e.g. the actions/checkout step at sh-ai-x/claude-statusline
        commit 445a0b7) between installs; the next `--force` install must
        warn the user before the change is overwritten.
        """
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            # Initial install — marker records SHAs.
            self.ci_setup.install_ci_config(target)
            # Consumer locally modifies a workflow file.
            ci_yml = target / ".github" / "workflows" / "ci.yml"
            original = ci_yml.read_text()
            ci_yml.write_text(original + "\n# LOCAL CUSTOMIZATION — do not lose this\n")
            # `--force` install: drift must be reported.
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertTrue(
                any("locally modified since last install" in w and "ci.yml" in w
                    for w in r.warnings),
                f"expected drift warning for ci.yml, got: {r.warnings!r}",
            )
            # After --force, the file has been overwritten: the local
            # customization is gone. The warning is advisory only — the
            # overwrite still happens (we don't silently revert; the
            # user explicitly asked for --force).
            final = ci_yml.read_text()
            self.assertNotIn("LOCAL CUSTOMIZATION", final,
                             "--force overwrote the local customization")

    def test_no_drift_warning_when_files_unchanged(self):
        """Issue #202: a no-op re-install (or --force with no local mods)
        produces zero drift warnings."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            self.ci_setup.install_ci_config(target)
            # Second install (no force): no-op path → still no drift.
            r1 = self.ci_setup.install_ci_config(target)
            self.assertEqual(
                [w for w in r1.warnings if "locally modified since last install" in w],
                [],
                "no-op re-install should not report drift when files unchanged",
            )
            # Third install with --force but no local mods: zero drift.
            r2 = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(
                [w for w in r2.warnings if "locally modified since last install" in w],
                [],
                "--force with unchanged files should not report drift",
            )

    def test_sha_tracking_round_trip_after_overwrite(self):
        """Issue #202: SHAs in the marker must reflect the template bytes
        that landed on disk AFTER the install, not the stale SHA from
        the previous install."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            target = _P(td)
            self.ci_setup.install_ci_config(target)
            # Locally modify.
            ci_yml = target / ".github" / "workflows" / "ci.yml"
            ci_yml.write_text("# trash\n")
            # `--force` overwrites; the new SHA must equal the template's
            # bytes, not the local trash or the prior install's SHA.
            self.ci_setup.install_ci_config(target, force=True)
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            recorded = marker["installed_file_shas"][".github/workflows/ci.yml"]
            actual = self.ci_setup._sha256_file(ci_yml)
            self.assertEqual(recorded, actual,
                             "marker SHA must match post-install file bytes")

    # === Issue #212: provider resolution is now env-based, not a tracked file ===

    def test_provider_file_not_in_expected_paths(self):
        """After the env refactor: the tracked provider file is GONE — there is
        no default to ship, so the same repo can be used by different operators
        with different providers. Provider selection lives in `.env` (local)
        and `vars.CI_REVIEW_PROVIDER` (CI)."""
        self.assertNotIn(
            ".github/ci-review-provider.txt",
            self.ci_setup.EXPECTED_PATHS,
            "ci-review-provider.txt must not be installed — provider is env-based now",
        )

    def test_required_secrets_catalog_contains_known_providers(self):
        """Issue #212-B1/B2: every supported provider has a secret entry."""
        for provider in ("minimax", "anthropic", "deepseek"):
            secrets = self.ci_setup.required_secrets_for_provider(provider)
            self.assertIn(
                "DEV_KIT_GITHUB_TOKEN", secrets,
                f"{provider}: missing DEV_KIT_GITHUB_TOKEN",
            )
            self.assertGreater(
                len(secrets), 1,
                f"{provider}: catalog only returned the consumer PAT; "
                "provider-specific API key is missing",
            )

    def test_required_secrets_unknown_provider_falls_back_to_minimax(self):
        """Unknown provider names fall back to the minimax catalog (matches the
        gate's default fallback). Always includes DEV_KIT_GITHUB_TOKEN."""
        secrets = self.ci_setup.required_secrets_for_provider("not-a-provider")
        self.assertIn("DEV_KIT_GITHUB_TOKEN", secrets)
        self.assertIn("MINIMAX_API_KEY", secrets)

    def test_gh_secret_set_command_format(self):
        """Issue #212-B3: helper renders an exact, paste-able gh command."""
        cmd = self.ci_setup.gh_secret_set_command("OWNER/REPO", "MINIMAX_API_KEY")
        self.assertEqual(cmd, "gh secret set MINIMAX_API_KEY --repo OWNER/REPO")

    def test_read_provider_returns_minimax_when_missing(self):
        """read_provider falls back to 'minimax' (not raises) when no env, no
        .env, no .env.example declares the provider — matches the historical
        default that the now-removed tracked file used to encode."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            # No .env, no .env.example → returns 'minimax'.
            self.assertEqual(self.ci_setup.read_provider(target), "minimax")
            # Unknown value in .env → returns 'minimax' (treated as missing).
            (target / ".env").write_text("CI_REVIEW_PROVIDER=garbage\n")
            self.assertEqual(self.ci_setup.read_provider(target), "minimax")
            # Recognized value in .env → returns normalized value.
            (target / ".env").write_text("CI_REVIEW_PROVIDER=DeepSeek\n")
            self.assertEqual(self.ci_setup.read_provider(target), "deepseek")
            # .env.example fallback when no .env.
            (target / ".env").unlink()
            (target / ".env.example").write_text("CI_REVIEW_PROVIDER=anthropic\n")
            self.assertEqual(self.ci_setup.read_provider(target), "anthropic")

    def test_read_provider_env_var_takes_precedence(self):
        """Process env `CI_REVIEW_PROVIDER` wins over .env — that's how the
        GitHub Actions workflow threads the repo variable through."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=anthropic\n")
            old = os.environ.get("CI_REVIEW_PROVIDER")
            os.environ["CI_REVIEW_PROVIDER"] = "deepseek"
            try:
                self.assertEqual(self.ci_setup.read_provider(target), "deepseek")
            finally:
                if old is None:
                    os.environ.pop("CI_REVIEW_PROVIDER", None)
                else:
                    os.environ["CI_REVIEW_PROVIDER"] = old

    def test_marker_verifies_after_install(self):
        """Issue #212-A3/E1: the marker must round-trip through a real
        read after atomic_write_json — empty/zero-byte markers fail loudly,
        not silently. After the env refactor the marker records the env
        key name (`provider_env_key`) instead of a file path."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = self.ci_setup.install_ci_config(target)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            marker = target / ".dev-kit" / "ci-config.json"
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict)
            self.assertGreater(len(payload), 0)
            self.assertEqual(
                payload.get("provider_env_key"),
                "CI_REVIEW_PROVIDER",
                "marker must point ci-doctor at the env key, not a file path",
            )
            self.assertNotIn(
                "ci_review_provider_file", payload,
                "old file-pointer key must not reappear in the marker",
            )

    # === Issue #273: hooks/lib/payload-parse.sh must ship to consumers ===

    def test_payload_parse_in_expected_paths(self):
        """EXPECTED_PATHS must list hooks/lib/payload-parse.sh so consumers
        don't ship a hook tree whose deny() helper is missing."""
        self.assertIn(
            "hooks/lib/payload-parse.sh",
            self.ci_setup.EXPECTED_PATHS,
            "EXPECTED_PATHS is missing hooks/lib/payload-parse.sh — fix #273",
        )

    def test_payload_parse_in_executable_paths(self):
        """The helper is sourced at runtime; +x bit must be set after install
        so consumers can also invoke it as a CLI guard if they want."""
        self.assertIn(
            "hooks/lib/payload-parse.sh",
            self.ci_setup.EXECUTABLE_PATHS,
            "EXECUTABLE_PATHS is missing hooks/lib/payload-parse.sh — fix #273",
        )

    def test_ci_setup_installs_payload_parse(self):
        """install_ci_config(force=True) lands payload-parse.sh under
        hooks/lib/ with the executable bit set (issue #273 reproduction)."""
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            helper = target / "hooks" / "lib" / "payload-parse.sh"
            self.assertTrue(
                helper.exists(),
                f"hooks/lib/payload-parse.sh missing from install target "
                f"(created={r.created}, overwritten={r.overwritten})",
            )
            self.assertTrue(
                helper.stat().st_mode & stat.S_IXUSR,
                f"hooks/lib/payload-parse.sh must be +x (mode={oct(helper.stat().st_mode)})",
            )
            # Marker must list the helper so ci-doctor / drift-detection see it
            marker = json.loads(
                (target / ".dev-kit" / "ci-config.json").read_text()
            )
            self.assertIn("hooks/lib/payload-parse.sh", marker["hooks"])

    def test_payload_parse_installed_into_already_partial_consumer(self):
        """A consumer repo that already has the pre-#273 install (marker +
        every old EXPECTED_PATHS file) gets payload-parse.sh on the next
        non-force install. This is the natural marker-pin: the new path
        flips `_is_already_installed` to False because the file isn't
        present, so the install re-runs copy+chmod and the consumer
        transitions to green without manual intervention."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            # Seed a pre-#273 install (everything except payload-parse.sh).
            # Iterating EXPECTED_PATHS minus the new helper gives a faithful
            # snapshot of what consumer repos currently have on disk.
            pre_273_paths = [
                p for p in self.ci_setup.EXPECTED_PATHS
                if p != "hooks/lib/payload-parse.sh"
            ]
            for rel in pre_273_paths:
                src = self.ci_setup._resolve_template_source(rel)
                self.assertTrue(src.is_file(), f"seed source missing: {rel}")
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            # Seed a minimal marker so the no-op detection has something to test.
            (target / ".dev-kit").mkdir()
            seed_marker = target / ".dev-kit" / "ci-config.json"
            seed_marker.write_text(json.dumps({
                "schema_version": self.ci_setup.MARKER_SCHEMA_VERSION,
                "installed_at": "2026-07-01T00:00:00Z",
                "installed_by": "dev-kit:ci-setup",
                "hooks": [p for p in pre_273_paths if p.startswith("hooks/")],
            }))
            # Sanity: pre-state has no payload-parse.sh.
            self.assertFalse(
                (target / "hooks" / "lib" / "payload-parse.sh").exists(),
                "seed must NOT contain payload-parse.sh — that's the bug",
            )
            # A plain (force=False) install must now refresh and ship the helper.
            r = self.ci_setup.install_ci_config(target)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            helper = target / "hooks" / "lib" / "payload-parse.sh"
            self.assertTrue(
                helper.exists(),
                "non-force install must add the helper that was missing pre-#273",
            )

    # === /dev-kit:skill-usage's tools/*.py must ship to consumers ===
    #
    # commands/skill-usage.md shells out to a bare relative path
    # (`python3 tools/skill_usage.py`). ${CLAUDE_PLUGIN_ROOT} does not
    # expand inside command markdown bodies (anthropics/claude-code#9354),
    # so any consumer that only ran ci-setup or bootstrap — and never cloned
    # dev-harness-kit itself — got "No such file or directory". These 3
    # files must ship the same way hooks/*.sh already do.

    def test_skill_usage_files_in_expected_paths(self):
        for rel in (
            "tools/skill_usage.py",
            "tools/skill_usage_normalize.py",
            "tools/skill_usage_render.py",
        ):
            self.assertIn(rel, self.ci_setup.EXPECTED_PATHS, f"missing from EXPECTED_PATHS: {rel}")

    def test_skill_usage_entrypoint_in_executable_paths(self):
        """Only the entrypoint needs +x; the two helper modules are
        imported, never invoked directly."""
        self.assertIn("tools/skill_usage.py", self.ci_setup.EXECUTABLE_PATHS)
        self.assertNotIn("tools/skill_usage_normalize.py", self.ci_setup.EXECUTABLE_PATHS)
        self.assertNotIn("tools/skill_usage_render.py", self.ci_setup.EXECUTABLE_PATHS)

    def test_ci_setup_installs_skill_usage_tool(self):
        """install_ci_config(force=True) lands all 3 files, byte-identical
        to the plugin-root source, with +x on the entrypoint, and the
        marker lists them under a `tools` key."""
        import stat
        import tempfile
        plugin_root = Path(__file__).parent.parent
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            for rel in (
                "tools/skill_usage.py",
                "tools/skill_usage_normalize.py",
                "tools/skill_usage_render.py",
            ):
                installed = target / rel
                self.assertTrue(installed.exists(), f"missing from install target: {rel}")
                self.assertEqual(
                    installed.read_bytes(), (plugin_root / rel).read_bytes(),
                    f"{rel} content mismatch vs plugin-root source",
                )
            entrypoint = target / "tools" / "skill_usage.py"
            self.assertTrue(
                entrypoint.stat().st_mode & stat.S_IXUSR,
                f"tools/skill_usage.py must be +x (mode={oct(entrypoint.stat().st_mode)})",
            )
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertIn("tools", marker)
            self.assertIn("tools/skill_usage.py", marker["tools"])

    def test_skill_usage_tool_runs_after_install(self):
        """End-to-end: the installed skill_usage.py must actually run (not
        just exist) -- imports its 2 sibling modules via sys.path, same as
        a real consumer's `python3 tools/skill_usage.py --help` would."""
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            proc = subprocess.run(
                [sys.executable, str(target / "tools" / "skill_usage.py"), "--help"],
                capture_output=True, text=True, cwd=str(target), timeout=15,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"installed skill_usage.py --help failed: stderr={proc.stderr}",
            )
            self.assertIn("usage", proc.stdout.lower())

    # === Issue #273 structural regression (issue suggestion):
    # Walk every hook in EXPECTED_PATHS, grep for `source ... lib/X.sh`,
    # assert every sourced helper is also in EXPECTED_PATHS + EXECUTABLE_PATHS.
    # Single test catches #273 (payload-parse) AND any future helper
    # that ships without being added to the catalog.
    # Resolves the structural root cause: hand-maintained lists drift; this
    # test fails on the first drift, before it ships to consumers.

    def test_verdict_from_comment_helper_is_in_expected_paths(self):
        """Regression: the comment-derived verdict fallback helper
        (templates/ci/.github/workflows/_verdict_from_comment.py) MUST
        be in EXPECTED_PATHS so ci-setup installs it on consumers.

        The helper lives next to review.yml and is invoked by the
        extract_verdict_comments step in the review + security jobs
        (issue #625). Without this EXPECTED_PATHS entry, every consumer
        that ran ci-setup would get a `No such file or directory` error
        the first time the extract-verdict.py parser returned
        PARSE_FAILED and the workflow tried to fall back to comment
        search.
        """
        self.assertIn(
            ".github/workflows/_verdict_from_comment.py",
            set(self.ci_setup.EXPECTED_PATHS),
            "the comment-derived verdict fallback helper must be in "
            "EXPECTED_PATHS so ci-setup copies it to consumers (`gh pr "
            "view ... --json comments` is piped into this helper when "
            "the agent's output file is unparseable -- issue #625)",
        )

    def test_every_sourced_lib_helper_is_in_expected_paths(self):
        """Regression: every `source ... lib/<helper>.sh` reference inside
        an EXPECTED_PATHS hook or bin/ script must resolve to a path that
        is also EXPECTED_PATHS + EXECUTABLE_PATHS. Prevents the #273/#277
        class of bugs where a new helper is added to the plugin tree and
        sourced by the plugin's own hooks but never catalogued.

        Extended in issue #619 to also walk bin/*.sh scripts (notably
        bin/review-local.sh, which sources lib/review_local_lib.sh).
        """
        import re
        # Match `source ... lib/<helper>.sh` for both common shapes:
        #   source "$(dirname "$0")/lib/worktree-detect.sh"
        #   source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
        sourced_helper_re = re.compile(
            r'source\s+[^&\n]*?lib/([a-zA-Z0-9_-]+\.sh)'
        )
        plugin_root = Path(__file__).parent.parent
        sourced: set[str] = set()
        sources_examined: list[str] = []
        # Walk both hooks/ (PR #273 catalog) and bin/ (issue #619). Either
        # can source a lib helper; both must be guarded.
        for rel in self.ci_setup.EXPECTED_PATHS:
            if not (rel.endswith(".sh") and (rel.startswith("hooks/") or rel.startswith("bin/"))):
                continue
            src_path = plugin_root / rel
            self.assertTrue(
                src_path.is_file(),
                f"EXPECTED_PATHS entry {rel} does not exist on disk — "
                "re-run the structural test against the plugin tree",
            )
            sources_examined.append(rel)
            # The helper ref is always relative to the file's own directory:
            # hooks/<x>.sh -> hooks/lib/<helper>.sh; bin/<x>.sh -> lib/<helper>.sh.
            helper_prefix = "hooks/lib/" if rel.startswith("hooks/") else "lib/"
            for m in sourced_helper_re.finditer(src_path.read_text(encoding="utf-8")):
                sourced.add(f"{helper_prefix}{m.group(1)}")
        # Every sourced helper must be in EXPECTED_PATHS (so it ships) AND
        # EXECUTABLE_PATHS (so it gets +x on install — sourced-as-library
        # files need +x if any consumer invokes them as a CLI later).
        missing_from_expected = sourced - set(self.ci_setup.EXPECTED_PATHS)
        missing_from_executable = sourced - set(self.ci_setup.EXECUTABLE_PATHS)
        self.assertEqual(
            missing_from_expected, set(),
            f"Hook / bin scripts in EXPECTED_PATHS source these helpers but the "
            f"helpers aren't in EXPECTED_PATHS (so consumers don't receive "
            f"them -> `command not found` at runtime): {sorted(missing_from_expected)}",
        )
        self.assertEqual(
            missing_from_executable, set(),
            f"Hook / bin scripts in EXPECTED_PATHS source these helpers but the "
            f"helpers aren't in EXECUTABLE_PATHS (so +x bit is missing): "
            f"{sorted(missing_from_executable)}",
        )
        # Sanity: this test must actually have examined something; an empty
        # iteration would mask a future silent regression. Catch the
        # case where the helper regex itself goes stale.
        self.assertGreater(
            len(sources_examined), 0,
            "no hooks/*/*.sh or bin/*.sh entries were examined — "
            "EXPECTED_PATHS shape or the test's own filter likely drifted; "
            "the structural guard would mask new regressions",
        )

    def test_import_succeeds_without_hooks_manifest(self):
        """Regression: consumer-side `from ci_setup import install_ci_config`
        must not raise FileNotFoundError when hooks/hooks.json is absent.

        `lib/install.sh` ships only `ci_setup.py` + `atomic.py` to a target
        repo's `lib/` — no `hooks/` tree. A prior version of this module
        called `_canonical_hook_paths()` eagerly at module level to build
        EXPECTED_PATHS/EXECUTABLE_PATHS, so the bare import itself raised
        FileNotFoundError before install_ci_config was ever called. The
        `_LazyTuple` wrapper defers that call past import; this test pins
        the deferral so a future rewrite back to a plain tuple concatenation
        (which would silently reintroduce the crash) fails loudly here
        instead of only on a real consumer's first install attempt.
        """
        import subprocess
        import sys
        import tempfile

        plugin_root = Path(__file__).parent.parent
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "lib"
            target.mkdir()
            for name in ("ci_setup.py", "atomic.py"):
                (target / name).write_bytes((plugin_root / "lib" / name).read_bytes())
            result = subprocess.run(
                [sys.executable, "-c", "from ci_setup import install_ci_config"],
                cwd=target,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode, 0,
                f"bare import failed without hooks/hooks.json present:\n"
                f"{result.stdout}{result.stderr}",
            )
            self.assertNotIn("FileNotFoundError", result.stderr)


class TestLinearToolsShip(unittest.TestCase):
    """Regression tests: hooks/linear-*.sh and hooks/worktree-auto-cut.sh
    all guard on `[ ! -f "$PROJECT_DIR/tools/linear_sync.py" ]` and silently
    `exit 0` when the file is missing. Without these scripts in
    EXPECTED_PATHS, every consumer repo after ci-setup would silently
    bail at that guard — issues never land in the consumer's Linear
    project (and the repo's own auto-sync is the only one that ever
    works, because dev-harness-kit itself carries the source file).
    """

    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_linear_files_in_expected_paths(self):
        for rel in (
            "tools/_repo_name.py",
            "tools/linear_sync.py",
            "tools/linear_pr_sync.py",
        ):
            self.assertIn(
                rel, self.ci_setup.EXPECTED_PATHS,
                f"missing from EXPECTED_PATHS: {rel} — hooks will silently bail "
                f"or raise ModuleNotFoundError on import",
            )

    def test_linear_entrypoints_not_in_executable_paths(self):
        """Python scripts invoked via `python3 <path>` (see every
        hooks/linear-*.sh and .github/workflows/linear-pr-sync.yml);
        shebang is not used, so +x is not required."""
        for rel in ("tools/linear_sync.py", "tools/linear_pr_sync.py"):
            self.assertNotIn(
                rel, self.ci_setup.EXECUTABLE_PATHS,
                f"{rel} must not be in EXECUTABLE_PATHS — invoked via python3",
            )

    def test_ci_setup_installs_linear_tools(self):
        """End-to-end install: all three files land byte-identical, the
        marker payload lists them under the `tools` key (drift guard
        without this entry would emit a warning on every --force), and
        a Python import of linear_sync from the consumer target succeeds
        (catches missing `_repo_name.py` dependency, which would
        otherwise raise ModuleNotFoundError on the first Edit/Write)."""
        import subprocess
        import sys
        import tempfile
        plugin_root = Path(__file__).parent.parent
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = self.ci_setup.install_ci_config(target, force=True)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            for rel in (
                "tools/_repo_name.py",
                "tools/linear_sync.py",
                "tools/linear_pr_sync.py",
            ):
                installed = target / rel
                self.assertTrue(installed.exists(), f"missing from install target: {rel}")
                self.assertEqual(
                    installed.read_bytes(), (plugin_root / rel).read_bytes(),
                    f"{rel} content mismatch vs plugin-root source",
                )
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertIn("tools", marker)
            self.assertIn("tools/_repo_name.py", marker["tools"])
            self.assertIn("tools/linear_sync.py", marker["tools"])
            self.assertIn("tools/linear_pr_sync.py", marker["tools"])
            # Import smoke: every consumer Edit/Write hook forks Python
            # to import linear_sync. A missing `_repo_name.py` shows up
            # as a ModuleNotFoundError on the very first hook fire, not
            # at install time — this regression pins the import.
            result = subprocess.run(
                [sys.executable, str(target / "tools" / "linear_sync.py"), "status"],
                cwd=target, capture_output=True, text=True,
            )
            self.assertEqual(
                result.returncode, 0,
                f"linear_sync.py execution failed in installed consumer target:\n"
                f"{result.stdout}{result.stderr}",
            )
            self.assertNotIn("ModuleNotFoundError", result.stderr)
            self.assertNotIn("ImportError", result.stderr)


def tempfile_path(name: str):
    """Return a Path to a tempfile file (helper for test_invalid_target_dir_raises)."""
    import tempfile
    fd, p = tempfile.mkstemp(prefix=f"ci_setup_{name}_", suffix=".txt")
    os.close(fd)
    return Path(p)


# --- per-function helpers (issue #91) -------------------------------------

class TestValidateTarget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_rejects_none(self):
        with self.assertRaises(FileNotFoundError):
            self.ci_setup._validate_target(None)

    def test_rejects_missing_dir(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope"
            with self.assertRaises(FileNotFoundError):
                self.ci_setup._validate_target(missing)

    def test_rejects_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "file"
            f.write_text("hi")
            with self.assertRaises(NotADirectoryError):
                self.ci_setup._validate_target(f)

    def test_accepts_directory(self):
        with tempfile.TemporaryDirectory() as td:
            resolved = self.ci_setup._validate_target(Path(td))
            self.assertEqual(resolved, Path(td).resolve())


class TestRunLintAndEmitSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_returns_list_on_empty_target(self):
        with tempfile.TemporaryDirectory() as td:
            warnings = self.ci_setup._run_lint_and_emit_summary(Path(td))
            self.assertIsInstance(warnings, list)


class TestInstallCiConfigDispatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_body_is_thin(self):
        """install_ci_config body should be < 80 logic lines after the split."""
        import inspect
        source = inspect.getsource(self.ci_setup.install_ci_config)
        logic_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertLess(
            len(logic_lines), 80,
            f"install_ci_config still too long: {len(logic_lines)} lines",
        )


class TestMarkerSchemaVersioning(unittest.TestCase):
    """Schema bump: marker records `installed_dev_kit_version` + `template_shas`.

    Closes the dev-kit ⇄ consumer gap: a consumer who ran /dev-kit:ci-setup
    at dev-kit v0.3.200 must be able to detect that v0.3.287 has shipped
    new templates without inspecting the live dev-kit source. The marker
    becomes the contract that makes drift queryable.
    """

    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def test_marker_records_installed_dev_kit_version_after_install(self):
        """Fresh install writes `installed_dev_kit_version` from plugin.json."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertIn("installed_dev_kit_version", marker)
            self.assertTrue(
                marker["installed_dev_kit_version"],
                "installed_dev_kit_version must be non-empty",
            )
            # The value should equal the runtime plugin_version() reading.
            self.assertEqual(
                marker["installed_dev_kit_version"],
                self.ci_setup.plugin_version(),
            )

    def test_marker_records_template_shas_for_each_expected_path(self):
        """Fresh install writes `template_shas` keyed by every EXPECTED_PATHS entry."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertIn("template_shas", marker)
            self.assertIsInstance(marker["template_shas"], dict)
            # Every EXPECTED_PATH must have a template_sha (or be skipped if source missing).
            for rel in self.ci_setup.EXPECTED_PATHS:
                self.assertIn(rel, marker["template_shas"], f"missing template_sha for {rel}")
                sha = marker["template_shas"][rel]
                self.assertEqual(len(sha), 64, f"template_sha for {rel} not 64-hex: {sha!r}")

    def test_template_sha_matches_dev_kit_source_bytes(self):
        """`template_shas[rel]` is the SHA of the dev-kit source, not the consumer copy.

        The source for `<rel>` is `templates/ci/<rel>` per
        `_resolve_template_source` — NOT the dev-kit project's own
        `.github/<rel>`. The templates tree is what ci-setup ships to
        consumers; the dev-kit repo's own `.github/` is a separate set of
        files. This test pins the distinction so future refactors cannot
        silently hash the wrong file.
        """
        import hashlib
        import tempfile
        plugin_root = PROJECT_ROOT
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            # Pick one file we know exists in the templates/ci/ source tree
            rel = ".github/workflows/ci.yml"
            src_path = plugin_root / "templates" / "ci" / rel
            if src_path.is_file():
                expected = hashlib.sha256(src_path.read_bytes()).hexdigest()
                self.assertEqual(marker["template_shas"][rel], expected)
                # After a clean install, the consumer copy IS byte-identical
                # to the template — this is the documented behavior of
                # ci-setup. Pin it so a future refactor that breaks the
                # copy step cannot silently desync the two.
                consumer_sha = hashlib.sha256((target / rel).read_bytes()).hexdigest()
                self.assertEqual(consumer_sha, expected)

    def test_backfill_writes_version_fields_on_idempotent_reinstall(self):
        """v1.0.0 marker (no version field) → next install backfills without file changes."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            # Simulate a v1.0.0 marker by stripping the new fields
            marker_path = target / ".dev-kit" / "ci-config.json"
            marker = json.loads(marker_path.read_text())
            marker.pop("installed_dev_kit_version", None)
            marker.pop("template_shas", None)
            marker_path.write_text(json.dumps(marker))
            # Capture file SHAs to confirm no files are touched
            file_shas = {}
            for rel in self.ci_setup.EXPECTED_PATHS:
                p = target / rel
                if p.is_file():
                    file_shas[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            # Reinstall — should be a no-op file-wise but backfill the marker
            r = self.ci_setup.install_ci_config(target)
            self.assertEqual(r.errors, [], f"errors: {r.errors}")
            # Idempotent: no creates, no overwrites, all skipped
            self.assertEqual(r.created, [])
            self.assertEqual(r.overwritten, [])
            self.assertEqual(len(r.skipped), len(self.ci_setup.EXPECTED_PATHS))
            # Marker now has the new fields
            new_marker = json.loads(marker_path.read_text())
            self.assertIn("installed_dev_kit_version", new_marker)
            self.assertIn("template_shas", new_marker)
            self.assertEqual(
                new_marker["installed_dev_kit_version"],
                self.ci_setup.plugin_version(),
            )
            # No files were touched
            for rel, expected_sha in file_shas.items():
                p = target / rel
                if p.is_file():
                    actual = hashlib.sha256(p.read_bytes()).hexdigest()
                    self.assertEqual(actual, expected_sha, f"{rel} touched during backfill")

    def test_backfill_preserves_original_installed_at(self):
        """Backfill must not bump `installed_at` — historical timestamp preserved."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.ci_setup.install_ci_config(target)
            marker_path = target / ".dev-kit" / "ci-config.json"
            original = json.loads(marker_path.read_text())
            original_installed_at = original["installed_at"]
            # Strip new fields, sleep 1s so any timestamp bump is visible
            original.pop("installed_dev_kit_version", None)
            original.pop("template_shas", None)
            marker_path.write_text(json.dumps(original))
            import time as _time
            _time.sleep(1.05)
            # Backfill
            self.ci_setup.install_ci_config(target)
            new_marker = json.loads(marker_path.read_text())
            self.assertEqual(new_marker["installed_at"], original_installed_at,
                             "backfill must preserve the original installed_at")


if __name__ == "__main__":
    unittest.main(verbosity=2)
