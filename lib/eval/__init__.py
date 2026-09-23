"""lib.eval — package split of lib/eval_runner.py (PR-E).

Currently contains:
    _rubric.py — RubricRegistry + CaseResult + mock/exception helpers

The rest of eval_runner (discovery / judgment / report / session-log
judge / golden diff / CLI) stays in lib/eval_runner.py for now — those
sections are heavier and their cross-references don't justify the split
yet (see the inspect report for the future-PR plan).
"""
from ._rubric import (  # noqa: F401
    CaseResult,
    RubricRegistry,
    _coerce_score,
    exception_rot,
    mock_drift_warning,
    mock_skipped,
    real_result,
)
