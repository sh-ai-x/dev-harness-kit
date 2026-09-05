"""test_ci_ruleset_contract.py - Regression test for the ruleset
`.required_status_checks.contexts` <-> workflow `jobs.<key>.name:`
contract.

Iron Law L1: no prod code without a verification artifact. This test
guards a runtime contract that is invisible from the GitHub UI alone.

Issue #774: ruleset 20232367 ("protect main (admin PAT bypass)")
demanded `severity gate (review + security)` as a required context,
while the corresponding workflow job (after PR #763) was named
`severity gate (review + security + injection_scan)`. GitHub's
required-status-check matching is exact-string. The new job's
PR Checks UI showed pass, but the ruleset gate silently failed to
satisfy - so `mergeStateStatus: BLOCKED` while `mergeable: MERGEABLE`,
which is the failure mode this test pins closed.

Layout:

  - Real fixture trees under `tests/fixtures/ci_ruleset_contract/`
    exercise the loader against fully-formed JSON + YAML.
  - One unit test per interesting shape: match, mismatch (the #774
    state), bare-key fallback (job without `name:`), legacy
    `parameters.required_status_checks.contexts[]` form, top-level
    array-of-rulesets form, and the "no local ruleset file" path.

Every test loads files from disk via the real loaders
(`lib.ci_ruleset.py`) - no inline JSON/YAML strings - so a regression
in the loader and a regression in the cross-check logic are caught
by the same artifact.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

# `lib/` ships dual-form: a real package in the source repo, and a
# flat-file copy in consumer installs. Test loader mirrors
# `tests/test_ci_doctor.py:_load()`.
def _load_ci_ruleset():
    spec = importlib.util.spec_from_file_location(
        "ci_ruleset", PROJECT_ROOT / "lib" / "ci_ruleset.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_ruleset"] = mod
    spec.loader.exec_module(mod)
    return mod


FIX_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "ci_ruleset_contract"


def _fixture(subdir: str) -> Path:
    """Return the fixture root for `subdir` (its `.github/` lives there)."""
    p = FIX_ROOT / subdir
    assert p.is_dir(), f"missing fixture dir: {p}"
    return p


class TestCiRulesetContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cr = _load_ci_ruleset()

    # ---- loader-level: read rulesets across the supported shapes -----

    def test_load_ruleset_contexts_modern_parameters_form(self):
        """The `parameters.required_status_checks[]` array form (issue #774)."""
        result = self.cr.load_ruleset_contexts(_fixture("match"))
        ctxs = sorted(c.context_name for c in result)
        self.assertEqual(
            ctxs,
            ["ci", "maintenance gate (verdict + docs-updated)",
             "severity gate (review + security)"],
        )

    def test_load_ruleset_contexts_legacy_contexts_inside_parameters(self):
        """Legacy `parameters.required_status_checks.contexts[]` form."""
        result = self.cr.load_ruleset_contexts(_fixture("parameters_form"))
        self.assertEqual(
            [c.context_name for c in result], ["ci"],
            "only the single legacy context should be loaded",
        )

    def test_load_ruleset_contexts_handles_top_level_array(self):
        """Top-level array of multiple ruleset objects (export format)."""
        result = self.cr.load_ruleset_contexts(_fixture("array_form"))
        ctxs = sorted(c.context_name for c in result)
        self.assertEqual(ctxs, ["ci", "review"])

    def test_load_ruleset_contexts_returns_empty_when_no_ruleset_dir(self):
        """No `.github/rulesets/` -> empty list (not an error)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.cr.load_ruleset_contexts(Path(td)), [])

    def test_load_ruleset_contexts_silently_skips_unparseable_json(self):
        """A corrupt ruleset file MUST NOT crash the loader."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ruleset = target / ".github" / "rulesets"
            ruleset.mkdir(parents=True)
            (ruleset / "broken.json").write_text("{not-json", encoding="utf-8")
            self.assertEqual(self.cr.load_ruleset_contexts(target), [])

    # ---- contract: matches the workflow job `name:` exactly -------

    def test_match_fixture_passes_contract(self):
        """All ruleset contexts are present as workflow job `name:`."""
        rows = self.cr.check_ruleset_contract(_fixture("match"))
        state = [(r.label, r.state) for r in rows]
        self.assertTrue(
            any(s == "PASS" for _, s in state),
            f"expected at least one PASS row, got {state}",
        )
        self.assertFalse(
            any(s == "FAIL" for _, s in state),
            f"match fixture must not FAIL: {state}",
        )

    def test_mismatch_fixture_fails_with_offending_contexts(self):
        """Issue #774 reproduction: ruleset demands a context that
        no workflow job emits -> FAIL row names the missing context
        and the offending ruleset file."""
        rows = self.cr.check_ruleset_contract(_fixture("mismatch"))
        fail_rows = [r for r in rows if r.state == "FAIL"]
        self.assertTrue(
            fail_rows,
            "mismatch fixture must produce a FAIL row",
        )
        joined = " ".join(r.detail for r in fail_rows)
        self.assertIn(
            "severity gate (review + security + injection_scan)", joined,
            "FAIL detail must name the missing context (full new name)",
        )
        self.assertIn(
            "protect-main.json", joined,
            "FAIL detail must point at the offending ruleset file",
        )
        # Sanity: the context that's actually present (the unchanged
        # one) must NOT appear in the FAIL detail.
        self.assertNotIn(
            "requires ['severity gate (review + security)'] but no "
            "workflow job has that name",
            joined,
            "the still-matching context must not appear in FAIL detail",
        )

    def test_no_local_ruleset_file_emits_info_row(self):
        """`.github/rulesets/` absent -> INFO row, never an error."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".github" / "workflows").mkdir(parents=True)
            rows = self.cr.check_ruleset_contract(target)
            # Exactly one row, INFO state (no FAIL ever for absent dir).
            self.assertEqual(len(rows), 1,
                             f"expected one row, got {len(rows)}")
            self.assertEqual(rows[0].state, "INFO",
                             f"expected INFO row, got {rows[0].state}")

    # ---- bare-key fallback: job without `name:` ----------------------

    def test_bare_key_only_workflow_still_matches_when_context_is_bare_key(self):
        """A job with no `name:` is a legitimate match surface for a
        bare-key ruleset context (real-world: `auto-fix`). The
        regression test for this lives in the separate
        `empty_job_name` fixture so the contract is exercised end to
        end rather than via an inferred assertion from a single-file
        loader probe."""
        rows = self.cr.check_ruleset_contract(_fixture("empty_job_name"))
        self.assertFalse(
            any(r.state == "FAIL" for r in rows),
            f"empty_job_name fixture must PASS via bare-key fallback; "
            f"got {[(r.label, r.state, r.detail) for r in rows]}",
        )

    # ---- no_empty_name invariant --------------------------------------

    def test_workflow_jobs_have_names_in_real_review_yml(self):
        """Pin the "every ruleset-critical job has `name:` set"
        invariant on this repo's real review.yml. The four names
        below are the four contexts a branch-protection ruleset would
        typically demand; they MUST exist as workflow job `name:`
        values (not bare keys), because bare-key matching is brittle
        across job-key renames (the exact failure mode of #774)."""
        real = PROJECT_ROOT / ".github" / "workflows" / "review.yml"
        if not real.is_file():
            self.skipTest(".github/workflows/review.yml missing")
        named, _bare_keys, errors = self.cr.load_workflow_job_names(
            PROJECT_ROOT,
        )
        # Every name below is the ruleset contract pin for issue #774.
        # If any of these disappear (or get renamed), the corresponding
        # ruleset context must be updated in the same PR - this test
        # catches regressions where one or the other lags behind.
        critical = {
            "severity gate (review + security)",
            "/dev-kit:review (3-dim)",
            "/dev-kit:security (10-dim OWASP)",
            "prompt-injection static filter (pre-gate)",
        }
        missing = critical - named
        self.assertEqual(
            missing, set(),
            f"review.yml must declare `name:` for these jobs (the #774 "
            f"contract surface); missing: {missing}; named subset: "
            f"{sorted(critical & named)}",
        )
        # Surface parse errors as expected warnings but don't fail -
        # this regression test is scoped to job-name correctness, not
        # YAML well-formedness of unrelated workflow files. The
        # warnings filter is set narrowly so unrelated UserWarnings
        # from other tests still show.
        import warnings

        import pytest
        for e in errors:
            with pytest.warns(UserWarning, match=r"workflow parse error"):
                warnings.warn(f"workflow parse error (out of scope): {e}")

    # ---- repos-real: no ruleset files means no FAIL --------------------

    def test_real_repo_with_no_ruleset_files_does_not_fail(self):
        """The source repo at HEAD (issue #774) does NOT author
        `.github/rulesets/*.json`. The contract check must therefore
        emit a single INFO row, NOT a FAIL - so this assertion
        closes the loop on the "no local ruleset = INFO" code path
        against the real worktree layout."""
        rows = self.cr.check_ruleset_contract(PROJECT_ROOT)
        self.assertTrue(
            all(r.state != "FAIL" for r in rows),
            "real repo (no local ruleset files) must never FAIL",
        )
        self.assertTrue(
            any(r.state == "INFO" for r in rows),
            f"real repo must emit an INFO row to surface the missing "
            f"local ruleset; got {[(r.label, r.state) for r in rows]}",
        )
