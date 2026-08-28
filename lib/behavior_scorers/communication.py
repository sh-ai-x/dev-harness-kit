"""communication.py — D5 Communication Quality (LLM judge).

Phase 0 placeholder. The full LLM judge wiring lands in Phase 1 (per
proposal §03); for now this scorer returns a neutral `value=3` with
`evidence={"status": "pending", "phase": 0}` so the registry stays
complete and the aggregate behaves deterministically.

When Phase 1 lands, this scorer will:
1. Collect hand-off notes under `.dev-kit/hand-off/*.md`
2. Read the PR description from the latest commit message body
3. Format them as a single judge prompt
4. Call `ctx.llm_judge(prompt, axes=[clarity, completeness, ...])`
5. Return the mean of the 5 axes as the dim value (1..5)

The judge prompt is stored at `eval/prompts/judge-communication.md`
and is intentionally NOT loaded here yet — Phase 1 introduces it.
"""
from __future__ import annotations

from pathlib import Path

from lib.behavior_scorers.types import Context, DimensionScore


def score(worktree: Path, ctx: Context) -> DimensionScore:
    """Return a neutral placeholder until Phase 1 wires the LLM judge.

    Built directly (not via `empty_scorer`) because D5 emits an extra
    `context_no_llm` flag from `ctx.is_deterministic_only()` so CI-gate
    runs can distinguish "judge unavailable" from "judge ran and scored
    3". Once Phase 1 lands the LLM judge, replace this body with the
    real scorer and delete the manual envelope.
    """
    return DimensionScore(
        dim="D5_communication",
        value=3,
        evidence={
            "status": "pending",
            "phase": 0,
            "reason": "LLM judge wiring deferred to Phase 1",
            "context_no_llm": ctx.is_deterministic_only(),
        },
    )
