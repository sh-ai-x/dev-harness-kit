#!/usr/bin/env python3
"""llm_judge.py — LLM-as-judge for evaluation (agent-behavior).

Provider-agnostic via Anthropic-compatible API (default MiniMax).

Two axis families:
- JUDGE_AXES: legacy 4-axis asset-freshness rubric (semantic_drift /
  completeness / correctness / consistency). Kept for backward-compat
  with tests/test_llm_judge.py and any external callers.
- DIM_AXES: per-dim agent-behavior rubric axes used by the new
  /dev-kit:evaluate (review / security / plan). Each tuple is the axis
  set a per-dim judge prompt is expected to return.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

JUDGE_AXES: Tuple[str, ...] = (
    "semantic_drift", "completeness", "correctness", "consistency",
)

# Per-dim axes for the new agent-behavior eval. The judge's prompt for
# each dim MUST return a JSON object whose keys are exactly this tuple.
DIM_AXES: Dict[str, Tuple[str, ...]] = {
    "review": (
        "verdict_consistency",
        "severity_calibration",
        "precision",
        "recall",
        "code_sanity_score",
    ),
    "security": (
        "owasp_classification_accuracy",
        "severity_accuracy",
        "precision",
    ),
    "plan": (
        "spec_clarity",
        "step_atomicity",
        "ac_executability",
        "dependency_ordering",
    ),
    # Phase 3 (issue #363, merged via PR #445): harness + os axes for
    # /dev-kit:evaluate --harness-quality and --os-quality.
    "harness": (
        "determinism",
        "isolation",
        "observability",
        "testability",
        "rollback_safety",
    ),
    "os": (
        "permission_separation",
        "cost_visibility",
        "rollback_capability",
        "escalation_path",
        "audit_trail",
    ),
    # Phase 4 (issue #371): plan_value 0-5 axis scores. The judge prompt
    # (eval/prompts/judge-plan-value.md) returns six scores; the
    # 4-way verdict (proceed/revise/hold/kill) is computed by
    # lib/valuation_engine.py:decide() from these six, so the verdict
    # is NOT an axis here.
    "plan_value": (
        "problem_fit",
        "roi_estimate",
        "existing_solution_edge",
        "team_capability",
        "risk_vs_reward",
        "measurability",
    ),
    # Phase 5 (issue #378, merged via PR #443): research_source +
    # research_claim axes for /dev-kit:research.
    "research_source": (
        "authority_score",
        "recency_score",
        "primary_vs_secondary",
        "url_validity",
        "citation_completeness",
    ),
    "research_claim": (
        "citation_required",
        "n_source_agreement",
        "primary_source_present",
        "timestamp_present",
        "rubric_match",
    ),
    # Phase 6 (issue #383): per-field clarity for the 5-field interview
    # safety contract. Each axis is the model's 0-10 clarity score for
    # one of the 5 mandated interview fields.
    "interview_ambiguity": (
        "goal_clarity",
        "constraints_clarity",
        "success_criteria_clarity",
        "anti_goals_clarity",
        "acceptance_rubric_clarity",
    ),
    # Phase 7 (this PR): push_intent dim for the pre-push LLM judge
    # (lib/push_intent_judge.py). Only the four value/meaning axes
    # (VM-1..4 from judge-code-sanity.md) — clean-code and
    # over-engineering belong to the dedicated CI maintenance gate.
    "push_intent": (
        "intent_clarity",
        "scope_discipline",
        "change_necessity",
        "value_alignment",
    ),
    # Phase 7 (this PR): maintenance dim for the CI maintenance gate
    # (.github/workflows/maintenance.yml). Composite code-sanity score
    # + a docs-coverage score that mirrors the docs-updated sub-gate
    # + a scope-discipline score mirroring VM-3.
    "maintenance": (
        "code_sanity_score",
        "docs_coverage_score",
        "scope_discipline_score",
    ),
}

# Per-dim score range. Most dims are 0-10 (higher = better, with the
# polarity override for lower_is_better dims handled in
# ``AXIS_POLARITY``). The plan_value dim is 0-5 because the valuation
# engine's SCORE_MIN/SCORE_MAX are pinned to that range; a judge
# emitting 6-10 for plan_value would crash the engine on validate().
# Default: 0-10 for unknown dims.
DIM_SCORE_RANGE: Dict[str, Tuple[float, float]] = {
    "plan_value": (0.0, 5.0),
}


def score_range_for_dim(dim: str) -> Tuple[float, float]:
    return DIM_SCORE_RANGE.get(dim, (0.0, 10.0))


# Regex fragment used by parse_scores_json to recognize axis tokens. Built
# lazily from the union of JUDGE_AXES + DIM_AXES so any new dim auto-extends.
_AXIS_TOKEN_RE = r"(?:" + "|".join(
    sorted({ax for axes in (JUDGE_AXES, *DIM_AXES.values()) for ax in axes})
) + r")"


def _env_get(env: Dict[str, str], key: str, default: str) -> str:
    """env.get(key) with empty-string fallback to default."""
    v = env.get(key, "")
    return v if v else default


def load_config(project_root: Path) -> Dict[str, str]:
    """Load judge config from env (or .env parser). Empty values fall back to defaults."""
    env = dict(os.environ)
    env_path = project_root / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cur = env.get(k.strip())
                # .env only populates if not already in os.environ
                if not cur:
                    env[k.strip()] = v.strip().strip('"').strip("'")

    provider = _env_get(env, "JUDGE_PROVIDER", "minimax")
    api_key_var = "MINIMAX_API_KEY" if provider == "minimax" else "ANTHROPIC_API_KEY"
    return {
        "provider": provider,
        "model": _env_get(env, "JUDGE_MODEL", "MiniMax-M3[1m]"),
        "api_key": _env_get(env, api_key_var, ""),
        "base_url": _env_get(env, "JUDGE_BASE_URL", "https://api.minimax.io/anthropic"),
    }


def format_prompt(project_root: Path, template_name: str, substitutions: Dict[str, str]) -> str:
    """Load prompt template from eval/prompts/<template_name>.md and substitute ${KEY}."""
    tpl_path = project_root / "eval" / "prompts" / template_name
    if tpl_path.suffix != ".md":
        tpl_path = tpl_path.with_suffix(".md")
    if not tpl_path.exists():
        return ""
    text = tpl_path.read_text(encoding="utf-8")
    for k, v in substitutions.items():
        text = text.replace("${" + k + "}", str(v))
    return text


def parse_scores_json(raw: str, axes: Optional[Iterable[str]] = None) -> Dict[str, float]:
    """Parse axis scores from LLM response. `axes` defaults to JUDGE_AXES.

    Tries a strict JSON parse first; on failure falls back to regex
    extraction of the first `{...}` block that mentions any axis token.
    Returns {} if both fail.

    Per the Phase 5 (issue #443) security review (M4): scores are
    validated for finiteness and 0-10 range. A non-finite or out-of-range
    score (NaN, Infinity, negative, >10) is dropped from the result so
    downstream aggregates do not produce invalid verdicts.
    """
    target_axes = tuple(axes) if axes is not None else JUDGE_AXES

    def _extract(d: dict) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for ax in target_axes:
            if ax not in d:
                continue
            try:
                v = float(d[ax])
            except (TypeError, ValueError):
                continue
            import math
            if not math.isfinite(v) or v < 0.0 or v > 10.0:
                continue
            out[ax] = v
        return out

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return _extract(data)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(
        r"\{[^{}]*?" + _AXIS_TOKEN_RE + r"[^{}]*?\}",
        raw, re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return _extract(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def call_judge(
    *,
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    axes: Tuple[str, ...] = JUDGE_AXES,
    dim: Optional[str] = None,
    base_url: str = "https://api.minimax.io/anthropic",
    max_tokens: int = 512,
    timeout: int = 30,
) -> Dict:
    """Call LLM judge and return {scores, tokens_in, tokens_out, raw}.

    `axes` lists the per-dim axis names the caller expects back. The system
    prompt is built from `axes` so it matches the per-dim user prompt the
    eval_runner constructs (verdict_consistency / severity_calibration /
    precision / recall / code_sanity_score for `review`; etc.). Without
    this alignment the judge receives contradictory axis lists between
    system and user and returns unreliable / axis-zero scores.

    `dim` is the parent dim name (e.g. "review", "plan_value"). When
    provided, the system prompt states the per-dim score range from
    ``DIM_SCORE_RANGE`` (e.g. "each 0-5" for plan_value). Defaults to
    0-10 when the dim is unknown.

    provider is informational only — both 'minimax' and 'anthropic' use
    the Anthropic-compatible POST /v1/messages endpoint.
    """
    if not api_key:
        raise ValueError(f"api_key missing for provider={provider}")
    axes_csv = ", ".join(axes)
    score_min, score_max = score_range_for_dim(dim) if dim else (0.0, 10.0)
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            f"You are a code review judge. Respond ONLY with a JSON object "
            f"containing these {len(axes)} axis scores ({axes_csv}), "
            f"each {score_min:g}-{score_max:g}. No other text."
        ),
    }
    return _call_anthropic_compatible(
        payload,
        api_key,
        f"{base_url.rstrip('/')}/v1/messages",
        timeout,
        axes=axes,
    )


def _call_anthropic_compatible(
    payload: Dict,
    api_key: str,
    url: str,
    timeout: int,
    axes: Optional[Iterable[str]] = None,
) -> Dict:
    """POST to /v1/messages via _http_post (test seam).

    `axes` is forwarded to parse_scores_json so the parser only matches
    the per-dim axis set the caller (eval_runner / call_judge) requested.
    Without this, parse_scores_json falls back to the legacy JUDGE_AXES
    tuple and returns {} for every per-dim response — silently dropping
    all 12 agent-behavior eval cases to score=0 / verdict=ROT.
    """
    response = _http_post(
        url=url,
        payload=payload,
        api_key=api_key,
        timeout=timeout,
    )
    content = response.get("content", [])
    raw_text = " ".join(
        part.get("text", "") for part in content if part.get("type") == "text"
    )
    usage = response.get("usage", {})
    return {
        "scores": parse_scores_json(raw_text, axes=axes),
        "tokens_in": usage.get("input_tokens", 0),
        "tokens_out": usage.get("output_tokens", 0),
        "raw": raw_text,
    }


def _http_post(*, url: str, payload: Dict, api_key: str, timeout: int) -> Dict:
    """Real HTTP POST. Wraps urllib.request.urlopen."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        raise RuntimeError(f"judge API call failed: {e}") from e


def score_aggregate(axes: Dict[str, float]) -> float:
    return round(sum(axes.values()) / max(1, len(axes)), 2)


# Per-dim polarity. Most dims are "higher_is_better" (a high score means
# the agent did well on that axis). `interview_ambiguity` is the
# exception: the judge prompt defines 0 = clear and 10 = ambiguous
# (per the local semantic in lib/interview_engine.py where
# MISSING_FIELD_SCORE=10 and CLEAR_FIELD_SCORE=2), so the aggregate
# verdict path must invert before applying verdict_from_score.
AXIS_POLARITY: Dict[str, str] = {
    "interview_ambiguity": "lower_is_better",
}


def verdict_from_score(score: float) -> str:
    if score >= 8.0:
        return "OK"
    if score >= 5.0:
        return "DRIFT_WARNING"
    return "ROT"


def normalize_for_verdict(dim: str, score: float) -> float:
    """Invert the score for lower_is_better dims so verdict_from_score
    can be applied uniformly (higher = better).
    """
    if AXIS_POLARITY.get(dim) == "lower_is_better":
        return 10.0 - score
    return score


if __name__ == "__main__":
    project_root = Path(os.environ.get("PROJECT_ROOT", "."))
    cfg = load_config(project_root)
    print(f"config: {cfg}")
