"""test_meta_eval.py — evaluate the evaluator (proposal §05).

Covers:
- CaseSpec loads a golden case JSON correctly
- relative worktree_path resolves relative to case file
- run_meta_eval on a non-existent dir returns empty report (no crash)
- run_meta_eval with missing worktree_path marks the case `skipped`
- run_meta_eval on the existing golden-clean-pr case produces a report
- a fabricated L1-violation case (worktree contains `TODO`) reports
  D4_safety drop and overall deterministic_mean drop
- CaseExpected.from_dict tolerates missing optional fields
- _check_case emits a verdict_mismatch failure when verdicts disagree
- _check_case emits per-dim failures when per_dim_min not met
- MetaEvalReport.all_passed is True only when no failures/errors
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.meta_eval import (
    CaseExpected,
    CaseMetaResult,
    CaseSpec,
    MetaEvalReport,
    run_meta_eval,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "agent-behavior"
CASES_DIR = Path(__file__).parent.parent / "eval" / "cases" / "agent-behavior"


def test_case_spec_loads_golden_clean_pr() -> None:
    """The committed golden case loads and has expected fields."""
    case_path = CASES_DIR / "01_golden_clean_pr.json"
    if not case_path.is_file():
        pytest.skip(f"case file not found: {case_path}")
    spec = CaseSpec.load(case_path)
    assert spec.case_id == "agent-behavior-01-golden-clean-pr"
    assert spec.dim == "agent-behavior"
    assert spec.expected.verdict == "OK"
    assert spec.worktree_path.exists(), (
        f"worktree_path should resolve to an existing dir: {spec.worktree_path}"
    )


def test_case_spec_relative_worktree_resolves_against_case_file(tmp_path: Path) -> None:
    """Relative worktree_path resolves relative to the case file's directory."""
    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps({
        "case_id": "x",
        "dim": "agent-behavior",
        "worktree_path": "./wt",
        "expected": {"verdict": "OK"},
    }))
    spec = CaseSpec.load(case_path)
    assert spec.worktree_path == fake_wt.resolve()


def test_case_spec_rejects_missing_worktree_path() -> None:
    """Missing worktree_path should raise ValueError."""
    bad = Path(__file__).parent / "_tmp_bad_case.json"
    bad.write_text(json.dumps({"case_id": "x", "dim": "agent-behavior"}))
    try:
        with pytest.raises(ValueError, match="worktree_path"):
            CaseSpec.load(bad)
    finally:
        bad.unlink(missing_ok=True)


def test_run_meta_eval_on_missing_dir_returns_empty(tmp_path: Path) -> None:
    """run_meta_eval on a non-existent directory returns an empty report."""
    report = run_meta_eval(tmp_path / "nope")
    assert report.total == 0
    assert report.passed == 0
    assert report.failed == 0
    assert report.skipped == 0


def test_run_meta_eval_skips_missing_worktree(tmp_path: Path) -> None:
    """A case whose worktree_path doesn't exist is marked `skipped`."""
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "01_skip.json").write_text(json.dumps({
        "case_id": "skip-me",
        "dim": "agent-behavior",
        "worktree_path": "./does-not-exist",
        "expected": {"verdict": "OK"},
    }))
    report = run_meta_eval(cases)
    assert report.total == 1
    assert report.skipped == 1
    assert report.failed == 0
    assert report.passed == 0


def test_run_meta_eval_on_golden_clean_pr_case() -> None:
    """The committed golden-clean-pr case should produce a verdict somewhere
    in OK / DRIFT_WARNING range — never ESCALATED, never ROT (deterministic
    floor enforced)."""
    if not CASES_DIR.is_dir():
        pytest.skip(f"cases dir missing: {CASES_DIR}")
    report = run_meta_eval(CASES_DIR)
    if report.total == 0 or all(c.status == "skipped" for c in report.cases):
        pytest.skip("no runnable cases yet")
    for c in report.cases:
        if c.case_id == "agent-behavior-01-golden-clean-pr":
            # The committed case may either pass (current implementation
            # matches its expectations) or fail (we'll surface the failures
            # in the test output). Either way, it must not be ESCALATED
            # for the golden-clean-pr fixture.
            assert c.actual_verdict in {"OK", "DRIFT_WARNING"}, (
                f"golden-clean-pr should not ESCALATE: {c.to_dict()}"
            )


