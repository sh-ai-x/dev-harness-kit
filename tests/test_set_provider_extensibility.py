"""test_set_provider_extensibility.py — regression for issue #714.

`bin/set-provider.sh` keeps the ALLOWLIST + provider→secret `case` block
inline. To onboard a fourth provider, an operator has to touch 5 files
but currently `--help` documents none of them. This test pins the
newly-added surface:

  T1: `bin/set-provider.sh --help` includes the "TO ADD A NEW PROVIDER"
      recipe section header.
  T2: `bin/set-provider.sh --help` lists the 5 checklist items
      (ALLOWLIST, case block, gh secret set, review.yml choices,
      .env.example) so an operator does not have to grep blindly.
  T3: `bin/set-provider.sh --check-extensibility` exits 0 and stdout
      mentions: ALLOWLIST, the case block location,
      `.github/workflows/review.yml`, `.env.example`, `gh secret set`.
  T4: `bin/set-provider.sh --check-extensibility` references the
      actual allowlist values (minimax, anthropic, deepseek) so the
      operator can confirm the parse matches their working tree.
  T5: `bin/set-provider.sh --check-extensibility` output is stable —
      two consecutive runs produce identical stdout (no flaky paths,
      no timestamps).
  T6: `.env.example` "see also" comment points to `--help` and
      `--check-extensibility` so the operator finds the recipe from
      the env template.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "bin" / "set-provider.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _run(*args) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("CI_REVIEW_PROVIDER", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT),
        env=env,
    )


class ExtensibilityContract(unittest.TestCase):
    # T1: --help advertises the recipe section header.
    def test_help_includes_recipe_header(self) -> None:
        result = _run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TO ADD A NEW PROVIDER", result.stdout)

    # T2: --help lists the 5 checklist items so the operator can find
    # all touchpoints without grepping.
    def test_help_lists_five_checklist_items(self) -> None:
        result = _run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        needle_groups = [
            # Step 1: ALLOWLIST in the script.
            ["ALLOWLIST"],
            # Step 2: the case block mapping provider → API key.
            ["case", "API_KEY"],
            # Step 3: gh secret set example.
            ["gh secret", "set"],
            # Step 4: review.yml choices.
            [".github/workflows/review.yml"],
            # Step 5: .env.example.
            [".env.example"],
        ]
        for group in needle_groups:
            with self.subTest(group=group):
                for needle in group:
                    self.assertIn(
                        needle, result.stdout,
                        f"--help recipe missing {needle!r} (group {group!r})",
                    )

    # T3: --check-extensibility exits 0 and references the same 5
    # files/concepts plus gh secret set.
    def test_check_extensibility_exits_zero_and_lists_touchpoints(self) -> None:
        result = _run("--check-extensibility")
        self.assertEqual(
            result.returncode, 0,
            f"--check-extensibility failed: stderr={result.stderr!r} stdout={result.stdout!r}",
        )
        required = [
            "ALLOWLIST",
            "case",
            ".github/workflows/review.yml",
            ".env.example",
            "gh secret set",
        ]
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, result.stdout,
                              f"--check-extensibility missing {needle!r}")

    # T3b: the line numbers printed by --check-extensibility must point at
    # the editable anchor in each file, not a comment that mentions the
    # same keyword. Regression net for the choices_line / env_key_line
    # grep fall-back that landed operators on prose.
    def test_check_extensibility_line_numbers_hit_editable_anchors(self) -> None:
        result = _run("--check-extensibility")
        self.assertEqual(result.returncode, 0, result.stderr)

        def _line(path: str, step_marker: str) -> int:
            # Checklist rows look like:
            #   "  3. .github/workflows/review.yml:59      # workflow_dispatch.inputs.review_provider.options  — extend the choice list."
            # Match by step marker (" 3. " / " 4. ") to avoid comment
            # text matching the same path/keyword.
            for ln in result.stdout.splitlines():
                if not ln.lstrip().startswith(step_marker):
                    continue
                if path not in ln:
                    continue
                after = ln.split(f"{path}:", 1)[1]
                digits = ""
                for ch in after:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                self.assertTrue(
                    digits, f"could not parse line number from {ln!r}",
                )
                return int(digits)
            self.fail(
                f"checklist step {step_marker!r} for {path!r} not found in stdout"
            )

        review_line = _line(".github/workflows/review.yml", "3.")
        env_line = _line(".env.example", "4.")

        review_text = (REPO_ROOT / ".github" / "workflows" / "review.yml").read_text()
        env_text = ENV_EXAMPLE.read_text()
        review_row = review_text.splitlines()[review_line - 1]
        env_row = env_text.splitlines()[env_line - 1]

        self.assertRegex(
            review_row, r"^\s*options:",
            f"review.yml:{review_line} must be the options: block, got: {review_row!r}",
        )
        self.assertRegex(
            env_row, r"^CI_REVIEW_PROVIDER=",
            f".env.example:{env_line} must be the CI_REVIEW_PROVIDER= assignment, got: {env_row!r}",
        )

    # T4: the checklist reflects the live allowlist, not stale text.
    def test_check_extensibility_refs_live_allowlist(self) -> None:
        result = _run("--check-extensibility")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("minimax", "anthropic", "deepseek"):
            with self.subTest(provider=name):
                self.assertIn(name, result.stdout,
                              f"current provider {name!r} missing from checklist")

    # T5: two consecutive runs produce identical stdout. The script must
    # not embed timestamps or random IDs that would break doctest-style
    # snapshot tests downstream.
    def test_check_extensibility_is_stable(self) -> None:
        r1 = _run("--check-extensibility")
        r2 = _run("--check-extensibility")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(
            r1.stdout, r2.stdout,
            "--check-extensibility output must be byte-stable across runs",
        )

    # T6: .env.example tells the operator where to find the recipe.
    def test_env_example_points_to_help_and_check(self) -> None:
        text = ENV_EXAMPLE.read_text()
        self.assertIn("--help", text,
                      ".env.example should reference bin/set-provider.sh --help")
        self.assertIn("--check-extensibility", text,
                      ".env.example should reference --check-extensibility")


if __name__ == "__main__":
    unittest.main()
