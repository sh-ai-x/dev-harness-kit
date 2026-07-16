"""cost_gate.py — cost measurement library (read-only display).

Cost is observed and aggregated; it is never used to block tool calls. The
subsystem is a measurement library: pricing, transcript scanning, state
I/O, threshold evaluation, footer parsing, and PR aggregation. There is no
hook driver — the only consumer is the read-only /dev-kit:cost-gate skill
(via tools/cost_gate_status.py).

Independent of the post-hoc dashboard (different pricing table, different
state file, different transcript scanner). Owns its own pricing tiers,
transcript scanner, state I/O, threshold evaluation, footer parsing, and
PR aggregation.

State lives at `<cwd>/.dev-kit/.cost-gate/state.json`. Atomic writes via
lib.atomic.atomic_write_json. Transcript scans use a byte-offset cursor
to dedupe records.

Pricing tiers (USD per 1M tokens):

    Tier    in    out   cw5   cw1h  cr
    opus    5.00  25.00 6.25  10.00 0.50
    sonnet  3.00  15.00 3.75   6.00 0.30
    haiku   1.00   5.00 1.25   2.00 0.10
    minimax 0.30   1.20 0.375  0.375 0.06

Heuristic tool-token estimates (when transcript usage is absent):

    Agent/Task=12000, WebSearch/WebFetch=8000, Read=4000,
    Bash/Grep/Glob=3000, Write/Edit/MultiEdit=2000, other=3000
    Costed as 85% input / 15% output.

Thresholds: session_warn=$5, pr_flag=$20.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Resolve lib/ for atomic.py without coupling to a relative layout.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from atomic import atomic_write_json, now_iso  # noqa: E402


# ---------------------------------------------------------------------------
# Pricing
#
# As of 2026-07-17 the inline PRICING dict has been replaced by a single
# shared loader: ``lib.llm_pricing``. That module reads
# ``docs/llm-info/<provider>.json`` (the SSOT refreshed via
# ``/dev-kit:llm-refresh``) so that ``lib/cost_gate.py`` (this file),
# ``tools/token_efficiency_analyzer.py``, and any future consumer stay in
# sync without re-typing numbers. The inline rows below remain only as a
# fallback for installs where ``docs/llm-info/`` does not yet exist
# (e.g., a partial `--strict` clone). New code MUST go through
# ``lib.llm_pricing`` — never edit these rows.
# ---------------------------------------------------------------------------
from llm_pricing import pricing_for as _loader_pricing_for  # noqa: E402

DEFAULT_PRICING_KEY = "sonnet"

# Loader returns the sonnet fallback row when the model id does not
# resolve. We mirror the lookup path through the merged PRICING dict so
# we can detect the fallback vs an actual match.
import llm_pricing as _llm_pricing  # noqa: E402

_UNKNOWN_MODELS: List[str] = []


def reset_unknown_models() -> None:
    """Clear the per-process list of unknown model ids (test helper)."""
    _UNKNOWN_MODELS.clear()


def _looks_like_known(model_id: str) -> bool:
    """Return True if model_id resolves to a non-fallback PRICING row."""
    if not model_id:
        return False
    pricing = _llm_pricing._pricing_cache()
    mid = model_id.lower()
    if mid in pricing:
        return True
    norm_mid = mid.replace("-", "").replace(".", "").replace("_", "")
    for key in sorted(pricing.keys(), key=len, reverse=True):
        if key and key.replace("-", "").replace(".", "").replace("_", "") in norm_mid:
            return True
    return False


def pricing_for(model_id: str, *, return_unknown: bool = False):
    """Resolve a model id to its pricing row.

    Delegates to ``lib.llm_pricing.pricing_for`` which loads from
    ``docs/llm-info/<provider>.json``. Unknown ids fall back to
    ``sonnet`` pricing and are recorded in the per-process
    ``_UNKNOWN_MODELS`` list (cleared via ``reset_unknown_models`` in
    tests).
    """
    if not model_id:
        if return_unknown:
            return _loader_pricing_for(""), list(_UNKNOWN_MODELS)
        return _loader_pricing_for("")
    if not _looks_like_known(model_id):
        _UNKNOWN_MODELS.append(model_id)
    row = _loader_pricing_for(model_id)
    if return_unknown:
        return row, list(_UNKNOWN_MODELS)
    return row


# ---------------------------------------------------------------------------
# Cost math
# ---------------------------------------------------------------------------

def cost_usd(
    model_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """Compute USD cost for a single assistant usage block."""
    p = pricing_for(model_id)
    # Legacy non-split cache write defaults to 5m bucket (cheap side).
    legacy_write = cache_write_tokens
    cw5 = cache_write_5m_tokens + legacy_write
    cw1 = cache_write_1h_tokens
    return (
        input_tokens      * p["in"]            / 1_000_000
        + output_tokens   * p["out"]           / 1_000_000
        + cw5             * p["cache_write_5m"] / 1_000_000
        + cw1             * p["cache_write_1h"] / 1_000_000
        + cache_read_tokens * p["cache_read"]  / 1_000_000
    )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "session_warn": 5.0,
    "pr_flag": 20.0,
}


def _env_threshold(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def resolve_thresholds(env: Optional[Dict[str, str]] = None) -> Dict[str, float]:
    """Resolve thresholds with env override (DEV_KIT_COST_WARN_USD etc.)."""
    if env is None:
        env = os.environ
    return {
        "session_warn": _env_threshold("DEV_KIT_COST_WARN_USD", DEFAULT_THRESHOLDS["session_warn"]),
        "pr_flag":      _env_threshold("DEV_KIT_PR_COST_FLAG_USD", DEFAULT_THRESHOLDS["pr_flag"]),
    }


# ---------------------------------------------------------------------------
# State schema + I/O
# ---------------------------------------------------------------------------

DEFAULT_TOTAL: Dict[str, Any] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_creation_5m_input_tokens": 0,
    "cache_creation_1h_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "estimated_tokens": 0,
    "cost_usd": 0.0,
}

STATE_SCHEMA_VERSION = 1


def new_session_state(
    *,
    session_id: str,
    cwd: str,
    branch: str,
    repository: str,
    model: str = "",
    transcript_path: str = "",
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    th = thresholds or resolve_thresholds()
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "scope": "session",
        "scope_id": session_id,
        "repository": repository,
        "branch": branch,
        "thresholds_usd": dict(th),
        "totals": dict(DEFAULT_TOTAL),
        "sessions": [{
            "session_id": session_id,
            "model": model,
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "usage": dict(DEFAULT_TOTAL),
            "cost_usd": 0.0,
            "provenance": "actual",
        }],
        "status": "ok",
        "warn_emitted": False,
        "cursor": {
            "transcript_path": transcript_path or "",
            "byte_offset": 0,
            "seen_ids": [],
        },
        "warnings": [],
    }




def load_state(path: Path) -> Optional[Dict[str, Any]]:
    """Load state from disk. Returns None on missing or corrupt JSON."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomic state write (POSIX-atomic via tempfile + os.replace)."""
    atomic_write_json(path, state)