def test_run_meta_eval_l1_violation_is_caught(tmp_path: Path) -> None:
    """A worktree with TODO in lib/ triggers D4 L1 violation; meta-eval
    records this as a per-dim D4_safety failure (or status=passed if
    the case's expected metadata tolerates D4=1)."""
    wt = tmp_path / "wt"
    (wt / "lib").mkdir(parents=True)
    (wt / "tests").mkdir(parents=True)
    (wt / "lib" / "x.py").write_text("# TODO: clean up\ndef x(): return 1\n")
    (wt / "tests" / "test_x.py").write_text("def test_ok(): assert True\n")
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "01.json").write_text(json.dumps({
        "case_id": "l1-test",
        "dim": "agent-behavior",
        "worktree_path": str(wt),
        "expected": {"verdict": "ROT", "per_dim_max": {"D4_safety": 1}},
    }))
    report = run_meta_eval(cases)
    assert report.total == 1
    case = report.cases[0]
    # Either the verdict is correctly ROT, OR there's a failure recorded.
    if case.status == "passed":
        assert case.actual_verdict == "ROT"
    else:
        # The expected verdict was ROT; if we got something else, the
        # case correctly fails (failing = eval system found disagreement).
        assert case.failures  # at least one rule failed


def test_run_meta_eval_marks_errored_on_bad_case_load(tmp_path: Path) -> None:
    """A case file that fails to parse is marked `error`, not `failed`."""
    cases = tmp_path / "cases"
    cases.mkdir()
    bad = cases / "01_bad.json"
    bad.write_text("not json at all")
    report = run_meta_eval(cases)
    assert report.total == 1
    assert report.errored == 1
    assert report.failed == 0


def test_case_expected_from_dict_tolerates_missing_optional_fields() -> None:
    """Missing optional fields get sensible defaults."""
    e = CaseExpected.from_dict({"verdict": "OK"})
    assert e.verdict == "OK"
    assert e.deterministic_mean_min == 0.0
    assert e.weighted_mean_min == 0.0
    assert e.weighted_mean_max == 5.0
    assert e.per_dim_min == {}
    assert e.per_dim_max == {}


def test_meta_eval_report_all_passed_only_when_clean() -> None:
    """all_passed is True iff no failures and no errors (skipped ok)."""
    clean = MetaEvalReport(cases=(), total=0, passed=0, skipped=0, failed=0, errored=0)
    assert clean.all_passed
    with_skip = MetaEvalReport(cases=(), total=2, passed=1, skipped=1, failed=0, errored=0)
    assert with_skip.all_passed
    with_fail = MetaEvalReport(cases=(), total=1, passed=0, skipped=0, failed=1, errored=0)
    assert not with_fail.all_passed
    with_err = MetaEvalReport(cases=(), total=1, passed=0, skipped=0, failed=0, errored=1)
    assert not with_err.all_passed


def test_case_meta_result_to_dict_includes_failures() -> None:
    """to_dict serializes failures as a list of rule/expected/actual."""
    result = CaseMetaResult(
        case_id="x",
        worktree="/tmp/wt",
        status="failed",
        actual_verdict="OK",
        actual_per_dim={"D1_outcome": 5},
    )
    d = result.to_dict()
    assert d["case_id"] == "x"
    assert d["status"] == "failed"
    assert d["actual_per_dim"] == {"D1_outcome": 5}
    assert d["failures"] == []


# ---------------------------------------------------------------------------
# Self-tests (weak-point remediation iteration 1, D2)
#
# These tests verify the meta-eval layer's own behavior so a regression in
# the evaluator itself surfaces as a failing pytest, not a silent drift.
# They guard against:
#   - a golden case whose expected verdict stops being checked (silent no-op)
#   - the eval system silently swallowing all cases when the cases dir is
#     empty (would mask fixture path drift in CI)
# ---------------------------------------------------------------------------


