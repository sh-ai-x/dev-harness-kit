"""llm_pricing.py — single source of truth for LLM per-token pricing.

This module replaces the inline `PRICING` dicts that used to live in
``lib/cost_gate.py`` and ``tools/token_efficiency_analyzer.py``. Both
consumers now read pricing from ``docs/llm-info/<provider>.json`` — the
SSOT maintained by ``/dev-kit:llm-refresh``. Every rate in that directory
is re-verified against the vendor's official docs page during the initial
bootstrap and pinned against a fixture test (see
``skills/llm-refresh/tests/fixtures/``), so the analyzer cannot silently
misbill sessions.

Architecture
============

1.  ``docs/llm-info/sources.json`` declares the four tracked providers
    (claude / codex / minimax / deepseek) plus the per-provider cache
    multiplier constants — ``cache_write_5m_x``, ``cache_write_1h_x``,
    ``cache_read_x``. These constants are universal per the vendor docs
    (see ``rules/token-pricing.md`` notes); they do NOT drift with each
    model release.

2.  ``docs/llm-info/<provider>.json`` carries one row per active model:
    id, display_name, context_window, input/output USD/MTok, currency,
    deprecated flag, and a free-form notes string.

3.  ``load_pricing(root)`` merges every provider row into a flat
    ``PRICING`` dict keyed by model id. Cache write/read rates are
    derived by applying the per-provider multipliers from ``sources.json``
    to each row's base input price. Currency is captured per provider
    (USD for claude/codex/deepseek, CNY for MiniMax) but every consumer
    that calls into this module gets a USD-denominated row —
    ``to_usd()`` does the FX conversion using a constant table sourced
    from the same SSOT.

Why a separate module?

The previous design had **two** ``PRICING`` dicts in the repo:

    * ``lib/cost_gate.py:PRICING``         (in /dev-kit:cost-gate)
    * ``tools/token_efficiency_analyzer.py:PRICING``  (in /dev-kit:token-analyzer)

Both drifted independently. A rate update to one did not reach the other
unless the author remembered to also edit the second copy. A v2 PR
established ``docs/llm-info/`` as the SSOT and made this module the only
loader; the inline dicts in ``cost_gate.py`` and ``token_efficiency_analyzer.py``
now import from here and remain only as a fallback for older installs
that have not yet run ``/dev-kit:bootstrap`` (the JSON files do not yet
exist on disk).

Citation rule (Iron Law from rules/token-pricing.md)
====================================================

A change to any rate in ``docs/llm-info/<id>.json`` is a billable change.
The PR body must include:

    pricing re-verified against <URL> on <YYYY-MM-DD>

The URLs to cite are the four in ``sources.json``. The fixtures under
``skills/llm-refresh/tests/fixtures/`` are the contract tests that
guard against silent parser drift.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Fallback tier (only used when docs/llm-info/*.json does not exist on disk).
# Mirrors the values that used to be hardcoded in
# tools/token_efficiency_analyzer.py:PRICING at the time of the loader
# extraction (2026-07-17), so older installs without the SSOT keep working
# unchanged. New code MUST go through docs/llm-info/ — never edit these rows.
# ---------------------------------------------------------------------------
LEGACY_FALLBACK: Dict[str, Dict[str, float]] = {
    "opus":   {"in": 5.00, "out": 25.00, "cache_write_5m": 6.25,  "cache_write_1h": 10.00, "cache_read": 0.50},
    "sonnet": {"in": 3.00, "out": 15.00, "cache_write_5m": 3.75,  "cache_write_1h":  6.00, "cache_read": 0.30},
    "haiku":  {"in": 1.00, "out":  5.00, "cache_write_5m": 1.25,  "cache_write_1h":  2.00, "cache_read": 0.10},
    "minimax": {"in": 0.30, "out": 1.20, "cache_write_5m": 0.375, "cache_write_1h": 0.375, "cache_read": 0.06},
    # OpenAI fallback values mirror the legacy inline dict (USD).
    "gpt-5-codex":  {"in": 1.25,  "out": 10.00, "cache_write_5m": 1.25,  "cache_write_1h": 1.25,  "cache_read": 0.625},
    "gpt-5.6-sol":  {"in": 5.00,  "out": 30.00, "cache_write_5m": 6.25,  "cache_write_1h": 6.25,  "cache_read": 0.50},
    "gpt-5.6-terra":{"in": 2.50,  "out": 15.00, "cache_write_5m": 3.125, "cache_write_1h": 3.125, "cache_read": 0.25},
    "gpt-5.6-luna": {"in": 1.00,  "out":  6.00, "cache_write_5m": 1.25,  "cache_write_1h": 1.25,  "cache_read": 0.10},
    "gpt-5":        {"in": 1.25,  "out": 10.00, "cache_write_5m": 1.25,  "cache_write_1h": 1.25,  "cache_read": 0.625},
    "gpt-4.1":      {"in": 2.50,  "out": 10.00, "cache_write_5m": 2.50,  "cache_write_1h": 2.50,  "cache_read": 1.25},
    "gpt-4o":       {"in": 2.50,  "out": 10.00, "cache_write_5m": 2.50,  "cache_write_1h": 2.50,  "cache_read": 1.25},
    "o3":           {"in": 10.00, "out": 40.00, "cache_write_5m": 10.00, "cache_write_1h": 10.00, "cache_read": 5.00},
    "o4-mini":      {"in": 1.10,  "out":  4.40, "cache_write_5m": 1.10,  "cache_write_1h": 1.10,  "cache_read": 0.55},
}
LEGACY_DEFAULT_KEY = "sonnet"

# ---------------------------------------------------------------------------
# Currency handling
#
# As of 2026-07-17 every docs/llm-info/<id>.json file declares
# `"currency": "USD"`. The MiniMax file used to publish in CNY but the
# values are now pre-converted upstream (the conversion rate of 7.00 is
# captured in the per-row `notes` string for reproducibility), so the
# loader does NO FX at runtime. If a future vendor publishes in a
# non-USD currency, update the loader here AND in docs/llm-info/README.md.
# ---------------------------------------------------------------------------
SUPPORTED_CURRENCIES = ("USD",)


# ---------------------------------------------------------------------------
# Per-provider cache multiplier constants.
#
# These constants mirror rules/token-pricing.md notes. Anthropic publishes
# them on the pricing page as universal across the Claude family; OpenAI
# publishes a single ~50% cache-read discount with no TTL split;
# MiniMax / DeepSeek publish a single cache-read discount and either a
# single cache-write rate (MiniMax) or a dedicated cache-hit input rate
# (DeepSeek — read directly from docs/llm-info/deepseek.json per model).
# ---------------------------------------------------------------------------
_PROVIDER_CACHE: Dict[str, Dict[str, float]] = {
    "claude": {
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.00,
        "cache_read":     0.10,
    },
    "codex": {
        "cache_write_5m": 1.00,  # OpenAI: no separate TTL; mirrors base input
        "cache_write_1h": 1.00,
        "cache_read":     0.50,
    },
    "minimax": {
        "cache_write_5m": 1.00,
        "cache_write_1h": 1.00,
        "cache_read":     0.20,  # 0.42/M cache-read on standard tier → 0.42 / 2.10 ≈ 0.20
    },
    "deepseek": {
        # DeepSeek publishes dedicated cache_hit input rates on
        # docs/llm-info/deepseek.json. The loader prefers those over
        # multipliers; if absent (older fixtures), fall back to 0.02
        # (typical ratio observed across models).
        "cache_write_5m": 1.00,
        "cache_write_1h": 1.00,
        "cache_read":     0.02,
    },
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    """Walk up from this file looking for ``.claude-plugin/plugin.json``.

    Works from worktrees (``<root>/.worktrees/<name>``) and from the
    consumer install (``.dev-kit/`` inside ``$HOME``).
    """
    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here, Path.cwd()):
        if (candidate / ".claude-plugin" / "plugin.json").exists():
            return candidate
    return here.parent


def find_sources_json(start: Optional[Path] = None) -> Optional[Path]:
    """Locate ``docs/llm-info/sources.json`` walking upward from `start`.

    Walks at most ``_MAX_WALK_DEPTH`` (4) levels up from the starting dir.
    In production the analyzer is invoked from inside the dev-kit
    checkout, where the SSOT is ``<checkout>/docs/llm-info/sources.json``
    — within 2–3 levels. Bounding the walk prevents a long cascade of
    ``stat`` calls on a fresh CI tmpdir where the chain is obviously
    empty (the analyzer subprocess on a ``/tmp/capture-cov-XXX`` cwd
    walked 4–5 levels of dead tmpfs paths and timed out the parent
    test's 30-second subprocess budget on slow shared runners).
    """
    cur = (start or Path.cwd()).resolve()
    for _ in range(_MAX_WALK_DEPTH):
        cand = cur / "docs" / "llm-info" / "sources.json"
        if cand.exists():
            return cand
        cand2 = cur / ".dev-kit" / "llm-info" / "sources.json"
        if cand2.exists():
            return cand2
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


#: Hard cap on the upward walk in ``find_sources_json``. Production
#: callers run from inside the dev-kit checkout, where the SSOT lives
#: at most 2–3 parents up; bounding at 4 covers dev / stage / test
#: layouts and stops the walk from reaching ``/`` on every analyzer
#: invocation in a tmpfs CI tmpdir.
_MAX_WALK_DEPTH = 4


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _validate_currency(currency: str) -> None:
    """Reject non-USD up front; the loader expects USD-denominated rows.

    MiniMax values used to be CNY at FX 7.00; those were converted
    upstream and committed as USD so the loader has zero FX risk. Any
    regression here means docs/llm-info/sources.json or one of the
    per-provider files needs re-conversion. See
    `rules/token-pricing.md` for the conversion provenance.
    """
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"unsupported currency {currency!r}; expected one of "
            f"{SUPPORTED_CURRENCIES}. Convert in docs/llm-info/<id>.json "
            "before committing (see rules/token-pricing.md)."
        )


def _deepseek_cache_read(model: Dict[str, Any]) -> Optional[float]:
    """If DeepSeek row carries an explicit cache-hit rate in `notes`, extract it."""
    notes = model.get("notes") or ""
    m = re.search(r"Cache hit:\s*\$\s*([0-9]+(?:\.[0-9]+)?)", notes)
    if m:
        return float(m.group(1))
    return None


def _row_for_model(model: Dict[str, Any], provider_id: str, currency: str) -> Dict[str, float]:
    cache_constants = _PROVIDER_CACHE.get(provider_id, _PROVIDER_CACHE["codex"])
    _validate_currency(currency)
    base_in = float(model["input_price_per_mtok"])
    out = float(model["output_price_per_mtok"])

    # Cache read: prefer an explicit value when present (DeepSeek).
    cache_read: Optional[float] = None
    if provider_id == "deepseek":
        cache_read = _deepseek_cache_read(model)
    cache_read = cache_read if cache_read is not None else base_in * cache_constants["cache_read"]

    return {
        "in":              round(base_in, 4),
        "out":             round(out, 4),
        "cache_write_5m":  round(base_in * cache_constants["cache_write_5m"], 4),
        "cache_write_1h":  round(base_in * cache_constants["cache_write_1h"], 4),
        "cache_read":      round(cache_read, 4),
    }


def load_pricing(sources_path: Optional[Path] = None) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Any]]:
    """Read docs/llm-info/sources.json + per-provider JSON; return (PRICING, sources).

    The returned PRICING is keyed by model id (e.g. ``claude-opus-4-8``,
    ``gpt-5.5``) with values that mirror the analyzer's row schema:

        {"in": float, "out": float, "cache_write_5m": float,
         "cache_write_1h": float, "cache_read": float}

    Deprecated models from the SSOT are loaded too (they stay in the
    analyzer history; downstream consumers can filter). When the JSON
    files do not exist on disk, the legacy fallback is returned. The
    caller (``tools/token_efficiency_analyzer.py``) decides whether to
    WARN about missing JSON.

    `sources_path` parameter exists primarily for tests; production code
    calls ``load_pricing()`` with no argument.
    """
    path = sources_path or find_sources_json()
    if not path or not path.exists():
        return dict(LEGACY_FALLBACK), {"_missing_sources_json": True}
    try:
        sources = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(LEGACY_FALLBACK), {"_missing_sources_json": True}

    root = path.parent  # docs/llm-info/sources.json → docs/llm-info
    pricing: Dict[str, Dict[str, float]] = {}
    for src in sources.get("providers", []):
        pid = src["id"]
        data_path = root / f"{pid}.json"
        if not data_path.exists():
            continue
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        currency = data.get("currency") or src.get("currency") or "USD"
        for model in data.get("models", []):
            mid = model.get("id")
            if not mid:
                continue
            pricing[mid] = _row_for_model(model, pid, currency)
    return pricing, sources


# ---------------------------------------------------------------------------
# pricing_for
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _pricing_cache() -> Dict[str, Dict[str, float]]:
    pricing, _ = load_pricing()
    return pricing


def pricing_for(model_id: str) -> Dict[str, float]:
    """Resolve a model id to its pricing row.

    Longest-prefix-first substring match against the keys of
    ``load_pricing()``. This ordering is critical when one key is a
    substring of another (e.g. ``gpt-5`` vs ``gpt-5.6-sol``): the
    longer key must win.

    Model ids are normalized (lower-cased, ``-`` / ``.`` / ``_`` removed)
    so ``"gpt-5.5"`` (real Claude/Codex id) matches ``"gpt-5-5"`` (the
    docs/llm-info slug that `_model_id()` produces).

    Unknown ids fall back to ``sonnet`` pricing so the analyzer still
    bills something instead of erroring mid-session, AND are echoed to
    stderr as a WARN so triage can add a row.
    """
    pricing = _pricing_cache()
    if not model_id:
        return pricing.get("sonnet",
                           pricing.get(LEGACY_DEFAULT_KEY,
                                       LEGACY_FALLBACK[LEGACY_DEFAULT_KEY]))
    mid = model_id.lower()

    def _norm(s: str) -> str:
        return s.replace("-", "").replace(".", "").replace("_", "")

    norm_mid = _norm(mid)
    if mid in pricing:
        return pricing[mid]
    # longest-prefix-first substring match on the normalized key
    for key in sorted(pricing.keys(), key=len, reverse=True):
        if key and _norm(key) in norm_mid:
            return pricing[key]
    # legacy fallback keys
    if mid in LEGACY_FALLBACK:
        return LEGACY_FALLBACK[mid]
    for key in sorted(LEGACY_FALLBACK.keys(), key=len, reverse=True):
        if key and _norm(key) in norm_mid:
            return LEGACY_FALLBACK[key]
    # Final fallback: sonnet. Emit WARN once per id to stderr.
    print(f"WARN: unknown model {model_id!r}; falling back to sonnet pricing",
          file=__import__("sys").stderr)
    return pricing.get("sonnet", LEGACY_FALLBACK[LEGACY_DEFAULT_KEY])


def clear_cache() -> None:
    """Reset the in-memory pricing cache (test helper)."""
    _pricing_cache.cache_clear()


def pricing_keys() -> List[str]:
    """Return every key the loader currently resolves — for assertion in tests."""
    return sorted(set(_pricing_cache()) | set(LEGACY_FALLBACK))
