"""robustness.py — D6 Robustness (scenario fixtures).

Phase 0 placeholder. Real scenario fixtures (compile-error, flaky-test,
missing-dep, conflicting-instructions, resource-exhaustion) land in
Phase 2 (proposal §03). For now this scorer returns a neutral
`value=3` with evidence showing the stub status.

When Phase 2 lands, this scorer will:
1. Run each scenario fixture in `eval/scenarios/*.yaml` against a
   clone of the worktree
2. Score each scenario 1..5 based on whether the agent recovered
   gracefully, escalated, or failed silently
3. Return the mean as the dim value
"""
from __future__ import annotations

from lib.behavior_scorers.types import empty_scorer

# Re-exported under the package's `score(worktree, ctx)` protocol so
# `__init__.SCORER_REGISTRY["D6_robustness"]` resolves without a per-module
# score() wrapper. inspect 2026-08-27 dup-3: collapse D5/D6/D7 empty
# envelopes into one helper so a refactor of DimensionScore fans out
# from a single edit (in `types.empty_scorer`).
score = empty_scorer(
    dim_id="D6_robustness",
    reason="scenario fixtures deferred to Phase 2",
    phase=0,
)