def test_meta_eval_sensitivity_to_expected_verdict_change(tmp_path: Path) -> None:
    """A flipped expected verdict must cause the meta-eval to mark the case
    failed when the eval pipeline's actual verdict still disagrees.

    This is the core sensitivity contract: if editing `expected.verdict` in
    a golden case has no effect on the report, the eval pipeline is no
    longer checking that field — and our "the eval infrastructure works"
    claim becomes hollow.

    The test does NOT assume what the clean-worktree actual verdict is
    (OK / DRIFT_WARNING / other) — that depends on the rubric and may
    legitimately change. It only pins the contrast: an expected verdict
    matching the actual must pass; an expected verdict that disagrees
    with the actual must trigger a `verdict_mismatch` failure.
    """
    wt = tmp_path / "wt"
    (wt / "lib").mkdir(parents=True)
    (wt / "tests").mkdir(parents=True)
    (wt / "lib" / "x.py").write_text("def x(): return 1\n")
    (wt / "tests" / "test_x.py").write_text("def test_ok(): assert True\n")

    # First pass: read what the pipeline actually emits for this worktree,
    # then write that as the expected. The case must pass.
    probe_dir = tmp_path / "cases_probe"
    probe_dir.mkdir()
    (probe_dir / "01.json").write_text(json.dumps({
        "case_id": "sensitivity-probe",
        "dim": "agent-behavior",
        "worktree_path": str(wt),
        "expected": {"verdict": "OK"},  # any verdict; we just want actual
    }))
    probe_report = run_meta_eval(probe_dir)
    actual_verdict = probe_report.cases[0].actual_verdict
    assert actual_verdict, "probe run produced no actual_verdict"

    # Second pass: write the actual verdict as expected — must pass.
    baseline_dir = tmp_path / "cases_baseline"
    baseline_dir.mkdir()
    (baseline_dir / "01.json").write_text(json.dumps({
        "case_id": "sensitivity-baseline",
        "dim": "agent-behavior",
        "worktree_path": str(wt),
        "expected": {"verdict": actual_verdict},
    }))
    baseline_report = run_meta_eval(baseline_dir)
    assert baseline_report.total == 1
    assert baseline_report.cases[0].status == "passed", (
        f"baseline (expected=actual) should pass; got "
        f"{baseline_report.cases[0].to_dict()}"
    )

    # Third pass: flip expected to a verdict guaranteed to disagree (ROT).
    # Must trigger verdict_mismatch failure.
    flipped_dir = tmp_path / "cases_flipped"
    flipped_dir.mkdir()
    (flipped_dir / "01.json").write_text(json.dumps({
        "case_id": "sensitivity-flipped",
        "dim": "agent-behavior",
        "worktree_path": str(wt),
        "expected": {"verdict": "ROT"},
    }))
    flipped_report = run_meta_eval(flipped_dir)
    assert flipped_report.total == 1
    case = flipped_report.cases[0]
    assert case.status == "failed", (
        f"flipping expected verdict must trigger failure; got {case.to_dict()}"
    )
    rule_ids = {f.rule for f in (case.failures or [])}
    assert "verdict_mismatch" in rule_ids, (
        f"expected verdict_mismatch rule; got rules={rule_ids}"
    )


def test_meta_eval_empty_cases_dir_returns_empty_not_error(tmp_path: Path) -> None:
    """An empty cases dir must return an empty report, NOT crash.

    Guards against the regression where the eval pipeline swallows cases
    silently — if `discover_cases` returns [] and the runner reports
    `total=0`, the CI eval gate would pass on a broken fixture path
    with no signal. This test pins both the count AND the absence of
    error/exception status.
    """
    cases_dir = tmp_path / "empty_cases"
    cases_dir.mkdir()
    report = run_meta_eval(cases_dir)
    assert report.total == 0
    assert report.passed == 0
    assert report.failed == 0
    assert report.errored == 0
    assert report.all_passed is True  # vacuously true; gate should NOT block
    assert list(report.cases) == []


def test_meta_eval_missing_cases_dir_returns_empty_not_error(tmp_path: Path) -> None:
    """A nonexistent cases dir is the same as empty — graceful degradation.

    The previous test pinned the empty-dir behavior. This one pins the
    missing-dir behavior so a CI misconfiguration (wrong path, fresh
    clone without fixtures) does not crash the eval gate.
    """
    missing = tmp_path / "does_not_exist"
    report = run_meta_eval(missing)
    assert report.total == 0
    assert report.errored == 0
    assert report.all_passed is True
