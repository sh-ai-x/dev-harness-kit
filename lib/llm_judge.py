#!/usr/bin/env python3
"""llm_judge.py — LLM-as-judge for asset freshness (MUST-32~34).

Provider-agnostic via Anthropic-compatible API (default MiniMax).
4 axes: semantic_drift / completeness / correctness / consistency.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

JUDGE_AXES = ("semantic_drift", "completeness", "correctness", "consistency")


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
    api_key_var = "MINMAX_API_KEY" if provider == "minimax" else "ANTHROPIC_API_KEY"
    return {
        "provider": provider,
        "model": _env_get(env, "JUDGE_MODEL", "MiniMax-M3[1m]"),
        "api_key": _env_get(env, api_key_var, ""),
        "base_url": _env_get(env, "JUDGE_BASE_URL", "https://api.minimax.io/anthropic"),
        "minimax_api_key": env.get("MINMAX_API_KEY", ""),
        "anthropic_api_key": env.get("ANTHROPIC_API_KEY", ""),
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


def parse_scores_json(raw: str) -> Dict[str, float]:
    """Parse 4-axis scores from LLM response (extract JSON even if wrapped)."""
    try:
        data = json.loads(raw)
        return {ax: float(data[ax]) for ax in JUDGE_AXES if ax in data}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    m = re.search(
        r"\{[^{}]*?(?:semantic_drift|completeness|correctness|consistency)[^{}]*?\}",
        raw, re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(0))
            return {ax: float(data[ax]) for ax in JUDGE_AXES if ax in data}
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
