#!/usr/bin/env python3
"""llm_judge.py — LLM-as-judge for eval (agent-behavior).

Provider-agnostic via Anthropic-compatible API (default MiniMax).

Two axis families:
- JUDGE_AXES: legacy 4-axis asset-freshness rubric (semantic_drift /
  completeness / correctness / consistency). Kept for backward-compat
  with tests/test_llm_judge.py and any external callers.
- DIM_AXES: per-dim agent-behavior rubric axes used by the new
  /dev-kit:eval (review / security / plan). Each tuple is the axis
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
}

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
    """
    target_axes = tuple(axes) if axes is not None else JUDGE_AXES
    target_set = set(target_axes)
    try:
        data = json.loads(raw)
        return {ax: float(data[ax]) for ax in target_axes if ax in data}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    m = re.search(
        r"\{[^{}]*?" + _AXIS_TOKEN_RE + r"[^{}]*?\}",
        raw, re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(0))
            return {ax: float(data[ax]) for ax in target_axes if ax in data}
        except Exception:
            pass
    return {}


def call_judge(
    *,
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    base_url: str = "https://api.minimax.io/anthropic",
    max_tokens: int = 512,
    timeout: int = 30,
) -> Dict:
    """Call LLM judge and return {scores, tokens_in, tokens_out, raw}.

    provider is informational only — both 'minimax' and 'anthropic' use
    the Anthropic-compatible POST /v1/messages endpoint.
    """
    if not api_key:
        raise ValueError(f"api_key missing for provider={provider}")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "You are a code review judge. Respond ONLY with a JSON object "
            "containing 4 axis scores (semantic_drift, completeness, "
            "correctness, consistency), each 0-10. No other text."
        ),
    }
    return _call_anthropic_compatible(
        payload, api_key, f"{base_url.rstrip('/')}/v1/messages", timeout
    )


def _call_anthropic_compatible(
    payload: Dict,
    api_key: str,
    url: str,
    timeout: int,
) -> Dict:
    """POST to /v1/messages via _http_post (test seam)."""
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
        "scores": parse_scores_json(raw_text),
        "tokens_in": usage.get("input_tokens", 0),
        "tokens_out": usage.get("output_tokens", 0),
        "raw": raw_text,
    }


def _http_post(*, url: str, payload: Dict, api_key: str, timeout: int) -> Dict:
    """Real HTTP POST. Wraps urllib.request.urlopen for test seam."""
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
    """Mean of 4 axes (0-10)."""
    return round(sum(axes.values()) / max(1, len(axes)), 2)


def verdict_from_score(score: float) -> str:
    """OK / DRIFT_WARNING / ROT verdict."""
    if score >= 8.0:
        return "OK"
    if score >= 5.0:
        return "DRIFT_WARNING"
    return "ROT"


if __name__ == "__main__":
    import sys
    project_root = Path(os.environ.get("PROJECT_ROOT", "."))
    cfg = load_config(project_root)
    print(f"config: {cfg}")
