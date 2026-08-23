#!/usr/bin/env python3
"""test_check_provider_consistency.py — Regression for issue #712.

Pins `lib/ci_setup.check_provider_consistency()` so the local
`.env:CI_REVIEW_PROVIDER` ↔ `vars.CI_REVIEW_PROVIDER` probe added to
`/dev-kit:ci-doctor` never silently regresses. Every status the function
emits (OK / WARN / SKIP) is exercised against a fake `.env` and a mocked
`_read_ci_provider_via_gh` so the test is independent of `gh` being
installed on the test host. Iron Law L1: regression test required for
this change; Iron Law L2: the WARN path is exercised with a literal
repro of the drift scenario from the issue body.

Behavior contract (issue #712):
  - OK    : both unset, OR both set to the same value
  - WARN  : exactly one set, OR both set but differ
  - SKIP  : gh absent / unauthenticated / variable-get errored
  - FAIL  : reserved (currently not emitted — `Check` allows it for future)

Runs under both `pytest tests/test_check_provider_consistency.py -v`
and `python -m unittest tests/test_check_provider_consistency.py`.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load_ci_setup():
    """Load `lib/ci_setup.py` by file path (mirrors test_ci_setup.py:24-35).

    `sys.modules[name]` registration happens BEFORE `exec_module` so the
    module-level `@dataclass` decorators in ci_setup resolve cross-module
    type lookups under Python 3.14's stricter resolver.
    """
    name = "ci_setup"
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "lib" / "ci_setup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestCheckProviderConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_setup = _load_ci_setup()

    def _check_with_gh(
        self,
        target: Path,
        *,
        ci_value: str = "",
        degraded_msg: str = "",
    ):
        """Run `check_provider_consistency(target)` with a fake gh reader.

        Patches `_read_ci_provider_via_gh` so the test does NOT require `gh`
        on PATH and does NOT depend on `gh auth status`. Mirrors how
        `tests/test_ci_doctor.py` patches the high-level gh checks rather
        than reaching into `subprocess.run` directly.
        """
        with patch.object(
            self.ci_setup,
            "_read_ci_provider_via_gh",
            return_value=(ci_value, degraded_msg),
        ):
            return self.ci_setup.check_provider_consistency(target)

    # --- OK paths -------------------------------------------------------

    def test_both_unset_returns_ok(self):
        """Issue #712 OK case 1: `.env:CI_REVIEW_PROVIDER` unset AND
        `vars.CI_REVIEW_PROVIDER` unset → OK with a message naming both."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            # No `.env` file at all → local reads empty
            status, msg = self._check_with_gh(target, ci_value="", degraded_msg="")
            self.assertEqual(status, "OK")
            self.assertIn("unset", msg)
            self.assertIn("CI_REVIEW_PROVIDER", msg)

    def test_both_set_equal_returns_ok(self):
        """Issue #712 OK case 2: `.env` and `vars` both set to the same
        value → OK with the shared value in the message."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".env").write_text(
                "OTHER_KEY=ignore_me\n"
                "CI_REVIEW_PROVIDER=minimax\n",
                encoding="utf-8",
            )
            status, msg = self._check_with_gh(target, ci_value="minimax", degraded_msg="")
            self.assertEqual(status, "OK")
            self.assertIn("minimax", msg)

    def test_both_set_equal_case_insensitive(self):
        """Local `Minimax` (mixed case) and CI `minimax` (lowercase) agree
        after lowercasing — the function lowercases both sides so this is OK.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=Minimax\n", encoding="utf-8")
            status, msg = self._check_with_gh(target, ci_value="minimax", degraded_msg="")
            self.assertEqual(status, "OK")

    # --- WARN paths (issue #712 Iron Law L2 — repro of the drift bug) --

    def test_local_and_ci_differ_returns_warn(self):
        """Issue #712 repro: `.env=anthropic` but CI=`minimax` → WARN with
        both values visible AND the `gh variable set` remediation command.

        This is the L2 regression: the literal scenario from the issue body
        ("Operator runs `bin/set-provider.sh anthropic` locally; forgets
        `gh variable set CI_REVIEW_PROVIDER --body anthropic`") MUST
        produce a WARN row with both values named verbatim.
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=anthropic\n", encoding="utf-8")
            status, msg = self._check_with_gh(target, ci_value="minimax", degraded_msg="")
            self.assertEqual(status, "WARN")
            self.assertIn("anthropic", msg)
            self.assertIn("minimax", msg)
            self.assertIn("gh variable set CI_REVIEW_PROVIDER", msg)
            # The remediation must echo the LOCAL value (the side the
            # operator can change), not the CI side.
            self.assertIn("--body anthropic", msg)

    def test_ci_set_local_unset_returns_warn(self):
        """Issue #712 WARN case: vars.CI_REVIEW_PROVIDER=`deepseek` but
        `.env:CI_REVIEW_PROVIDER` is unset → WARN with the CI value AND
        remediation to align both sides (either pull locally via
        `bin/set-provider.sh deepseek` or unset the variable)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            # No `.env` → local empty
            status, msg = self._check_with_gh(
                target, ci_value="deepseek", degraded_msg="",
            )
            self.assertEqual(status, "WARN")
            self.assertIn("deepseek", msg)
            self.assertIn("gh variable set CI_REVIEW_PROVIDER", msg)

    def test_local_set_ci_unset_returns_warn(self):
        """Issue #712 WARN case: `.env=minimax` but the repo has NO
        `CI_REVIEW_PROVIDER` variable set → WARN with remediation to push
        the local value to the repo (`gh variable set`)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8")
            # ci_value="" + degraded_msg="" means "gh returned successfully
            # with no variable set" (the not-found path the fake returns).
            status, msg = self._check_with_gh(target, ci_value="", degraded_msg="")
            self.assertEqual(status, "WARN")
            self.assertIn("minimax", msg)
            self.assertIn("unset", msg)
            self.assertIn("gh variable set CI_REVIEW_PROVIDER", msg)

    # --- SKIP paths -----------------------------------------------------

    def test_gh_absent_returns_skip(self):
        """`_read_ci_provider_via_gh` returns a degraded message → SKIP.
        Patches `shutil.which` to simulate `gh` absent from PATH so the
        test does not depend on the test host having `gh` installed."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".env").write_text("CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8")
            # Patch the function's view of `shutil.which`. `ci_setup`
            # imported `shutil` at module load time, so the binding lives
            # at `ci_setup.shutil.which` — patching the module attribute
            # there intercepts the function's lookup.
            with patch.object(self.ci_setup.shutil, "which", return_value=None):
                status, msg = self.ci_setup.check_provider_consistency(target)
            self.assertEqual(status, "SKIP")
            self.assertIn("PATH", msg)

    def test_gh_unauthenticated_returns_skip(self):
        """`_read_ci_provider_via_gh` reports unauth → SKIP. Uses the
        higher-level fake so this test stays independent of subprocess
        behavior and is readable as a contract check."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            status, msg = self._check_with_gh(
                target, ci_value="", degraded_msg="gh not authenticated",
            )
            self.assertEqual(status, "SKIP")
            self.assertEqual(msg, "gh not authenticated")

    def test_gh_variable_get_errored_returns_skip(self):
        """`_read_ci_provider_via_gh` reports a degraded `gh variable get`
        error (network, rate limit, etc.) → SKIP, not WARN. SKIP is the
        honest answer when we cannot confirm the CI value."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            status, msg = self._check_with_gh(
                target,
                ci_value="",
                degraded_msg="gh variable get failed: HTTP 500",
            )
            self.assertEqual(status, "SKIP")
            self.assertIn("HTTP 500", msg)

    # --- API contract ---------------------------------------------------

    def test_return_type_is_status_message_tuple(self):
        """The function returns a 2-tuple of strings. No dict, no dataclass
        — callers in ci-doctor pattern-match the first element via
        `state ∈ {"OK", "WARN", "SKIP", "FAIL"}`."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            result = self._check_with_gh(target, ci_value="", degraded_msg="")
            self.assertIsInstance(result, tuple)
            self.assertEqual(len(result), 2)
            self.assertIsInstance(result[0], str)
            self.assertIsInstance(result[1], str)

    def test_status_in_allowed_set(self):
        """Every status the function returns must be in the documented
        contract set {OK, WARN, SKIP, FAIL}. INFO is reserved for ci-doctor
        rows that this helper does NOT emit — it never raises INFO."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            allowed = {"OK", "WARN", "SKIP", "FAIL"}
            for local, ci in [
                ("", ""),
                ("minimax", "minimax"),
                ("anthropic", "minimax"),
                ("minimax", ""),
                ("", "deepseek"),
            ]:
                if local:
                    (target / ".env").write_text(
                        f"CI_REVIEW_PROVIDER={local}\n", encoding="utf-8",
                    )
                else:
                    (target / ".env").unlink(missing_ok=True)
                status, _ = self._check_with_gh(target, ci_value=ci, degraded_msg="")
                self.assertIn(
                    status, allowed,
                    f"local={local!r} ci={ci!r} → status={status!r} not in {allowed}",
                )


if __name__ == "__main__":
    unittest.main()
