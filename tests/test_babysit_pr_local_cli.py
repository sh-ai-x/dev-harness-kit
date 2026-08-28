"""test_babysit_pr_local_cli.py — unit tests for the local-mode routing
helper added to lib/babysit_pr_cli.py.

Coverage:
  `is_local_mode(argv)`
    T1: empty argv -> False
    T2: ["--local-mode"] -> True
    T3: ["--pr", "42"] -> False (other flags do not trigger)
    T4: ["--local-test-cmd", "make test"] -> False
    T5: ["--local-mode", "--pr", "522"] -> True (orthogonal to --pr)

  `parse_babysit_args` integration with --local-mode (hidden flag):
    T6: --local-mode -> ns.local_mode=True, default is False
    T7: argparse --help output does NOT list --local-mode (hidden via
        argparse.SUPPRESS so operators never see it; L5 compliance).
    T8: parse_babysit_args(["--local-mode"]) returns a Namespace
        without raising SystemExit (the helper accepts it; it's a
        hidden flag, not an unknown one).
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import babysit_pr_cli as bpc  # noqa: E402


class TestIsLocalMode(unittest.TestCase):
    """Pure helper: any caller can pre-scan argv for the route without
    invoking `parse_babysit_args` (which raises on unknown flags and
    prefers `sys.argv`). Keeping this pure means the routing decision
    has its own contract tests independent of the parser."""

    def test_empty_argv_returns_false(self) -> None:
        """T1."""
        self.assertFalse(bpc.is_local_mode([]))

    def test_hidden_flag_returns_true(self) -> None:
        """T2: presence of `--local-mode` flips the route on."""
        self.assertTrue(bpc.is_local_mode(["--local-mode"]))

    def test_other_flags_do_not_trigger(self) -> None:
        """T3: --pr alone is the CI-driven babysit flow, not local mode."""
        self.assertFalse(bpc.is_local_mode(["--pr", "42"]))

    def test_local_test_cmd_alone_does_not_trigger(self) -> None:
        """T4: `--local-test-cmd` only names the pytest command; it does
        NOT route to local mode. Only the explicit `--local-mode` flag
        selects the /dev-kit:babysit-pr-local algorithm."""
        self.assertFalse(bpc.is_local_mode(
            ["--local-test-cmd", "make test"]
        ))

    def test_local_mode_orthogonal_with_pr(self) -> None:
        """T5: --local-mode composes with --pr (the hidden escape
        hatch for babysitting a PR whose owning branch is a different
        worktree). Both flags are accepted; is_local_mode returns True.
        """
        self.assertTrue(bpc.is_local_mode(["--local-mode", "--pr", "522"]))


class TestLocalModeParserIntegration(unittest.TestCase):
    """Pin the parser-level contract for the hidden flag. L5 (no
    operator-visible flags) requires `--help` to suppress --local-mode;
    the helpers in lib.babysit_pr_cli must never leak it to operator
    output."""

    def test_default_local_mode_is_false(self) -> None:
        """T6: ns.local_mode defaults to False (additive flag; the
        existing CI-driven babysit-pr flow is untouched)."""
        ns = bpc.parse_babysit_args([])
        self.assertFalse(ns.local_mode)

    def test_local_mode_sets_namespace_field(self) -> None:
        """T6: --local-mode -> ns.local_mode=True."""
        ns = bpc.parse_babysit_args(["--local-mode"])
        self.assertTrue(ns.local_mode)

    def test_help_does_not_list_local_mode(self) -> None:
        """T7: argparse --help output excludes --local-mode (hidden via
        argparse.SUPPRESS so the skill-only no-flag UX holds; L5
        compliance). Capture argparse's stdout via redirect.
        """
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(buf):
                bpc.parse_babysit_args(["--help"])
        rendered = buf.getvalue()
        self.assertNotIn("--local-mode", rendered)
        # `--local-test-cmd` is likewise hidden: it is a power-user
        # override for the local-mode skill, not an operator-facing flag.
        self.assertNotIn("--local-test-cmd", rendered)
        # Sanity: documented flags still appear in --help (regression
        # guard against an over-aggressive SUPPRESS).
        self.assertIn("--operator-is-only-human", rendered)

    def test_local_mode_accepted_without_systemexit(self) -> None:
        """T8: --local-mode is registered (not unknown), so argparse
        does not raise SystemExit on the hidden flag. The skill body
        reaches this path; a regression that accidentally drops the
        add_argument call would surface here."""
        try:
            ns = bpc.parse_babysit_args(["--local-mode"])
        except SystemExit as exc:  # pragma: no cover -- defensive only
            self.fail(
                f"parse_babysit_args raised SystemExit({exc.code}) on "
                "--local-mode; the hidden flag should be accepted, not unknown."
            )
        self.assertTrue(ns.local_mode)


if __name__ == "__main__":
    unittest.main()