def default_state_path(cwd: str) -> Path:
    """The canonical state path under a given cwd."""
    return Path(cwd) / ".dev-kit" / ".cost-gate" / "state.json"


# ---------------------------------------------------------------------------
# Footer parsing + PR aggregation
# ---------------------------------------------------------------------------

_FOOTER_USD_RE = re.compile(r"^Cost-gate:\s*\$([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)
_FOOTER_SID_RE = re.compile(r"^Cost-gate-Session:\s*(\S+)\s*$", re.MULTILINE)


def parse_footers(commit_bodies: List[str]) -> List[Dict[str, Any]]:
    """Extract (session, cumulative_usd) tuples from commit message bodies.

    Each commit may contain one or both footers. The Cost-gate value is
    interpreted as that session's cumulative cost at commit time. When
    the same session appears in multiple commits, this function keeps
    the maximum cumulative value (since later snapshots supersede earlier
    ones from the same session).

    Returns a list of {session, usd} dicts, one per unique session.
    """
    by_session: Dict[str, float] = {}
    for body in commit_bodies or []:
        if not body:
            continue
        usd_m = _FOOTER_USD_RE.search(body)
        sid_m = _FOOTER_SID_RE.search(body)
        if not usd_m or not sid_m:
            continue
        sid = sid_m.group(1).strip()
        usd = float(usd_m.group(1))
        prev = by_session.get(sid, 0.0)
        if usd > prev:
            by_session[sid] = usd
    return [{"session": s, "usd": u} for s, u in by_session.items()]


def aggregate_pr_sessions(records: List[Dict[str, Any]]) -> float:
    """Sum per-session maxima. Records that already deduped at parse time
    are summed directly; raw records are deduped again defensively."""
    by_session: Dict[str, float] = {}
    for r in records:
        sid = r.get("session", "")
        if not sid:
            continue
        usd = float(r.get("usd", 0.0))
        prev = by_session.get(sid, 0.0)
        if usd > prev:
            by_session[sid] = usd
    return sum(by_session.values())


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def format_text(state: Dict[str, Any], state_path: Path) -> str:
    """Default text rendering of a state."""
    totals = state.get("totals") or {}
    th = state.get("thresholds_usd") or DEFAULT_THRESHOLDS
    sessions = state.get("sessions") or []
    actual_n = sum(1 for s in sessions if s.get("provenance") == "actual")
    est_n = sum(1 for s in sessions if s.get("provenance") == "estimated")
    lines = [
        f"scope: {state.get('scope', '?')}  scope_id: {state.get('scope_id', '?')}",
        f"status: {state.get('status', 'ok')}  cost_usd: ${float(totals.get('cost_usd', 0.0)):.2f}",
        f"sessions: {len(sessions)}  actual={actual_n}  estimated={est_n}",
        f"input={totals.get('input_tokens', 0)}  output={totals.get('output_tokens', 0)}  cache_read={totals.get('cache_read_input_tokens', 0)}",
        f"session_warn: ${th.get('session_warn', 0):.2f}  pr_flag: ${th.get('pr_flag', 0):.2f}",
        f"warnings: {state.get('warnings', [])}",
        f"state_path: {state_path}",
    ]
    return "\n".join(lines)


def format_html(state: Dict[str, Any], state_path: Path) -> str:
    """Self-contained HTML (no JS, no external assets, dark-mode aware)."""
    totals = state.get("totals") or {}
    th = state.get("thresholds_usd") or DEFAULT_THRESHOLDS
    sessions = state.get("sessions") or []
    status = state.get("status", "ok")
    bg = {"ok": "#0a3d0a", "warn": "#7a5a00"}.get(status, "#333")
    fg = "#fff"
    rows = "".join(
        f"<tr><td>{s.get('session_id','')}</td><td>{s.get('model','')}</td>"
        f"<td>${float(s.get('cost_usd', 0)):.2f}</td><td>{s.get('provenance','')}</td></tr>"
        for s in sessions
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>cost-gate — {state.get('scope_id','')}</title>"
        f"<style>body{{font-family:system-ui,sans-serif;max-width:780px;margin:2em auto;"
        f"padding:1em;background:#111;color:#eee;}}table{{border-collapse:collapse;width:100%;}}"
        f"th,td{{border:1px solid #444;padding:6px 10px;text-align:left;}}"
        f".banner{{background:{bg};color:{fg};padding:8px 14px;border-radius:6px;}}</style>"
        f"</head><body><div class='banner'>status: {status} — "
        f"${float(totals.get('cost_usd', 0)):.2f} "
        f"(warn ${th.get('session_warn',0):.2f})</div>"
        f"<h2>Totals</h2><ul>"
        f"<li>input_tokens: {totals.get('input_tokens',0)}</li>"
        f"<li>output_tokens: {totals.get('output_tokens',0)}</li>"
        f"<li>cache_read: {totals.get('cache_read_input_tokens',0)}</li>"
        f"<li>estimated_tokens: {totals.get('estimated_tokens',0)}</li></ul>"
        f"<h2>Sessions</h2><table><tr><th>id</th><th>model</th><th>usd</th>"
        f"<th>provenance</th></tr>{rows}</table>"
        f"<p>state: {state_path}</p></body></html>"
    )


def format_json(state: Dict[str, Any], state_path: Path) -> str:
    """JSON rendering — stable schema for CI consumers."""
    doc = dict(state)
    doc["state_path"] = str(state_path)
    return json.dumps(doc, indent=2, sort_keys=True)


def format_footer(state: Dict[str, Any]) -> str:
    """Two-line git trailer block for inclusion in commit messages."""
    cost = float((state.get("totals") or {}).get("cost_usd", 0.0))
    sid = state.get("scope_id", "")
    return f"Cost-gate: ${cost:.2f}\nCost-gate-Session: {sid}"
