# ---------- lib/eval/_rubric.py (PR-E) ----------
"""RubricRegistry + CaseResult + mock/exception helpers for eval_runner.

Extracted from lib/eval_runner.py per PR-E. The rubric section owns:
  - RubricRegistry — class-level registry of named eval rubrics
  - CaseResult — dataclass for one case's judge output
  - mock_skipped / mock_drift_warning / real_result / exception_rot — helpers
  - _coerce_score — strict int/float parser used by every judge
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

# ---------- RubricRegistry (Phase 3) ----------
#
# Class-level registry of named eval rubrics. Each entry pairs a YAML
# rubric path with the judge prompt path used to score it. The default
# registry is empty so existing call sites that rely on the legacy
# case-fixture + DIM_AXES path are untouched (backward-compat).
#
# `version` is a monotonic counter bumped on every successful
# `register()` so audit consumers can detect registry drift without
# diffing the full entry set.
#
# Iron Law L1: the registry is the deterministic counterpart to the
# LLM judge prompt — it lets `skills/evaluate` (`alpha: enforcement`)
# gate on a registered rubric before invoking the LLM, so a caller
# cannot ask the judge to score an unknown rubric.

class RubricRegistry:
    """Class-level registry of named eval rubrics.

    `register()` adds a (rubric_yaml_path, judge_prompt_path) pair
    under a kebab-case name and bumps `version`. `lookup()` returns
    the pair by name (raises KeyError on miss). `get_rubric()` is the
    convenience accessor returning just the YAML path.

    The registry is intentionally empty at import time — opt-in. The
    `evaluate` skill calls `register()` for each rubric it ships with
    (harness-quality, os-quality) so the public API is stable.
    """

    _entries: dict = {}
    version: int = 0

    @classmethod
    def register(
        cls,
        name: str,
        rubric_yaml_path: str,
        judge_prompt_path: str,
    ) -> None:
        """Add or replace an entry under `name`. Bumps `version`."""
        if not isinstance(name, str) or not name:
            raise ValueError(f"rubric name must be a non-empty string, got {name!r}")
        cls._entries[name] = {
            "rubric_yaml_path": rubric_yaml_path,
            "judge_prompt_path": judge_prompt_path,
        }
        cls.version += 1

    @classmethod
    def lookup(cls, name: str) -> dict:
        """Return the entry dict for `name`. Raises KeyError on miss."""
        if name not in cls._entries:
            raise KeyError(
                f"unknown rubric: {name!r}. Registered: {sorted(cls._entries)}"
            )
        return cls._entries[name]

    @classmethod
    def get_rubric(cls, name: str) -> str:
        """Convenience: return just the rubric YAML path for `name`."""
        return cls.lookup(name)["rubric_yaml_path"]

    @classmethod
    def clear(cls) -> None:
        """Reset registry. Test-only helper."""
        cls._entries = {}
        cls.version = 0

    @classmethod
    def names(cls) -> tuple:
        """Return all registered rubric names (sorted)."""
        return tuple(sorted(cls._entries))


@dataclass
class CaseResult:
    """One case outcome from run_eval.

    Mutable because _judge_case populates fields incrementally before
    returning; converted to dict at the API boundary via asdict().
    """
    case_id: str = ""
    dim: str = ""
    scores: Dict[str, float] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: str = ""
    verdict: str = ""
    score: float = 0.0
    error: Optional[str] = None


def mock_skipped(case: Dict, axes: tuple) -> CaseResult:
    return CaseResult(
        case_id=case["case_id"], dim=case["dim"],
        scores={ax: 0.0 for ax in axes},
        raw="TRANSCRIPT_MISSING", verdict="SKIPPED", score=0.0,
    )


def mock_drift_warning(case: Dict, axes: tuple) -> CaseResult:
    return CaseResult(
        case_id=case["case_id"], dim=case["dim"],
        scores={ax: 7.0 for ax in axes},
        raw="DRY_RUN", verdict="DRIFT_WARNING", score=7.0,
    )


def real_result(case: Dict, *, scores: Dict[str, float],
                tokens_in: int, tokens_out: int,
                raw: str, verdict: str, score: float) -> CaseResult:
    return CaseResult(
        case_id=case["case_id"], dim=case["dim"],
        scores=scores, tokens_in=tokens_in, tokens_out=tokens_out,
        raw=raw, verdict=verdict, score=score,
    )


def exception_rot(case: Dict, axes: tuple, exc: Exception) -> CaseResult:
    return CaseResult(
        case_id=case["case_id"], dim=case["dim"],
        scores={ax: 0.0 for ax in axes},
        raw=str(exc), verdict="ROT", score=0.0, error=str(exc),
    )


def _coerce_score(raw: object) -> Optional[float]:
    """Coerce a raw axis-score value to a float, or None on failure.

    Shared between `_judge_case` (per-dim scores from the LLM) and
    `run_golden_diff` (golden baseline scores from JSON). Returns None
    for non-numeric inputs so the caller can skip the axis entirely
    instead of silently treating bad data as 0.0.
    """
    if raw is None:
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


