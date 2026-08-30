"""view_model — extracted from tools/token_efficiency_analyzer.py per PR-D.

Owns: filter_sessions + build_view_model + their internal helpers.
Uses lazy lookup for parent-module symbols (cost_usd, grade_for, etc.)
to avoid the circular import: parent module would otherwise need to
import this child eagerly to call build_view_model internally.
"""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean


# Lazy lookup helpers — defer the parent import until each is called.
# This breaks the would-be cycle (parent uses these symbols in its own
# function bodies, so it can't import us at top level).
def _cost_usd(*a, **kw):
    from token_efficiency_analyzer import cost_usd as _f
    return _f(*a, **kw)


def _session_cost(*a, **kw):
    from token_efficiency_analyzer import session_cost as _f
    return _f(*a, **kw)


def _grade_for(*a, **kw):
    from token_efficiency_analyzer import grade_for as _f
    return _f(*a, **kw)


def _score_cache_utilization(*a, **kw):
    from token_efficiency_analyzer import score_cache_utilization as _f
    return _f(*a, **kw)


def _aggregate_worktree_rows(*a, **kw):
    from token_efficiency_analyzer import _aggregate_worktree_rows as _f
    return _f(*a, **kw)


def _stale_worktree_states():
    from token_efficiency_analyzer import STALE_WORKTREE_STATES
    return STALE_WORKTREE_STATES


def _pricing_for(model_id, **kw):
    from token_efficiency_analyzer import pricing_for as _f
    return _f(model_id, **kw)


# Module-level constant lookups (these are constants, not functions, so
# we look them up once and cache — but defer to call time to break the
# import cycle).
_DEFAULT_DUP_READ_TOKENS_CACHE = None
def _DEFAULT_DUP_READ_TOKENS():
    global _DEFAULT_DUP_READ_TOKENS_CACHE
    if _DEFAULT_DUP_READ_TOKENS_CACHE is None:
        from token_efficiency_analyzer import DEFAULT_DUP_READ_TOKENS
        _DEFAULT_DUP_READ_TOKENS_CACHE = DEFAULT_DUP_READ_TOKENS
    return _DEFAULT_DUP_READ_TOKENS_CACHE

# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_sessions(sessions: list[dict], repo: str, days: int,
                    branch: str = "", worktree: str = "") -> list[dict]:
    """Keep sessions whose derived repo matches ``repo`` AND branch matches
    ``branch`` (case-insensitive substring) AND worktree matches ``worktree``
    (case-insensitive substring) AND last_ts within ``days``.

    Empty ``repo``, ``branch``, or ``worktree`` disables that filter.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    repo = repo or ""
    branch_lc = branch.lower() if branch else ""
    worktree_lc = worktree.lower() if worktree else ""
    out: list[dict] = []
    for s in sessions:
        if repo and repo not in (s["repo"] or ""):
            continue
        if branch_lc and branch_lc not in (s.get("branch") or "").lower():
            continue
        if worktree_lc and worktree_lc not in (s.get("worktree") or "").lower():
            continue
        last = s["last_ts"]
        if last is None:
            continue
        if last < cutoff:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# HTML rendering (single self-contained file, embedded CSS, no JS, no deps)
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #0e1117;
  --panel: #161b22;
  --panel-2: #1c232c;
  --text: #e6edf3;
  --muted: #8b95a7;
  --accent: #58a6ff;
  --good: #3fb950;
  --warn: #d29922;
  --bad: #f85149;
  --border: #30363d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 24px; }
h1 { margin: 0 0 8px 0; font-size: 28px; letter-spacing: -0.02em; }
.subtitle { color: var(--muted); margin-bottom: 28px; }
.grid { display: grid; gap: 16px; }
.cols-4 { grid-template-columns: repeat(4, 1fr); }
.cols-5 { grid-template-columns: repeat(5, 1fr); }
.cols-2 { grid-template-columns: 1fr 1fr; }
@media (max-width: 1100px) {
  .cols-5 { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 900px) {
  .cols-4 { grid-template-columns: repeat(2, 1fr); }
  .cols-5 { grid-template-columns: repeat(2, 1fr); }
  .cols-2 { grid-template-columns: 1fr; }
}
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
}
.metric .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
.metric .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
.metric .delta { font-size: 12px; color: var(--muted); margin-top: 2px; }
.section-title { font-size: 16px; font-weight: 600; margin: 28px 0 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
tr:last-child td { border-bottom: none; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 500; margin-right: 4px; }
.pill-good { background: rgba(63,185,80,0.15); color: var(--good); }
.pill-warn { background: rgba(210,153,34,0.15); color: var(--warn); }
.pill-bad  { background: rgba(248,81,73,0.15); color: var(--bad); }
.bar { height: 8px; background: var(--panel-2); border-radius: 4px; overflow: hidden; }
.bar > span { display: block; height: 100%; background: var(--accent); }
.warning {
  border-left: 3px solid var(--bad);
  background: rgba(248,81,73,0.06);
  padding: 14px 16px;
  border-radius: 6px;
  margin: 10px 0;
  font-size: 13px;
  white-space: pre-wrap;
}
.warning.warn { border-left-color: var(--warn); background: rgba(210,153,34,0.06); }
.savings {
  background: linear-gradient(135deg, rgba(63,185,80,0.10), rgba(88,166,255,0.10));
  border: 1px solid rgba(63,185,80,0.35);
  border-radius: 10px;
  padding: 22px 26px;
  margin: 18px 0 6px;
}
.savings .big { font-size: 32px; font-weight: 700; color: var(--good); }
.muted { color: var(--muted); }
.footer { color: var(--muted); font-size: 12px; margin-top: 36px; text-align: center; }
code { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }
.grade {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
  margin-left: 6px;
  vertical-align: 1px;
}
.grade-A { background: rgba(63,185,80,0.18); color: var(--good); }
.grade-B { background: rgba(88,166,255,0.18); color: var(--accent); }
.grade-C { background: rgba(210,153,34,0.18); color: var(--warn); }
.grade-D { background: rgba(248,81,73,0.12); color: var(--warn); }
.grade-F { background: rgba(248,81,73,0.22); color: var(--bad); }
.cost-gate {
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 18px;
  font-size: 13px;
  border: 1px solid var(--border);
}
.cost-gate.ok   { background: rgba(63,185,80,0.06); border-color: rgba(63,185,80,0.35); }
.cost-gate.warn { background: rgba(210,153,34,0.08); border-color: rgba(210,153,34,0.45); }
.cost-gate.bad  { background: rgba(248,81,73,0.08); border-color: rgba(248,81,73,0.55); }
.cost-gate .label { font-weight: 600; margin-right: 8px; }
.cost-gate ul { margin: 8px 0 0 18px; padding: 0; }
.cost-gate li { color: var(--muted); }
.ttl-mix { display: grid; grid-template-columns: 140px 1fr 80px; gap: 6px 12px; align-items: center; font-size: 12px; }
.ttl-mix .ttl-name { color: var(--muted); }
.ttl-mix .ttl-pct { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.roi { padding: 0; margin: 0; list-style: none; }
.roi li { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; cursor: help; }
.roi li:last-child { border-bottom: none; }
.roi .rank { color: var(--muted); min-width: 22px; font-variant-numeric: tabular-nums; }
.roi .save { color: var(--good); font-weight: 600; min-width: 80px; font-variant-numeric: tabular-nums; }
.roi .code { min-width: 150px; }
.roi .sid { color: var(--muted); font-family: ui-monospace, monospace; min-width: 66px; }
.roi .evidence { flex: 1; color: var(--text); }
.optimize { padding: 0; margin: 0; list-style: none; }
.optimize li { padding: 6px 0; font-size: 13px; line-height: 1.55; }
.optimize li.do::before { content: "✓ "; color: var(--good); font-weight: 700; }
.optimize li.dont::before { content: "✗ "; color: var(--bad); font-weight: 700; }
.optimize li.muted-item { color: var(--muted); }
.ttl-caveat { font-size: 11px; color: var(--muted); margin-top: 10px; line-height: 1.5; }
.bar-legend { display: flex; gap: 12px; margin-top: 8px; font-size: 11px; color: var(--muted); }
.bar-legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
.bar-legend .legend-read::before { background: var(--accent); }
.bar-legend .legend-5m::before { background: var(--good); }
.bar-legend .legend-1h::before { background: var(--warn); }
.bar-legend .legend-miss::before { background: var(--bad); }
.bar.read > span { background: var(--accent); }
.bar.write5m > span { background: var(--good); }
.bar.write1h > span { background: var(--warn); }
.bar.miss > span { background: var(--bad); }
"""

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Token Efficiency Dashboard — {repo}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
  <h1>Token Efficiency Dashboard</h1>
  <div class="subtitle">{subtitle}</div>

  {cost_gate_banner}

  <div class="section-title">Overview</div>
  <div class="grid cols-5">
    <div class="panel metric"><div class="label">Active Sessions</div><div class="value">{active_count}</div><div class="delta">{inactive_count} inactive · {repos_named} distinct repo labels</div></div>
    <div class="panel metric"><div class="label">Total Cost</div><div class="value">${total_cost:.2f}</div><div class="delta">{total_tokens:,} tokens processed</div></div>
    <div class="panel metric"><div class="label">Avg Score</div><div class="value">{avg_score:.1f}<span class="muted" style="font-size:14px">/100</span><span class="grade grade-{avg_grade}">{avg_grade}</span></div><div class="delta">cache {avg_cache:.0f} · density {avg_density:.0f} · redundancy {avg_redundancy:.0f} · economy {avg_economy:.0f}</div></div>
    <div class="panel metric"><div class="label">Cache Hit Ratio</div><div class="value">{avg_cache_hit:.0%}</div><div class="delta">cache_read / total_input (token-weighted) · unweighted {avg_cache_hit_unweighted:.0%}</div></div>
    <div class="panel metric"><div class="label">Stale Cost</div><div class="value">${stale_cost:.2f}</div><div class="delta">{stale_pct:.0%} of total · merged-or-gone worktrees</div></div>
  </div>

  <div class="section-title">Cost &amp; Token Distribution</div>
  <div class="grid cols-2">
    <div class="panel">
      <div style="font-weight:600;margin-bottom:10px">Cost by Repository <span class="muted" style="font-weight:400;font-size:11px">(all repos in window)</span></div>
      <table>
        <thead><tr><th>Repo</th><th style="text-align:right">Sessions</th><th style="text-align:right">Cost</th><th style="width:30%">Share</th></tr></thead>
        <tbody>{repo_rows}</tbody>
      </table>
    </div>
    <div class="panel">
      <div style="font-weight:600;margin-bottom:10px">Cost by Tool</div>
      <table>
        <thead><tr><th>Tool</th><th style="text-align:right">Calls</th><th style="text-align:right">Est. Cost</th><th style="width:30%">Share</th></tr></thead>
        <tbody>{tool_rows}</tbody>
      </table>
      {read_warning_html}
    </div>
  </div>

  <div class="section-title">Cost by Branch <span class="muted" style="font-weight:400;font-size:11px">(all branches in window, derived from gitBranch wire field)</span></div>
  <div class="panel">
    <table>
      <thead><tr><th>Branch</th><th style="text-align:right">Sessions</th><th style="text-align:right">Cost</th><th style="width:40%">Share</th></tr></thead>
      <tbody>{branch_rows}</tbody>
    </table>
  </div>

  <div class="section-title">Cost by Worktree <span class="muted" style="font-weight:400;font-size:11px">(all worktrees in window, derived from cwd path; ``(main)`` = main checkout; State = live / fresh / merged / gone)</span></div>
  <div class="panel">
    <table>
      <thead><tr><th>Worktree</th><th style="text-align:right">Sessions</th><th style="text-align:right">Cost</th><th style="width:36%">Share</th><th>State</th></tr></thead>
      <tbody>{worktree_rows}</tbody>
    </table>
  </div>

  <div class="section-title">Cost by Model &amp; Cache TTL Mix</div>
  <div class="grid cols-2">
    <div class="panel">
      <div style="font-weight:600;margin-bottom:10px">Cost by Model</div>
      <table>
        <thead><tr><th>Model</th><th style="text-align:right">Sessions</th><th style="text-align:right">Tokens</th><th style="text-align:right">Cost</th><th style="width:30%">Share</th></tr></thead>
        <tbody>{model_rows}</tbody>
      </table>
      {unknown_model_html}
    </div>
    <div class="panel">
      <div style="font-weight:600;margin-bottom:10px">Cache TTL Mix</div>
      <div class="ttl-mix">
        <div class="ttl-name">cache_read</div><div class="bar read"><span style="width:{ttl_read_pct:.1f}%"></span></div><div class="ttl-pct">{ttl_read_tokens:,}</div>
        {ttl_middle_html}
        <div class="ttl-name">pure miss</div><div class="bar miss"><span style="width:{ttl_miss_pct:.1f}%"></span></div><div class="ttl-pct">{ttl_miss_tokens:,}</div>
      </div>
      <div class="bar-legend">
        <span class="legend-read">cache_read</span>
        <span class="legend-5m">write 5m</span>
        <span class="legend-1h">write 1h</span>
        <span class="legend-miss">miss</span>
      </div>
      <div class="ttl-caveat">{ttl_caveat}</div>
    </div>
  </div>

  <div class="section-title">Cache hit ratio vs turn index <span class="muted" style="font-weight:400;font-size:11px">(F1 cache_decay fix — bucket-band median; stable curve = stable prefix, dropping curve = volatile content reaching the prompt head)</span></div>
  <div class="panel">
    <table>
      <thead><tr>
        <th>Bucket</th>
        <th style="text-align:right">Sessions</th>
        <th>Turn-index curve (median cache-hit %)</th>
      </tr></thead>
      <tbody>{cache_decay_rows}</tbody>
    </table>
    <div class="muted" style="margin-top:8px;font-size:11px">Empty buckets are skipped. A bucket's curve ends at its shortest session so the median is over the same number of sessions at every turn index.</div>
  </div>

  <div class="section-title">Active Sessions <span class="muted" style="font-weight:400;font-size:11px">(worktree state: main / live / fresh)</span></div>
  <div class="panel" style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Session</th><th>Branch</th><th>Worktree</th><th>Model</th><th>Started</th>
        <th style="text-align:right">Input</th><th style="text-align:right">Output</th>
        <th style="text-align:right">Tools</th>
        <th style="text-align:right">Cache Hit</th><th style="text-align:right">Cost</th>
        <th style="text-align:right">Score</th><th>Warnings</th>
      </tr></thead>
      <tbody>{active_session_rows}</tbody>
    </table>
  </div>

  <div class="section-title">Inactive Sessions <span class="muted" style="font-weight:400;font-size:11px">(worktree state: merged / gone — stale, safe to clean up)</span></div>
  <div class="panel" style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Session</th><th>Branch</th><th>Worktree</th><th>Model</th><th>Started</th>
        <th style="text-align:right">Input</th><th style="text-align:right">Output</th>
        <th style="text-align:right">Tools</th>
        <th style="text-align:right">Cache Hit</th><th style="text-align:right">Cost</th>
        <th style="text-align:right">Score</th><th>Warnings</th>
      </tr></thead>
      <tbody>{inactive_session_rows}</tbody>
    </table>
  </div>

  <div class="section-title">Transcript Index <span class="muted" style="font-weight:400;font-size:11px">(click a worktree, then a session, to read the full captured log — loaded lazily per worktree)</span></div>
  <div class="panel">
    <table>
      <thead><tr><th>Worktree</th><th style="text-align:right">Sessions</th><th style="text-align:right">Cost</th><th>Open</th></tr></thead>
      <tbody>{transcript_index_rows}</tbody>
    </table>
  </div>

  <div class="section-title">ROI Actions (ranked by estimated savings)</div>
  <div class="panel">
    <ol class="roi">{roi_items}</ol>
  </div>

  <div class="section-title">Actionable Insights &amp; Estimated Savings</div>
  <div class="savings">
    <div class="muted" style="font-size:12px;text-transform:uppercase;letter-spacing:0.06em">Estimated Savings if Recommendations Applied</div>
    <div class="big">${estimated_total:.2f}</div>
    <div class="muted" style="margin-top:6px">cache-miss ${estimated_cache_miss:.2f} · duplicate-read ${estimated_dup_read:.2f} · model-downgrade ${estimated_downgrade:.2f} across the {session_count} sessions.</div>
  </div>
  <div>{warnings_html}</div>

  <div class="section-title">Recommended Optimizations</div>
  <div class="panel">
    <ul class="optimize">{optimize_items}</ul>
  </div>

  <div class="footer">Computed by tools/token_efficiency_analyzer.py · stdlib only · no external assets</div>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Transcript sidecar rendering (index -> worktree -> per-session).
#
# The main dashboard links each worktree to ``<out>.assets/<worktree>/index.html``;
# that page links each session to ``<session>.html`` (full raw transcript).
# Navigation is plain ``<a href>`` so the browser loads a page only when it is
# clicked — genuinely lazy per worktree, and it works under ``file://`` with no
# JS, no fetch, and no server. Sidecar pages reuse the dashboard ``CSS`` plus a
# small ``SIDECAR_CSS`` overlay so index, worktree, and transcript pages share
# one look.
# ---------------------------------------------------------------------------

SIDECAR_CSS = """
.backlinks { margin-bottom: 18px; font-size: 13px; }
.backlinks a { color: var(--accent); text-decoration: none; margin-right: 14px; }
.backlinks a:hover { text-decoration: underline; }
.sess-head { margin-bottom: 20px; }
.sess-head .kv { color: var(--muted); font-size: 13px; margin-top: 4px; }
.sess-head code { color: var(--text); }
.wt-group { margin: 22px 0 8px; font-size: 14px; font-weight: 600; }
.turn { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 12px 0; background: var(--panel); }
.turn-user { border-left: 3px solid var(--accent); }
.turn-assistant { border-left: 3px solid var(--good); }
.turn .role { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 6px; }
.turn .meta { font-size: 11px; color: var(--muted); margin-top: 8px; }
.bubble { white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.55; }
.thinking { white-space: pre-wrap; word-break: break-word; font-size: 12px; color: var(--muted); font-style: italic; border-left: 2px solid var(--border); padding-left: 10px; margin: 8px 0; }
.toolcall { margin: 10px 0; }
.toolcall .tname { font-size: 12px; font-weight: 600; color: var(--warn); }
.io { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; white-space: pre-wrap; word-break: break-word; max-height: 360px; overflow: auto; margin: 6px 0 0; }
details.toolresult { margin: 8px 0 0; }
details.toolresult > summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.empty { color: var(--muted); font-size: 13px; }
"""

SIDECAR_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
{body}
<div class="footer">Computed by tools/token_efficiency_analyzer.py · stdlib only · no external assets</div>
</div>
</body>
</html>
"""


def _sidecar_page(title: str, body: str) -> str:
    """Wrap sidecar body markup in the shared HTML shell (dashboard CSS + overlay)."""
    return SIDECAR_TEMPLATE.format(title=html.escape(title), css=CSS + SIDECAR_CSS, body=body)


def _safe_seg(name: str) -> str:
    """Sanitize a worktree name into a filesystem/URL-safe path segment."""
    s = (name or "").strip()
    if s in ("", "(main)"):
        return "main"
    if s == "(unknown)":
        return "unknown"
    s = re.sub(r"[^\w.\-]", "_", s)
    return s or "unnamed"


def _sid_file(sid: str) -> str:
    """Sanitize a session id into a ``<sid>.html`` sidecar filename."""
    return re.sub(r"[^\w.\-]", "_", sid or "session") + ".html"


def _is_zero_turn_session(s: dict) -> bool:
    """True iff a session has no assistant-turn signal.

    Zero-turn = zero in+out tokens AND zero tool calls. Sessions the user
    started and abandoned before the model replied (a2914f3e / b72bba75 —
    3 + 1 user turns, 0 assistant turns) hit this case. They carry zero
    signal for cost scoring and clutter the Sessions table, so we drop
    them. They remain in the Transcript Index for traceability.
    """
    in_out = int(s.get("input_tokens", 0)) + int(s.get("output_tokens", 0))
    tools = sum(s.get("tool_counts", {}).values())
    return in_out == 0 and tools == 0


def _session_cost(s: dict) -> float:
    """USD cost for one aggregated session (same inputs as the dashboard panels)."""
    return _cost_usd(
        s["model"],
        input_tokens=s["input_tokens"],
        output_tokens=s["output_tokens"],
        cache_write_5m_tokens=s.get("ephemeral_5m", 0),
        cache_write_1h_tokens=s.get("ephemeral_1h", 0),
        cache_write_tokens=s["cache_write_tokens"],
        cache_read_tokens=s["cache_read_tokens"],
    )


def _ts_range(session: dict) -> str:
    """Human-readable ``first → last`` UTC range for a session header."""
    first_ts = session.get("first_ts")
    last_ts = session.get("last_ts")
    if not first_ts:
        return "—"
    fs = first_ts.strftime("%Y-%m-%d %H:%M")
    ls = last_ts.strftime("%H:%M") if last_ts else ""
    return f"{fs} → {ls} UTC" if ls else f"{fs} UTC"


def _flatten_tool_result(content) -> str:
    """Reduce a tool_result ``content`` (str | list-of-blocks | None) to raw text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    out.append(str(blk.get("text") or ""))
                else:
                    try:
                        out.append(json.dumps(blk, ensure_ascii=False))
                    except (TypeError, ValueError):
                        out.append(str(blk))
            else:
                out.append(str(blk))
        return "\n".join(out)
    if content is None:
        return ""
    return str(content)


def _render_content_blocks(content) -> str:
    """Render a message ``content`` (str or block list) into transcript markup."""
    parts: list[str] = []
    if isinstance(content, str):
        if content.strip():
            parts.append(f"<div class='bubble'>{html.escape(content)}</div>")
    elif isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                t = blk.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(f"<div class='bubble'>{html.escape(t)}</div>")
            elif btype == "thinking":
                t = blk.get("thinking")
                if isinstance(t, str) and t.strip():
                    parts.append(f"<div class='thinking'>{html.escape(t)}</div>")
            elif btype == "tool_use":
                name = blk.get("name") or "?"
                try:
                    inp_str = json.dumps(blk.get("input"), indent=2, ensure_ascii=False)
                except (TypeError, ValueError):
                    inp_str = str(blk.get("input"))
                parts.append(
                    f"<div class='toolcall'><div class='tname'>⚙ {html.escape(str(name))}</div>"
                    f"<pre class='io'>{html.escape(inp_str)}</pre></div>"
                )
            elif btype == "tool_result":
                out_str = _flatten_tool_result(blk.get("content"))
                parts.append(
                    "<details class='toolresult'><summary>tool result</summary>"
                    f"<pre class='io'>{html.escape(out_str)}</pre></details>"
                )
    return "".join(parts)


def _render_record(rec: dict) -> str:
    """Render one JSONL record (a user or assistant turn) into transcript markup."""
    rtype = rec.get("type")
    if rtype not in ("user", "assistant"):
        return ""
    msg = rec.get("message") or {}
    inner = _render_content_blocks(msg.get("content"))
    if not inner:
        return ""
    ts = rec.get("timestamp") or ""
    if rtype == "user":
        return (
            f"<div class='turn turn-user'><div class='role'>user</div>{inner}"
            f"<div class='meta'>{html.escape(ts)}</div></div>"
        )
    usage = msg.get("usage") or {}
    meta_bits = [b for b in (
        html.escape(msg.get("model") or ""),
        f"in {usage.get('input_tokens')}" if usage.get("input_tokens") else "",
        f"out {usage.get('output_tokens')}" if usage.get("output_tokens") else "",
        html.escape(ts),
    ) if b]
    return (
        f"<div class='turn turn-assistant'><div class='role'>assistant</div>{inner}"
        f"<div class='meta'>{' · '.join(meta_bits)}</div></div>"
    )


def render_transcript_page(path: Path, session: dict, *, main_href: str, wt_href: str) -> str:
    """Render one session's full raw transcript — every turn, in file order."""
    turns: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                block = _render_record(rec)
                if block:
                    turns.append(block)
    except OSError:
        turns = []

    sid = session.get("session_id", "")
    head = (
        f"<div class='backlinks'><a href='{html.escape(main_href)}'>← Dashboard</a>"
        f"<a href='{html.escape(wt_href)}'>← {html.escape(session.get('worktree') or 'worktree')}</a></div>"
        "<h1>Transcript</h1>"
        "<div class='sess-head'>"
        f"<div class='kv'>session <code>{html.escape(sid)}</code></div>"
        f"<div class='kv'>branch {html.escape(session.get('branch') or '—')} · "
        f"worktree {html.escape(session.get('worktree') or '—')} · "
        f"model {html.escape(session.get('model') or '?')}</div>"
        f"<div class='kv'>{html.escape(_ts_range(session))}</div>"
        "</div>"
    )
    body_inner = "\n".join(turns) or "<div class='empty'>No renderable turns in this transcript.</div>"
    return _sidecar_page(f"Transcript {sid[:8]}", head + body_inner)


def render_worktree_index(worktree: str, sessions: list[dict], *, main_href: str) -> str:
    """Render a worktree's session list (grouped by branch), each linking to a transcript."""
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    by_branch: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        by_branch[s.get("branch") or "—"].append(s)

    blocks: list[str] = []
    for branch in sorted(by_branch):
        rows: list[str] = []
        for s in sorted(by_branch[branch], key=lambda x: x.get("first_ts") or _epoch, reverse=True):
            sid = s.get("session_id", "")
            started = s["first_ts"].strftime("%Y-%m-%d %H:%M") if s.get("first_ts") else "—"
            tokens = s["input_tokens"] + s["output_tokens"] + s["cache_write_tokens"] + s["cache_read_tokens"]
            tools = sum(s["tool_counts"].values())
            rows.append(
                f"<tr><td><a href='{html.escape(_sid_file(sid))}'><code>{html.escape(sid[:8])}</code></a></td>"
                f"<td>{html.escape(s.get('model') or '?')}</td>"
                f"<td class='muted'>{html.escape(started)}</td>"
                f"<td style='text-align:right'>{tokens:,}</td>"
                f"<td style='text-align:right'>{tools:,}</td>"
                f"<td style='text-align:right'>${_session_cost(s):.2f}</td></tr>"
            )
        blocks.append(
            f"<div class='wt-group'>branch: {html.escape(branch)} "
            f"<span class='muted'>({len(by_branch[branch])})</span></div>"
            "<div class='panel' style='overflow-x:auto'><table>"
            "<thead><tr><th>Session</th><th>Model</th><th>Started</th>"
            "<th style='text-align:right'>Tokens</th><th style='text-align:right'>Tools</th>"
            "<th style='text-align:right'>Cost</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

    body = (
        f"<div class='backlinks'><a href='{html.escape(main_href)}'>← Dashboard</a></div>"
        f"<h1>Worktree: {html.escape(worktree)}</h1>"
        f"<div class='subtitle'>{len(sessions)} session(s) · click a session to open its full transcript</div>"
        + ("".join(blocks) or "<div class='empty'>No sessions.</div>")
    )
    return _sidecar_page(f"Worktree {worktree}", body)


# Cache decay bucket boundaries — sessions are bucketed by call count
# so the dashboard shows whether long sessions hold their prefix
# (Iron Law 3) or whether short / medium / long curves diverge.
_CACHE_DECAY_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1-3",  1,  3),
    ("4-10", 4, 10),
    ("11-30", 11, 30),
    ("30+",  31, None),
)


def _bucket_cache_decay(sessions: list[dict]) -> dict:
    """Aggregate the per-turn ``cache_decay`` curve across sessions,
    bucketed by session length (F1 cache_decay fix).

    For each bucket, return a list of ``(turn_index, median_hit)``
    points spanning the shortest session in the bucket so the bucket
    can be plotted turn-by-turn. Sessions longer than the shortest
    contribute to the median at every turn index they cover.

    Empty buckets emit ``points: []`` so the renderer can skip them.
    """
    out: dict[str, dict] = {}
    for label, lo, hi in _CACHE_DECAY_BUCKETS:
        # Collect every (turn_index, hit_ratio) sample across all
        # sessions in this bucket.
        samples_by_turn: dict[int, list[float]] = {}
        n_sessions = 0
        for s in sessions:
            cd = s.get("cache_decay") or []
            n_calls = len(cd)
            if n_calls < lo:
                continue
            if hi is not None and n_calls > hi:
                continue
            n_sessions += 1
            for i, ratio in enumerate(cd):
                samples_by_turn.setdefault(i, []).append(float(ratio))
        if n_sessions == 0:
            out[label] = {"points": [], "n_sessions": 0}
            continue
        # Bucket length = the longest session in the bucket (so the
        # median at turn index N is over sessions with N+1 turns).
        max_len = max((len(s.get("cache_decay") or []) for s in sessions
                       if lo <= len(s.get("cache_decay") or []) <= (hi if hi else 10**9)),
                      default=0)
        points: list[dict] = []
        for i in range(max_len):
            samples = samples_by_turn.get(i, [])
            if not samples:
                continue
            samples_sorted = sorted(samples)
            mid = len(samples_sorted) // 2
            if len(samples_sorted) % 2:
                median = samples_sorted[mid]
            else:
                median = (samples_sorted[mid - 1] + samples_sorted[mid]) / 2
            points.append({
                "turn": i + 1,
                "median": round(median, 4),
                "p25": round(samples_sorted[max(0, len(samples_sorted) // 4)], 4),
                "p75": round(samples_sorted[min(len(samples_sorted) - 1, (3 * len(samples_sorted)) // 4)], 4),
                "n": len(samples),
            })
        out[label] = {"points": points, "n_sessions": n_sessions}
    return out


def build_view_model(
    *,
    repo: str,
    days: int,
    sessions: list[dict],
    scored: list[tuple[dict, dict]],
    warnings_per_session: list[list["Warning"]],
    estimated: dict[str, float] | float,
    cost_gate: tuple[str, list[dict]],
    all_sessions_in_window: list[dict] | None,
    unknown_models: set[str] | None = None,
    wt_meta: dict[str, dict] | None = None,
    stale_cost: float = 0.0,
    stale_pct: float = 0.0,
) -> dict:
    """Build the dashboard view-model — single source of truth shared by
    JSON + HTML sinks (issue #310).

    Before this helper existed, ``main()`` and ``render_dashboard()``
    each independently recomputed the same per-panel aggregations
    (cost_by_repo / cost_by_branch / cost_by_worktree / cost_by_tool /
    cost_by_model + totals + stale_cost + stale_pct). Adding a new
    panel meant touching two places. With this helper, every panel
    is computed once and exposed as a key on the returned view-model
    dict; the JSON output dumps the relevant keys and the HTML
    rendering reads the same keys.

    The view-model is a plain dict (not a dataclass) so:
      * ``main() --json`` can pass it straight to ``json.dumps``
      * ``render_dashboard(view_model=vm)`` can pluck individual panels

    Returned keys:

      ``cost_by_repo``             list of {repo, sessions, cost_usd, share}
      ``cost_by_branch``           list of {branch, sessions, cost_usd, share}
      ``cost_by_worktree``         list of {worktree, sessions, cost_usd, share}
      ``cost_by_worktree_rows``    the canonical worktree row shape
                                   (same as ``_aggregate_worktree_rows``) for
                                   JSON consumers
      ``cost_by_tool``             list of {tool, calls, cost_usd, share}
      ``cost_by_model``            list of {model, sessions, tokens, cost_usd, share}
      ``cache_ttl``                {ttl_read, ttl_5m, ttl_1h, ttl_legacy, ttl_miss,
                                    read_pct, write5m_pct, write1h_pct,
                                    legacy_pct, miss_pct, state} — ``state``
                                    selects which TTL layout the HTML uses
      ``totals``                   {total_cost, total_tokens, input_tokens,
                                    output_tokens, cache_read_tokens,
                                    cache_write_tokens, session_count,
                                    repos_named, avg_score, avg_grade,
                                    avg_cache_hit, avg_cache, avg_density,
                                    avg_redundancy, avg_economy}
      ``active_count``             sessions with non-stale worktree_state
      ``inactive_count``           sessions with stale worktree_state
      ``stale_cost``               sum of cost_usd for inactive sessions
      ``stale_pct``                stale_cost / total_cost
      ``estimated``                {cache_miss, dup_read, model_downgrade, total}
      ``cost_gate``                {"status", "violations"}
      ``unknown_models``           sorted list of model ids that fell back
      ``warnings``                 flat list of {code, level, session_id,
                                   branch, worktree, worktree_state,
                                   estimated_save_usd, reclaim_axis, priority,
                                   evidence} — one per (session, warning) pair
    """
    wt_meta = wt_meta or {}
    repo_pool = all_sessions_in_window if all_sessions_in_window is not None else sessions

    if isinstance(estimated, (int, float)):
        estimated = {
            "cache_miss": float(estimated), "dup_read": 0.0,
            "model_downgrade": 0.0, "total": float(estimated),
        }

    # ---- per-session cost helper (local closure) ----
    def _cost(s: dict) -> float:
        return _cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )

    # ---- totals ----
    session_costs = [_cost(s) for s in sessions]
    total_cost = sum(session_costs)
    total_tokens = sum(
        s["input_tokens"] + s["output_tokens"]
        + s["cache_write_tokens"] + s["cache_read_tokens"]
        for s in sessions
    )
    avg_score = mean(sc["total"] for _, sc in scored) if scored else 0.0
    avg_cache = mean(sc["cache"] for _, sc in scored) if scored else 0.0
    avg_density = mean(sc["density"] for _, sc in scored) if scored else 0.0
    avg_redundancy = mean(sc["redundancy"] for _, sc in scored) if scored else 0.0
    avg_economy = mean(sc["economy"] for _, sc in scored) if scored else 0.0
    # Token-weighted: total cache_read / total (input + cache_read). An unweighted
    # mean over sessions lets many tiny short-session misses drown out the
    # 99.6% of spend that lives in long sessions with healthy cache hits.
    _total_input_billable = sum(s["input_tokens"] + s["cache_read_tokens"] for s, _ in scored)
    _total_cache_read = sum(s["cache_read_tokens"] for s, _ in scored)
    avg_cache_hit = (_total_cache_read / _total_input_billable) if _total_input_billable else 0.0
    # Kept as a diagnostic so we can still see the unweighted mean next to the headline.
    avg_cache_hit_unweighted = mean(sc["cache_hit_ratio"] for _, sc in scored) if scored else 0.0
    avg_grade = _grade_for(avg_score)

    # ---- cost_by_repo ----
    repo_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in repo_pool:
        c = _cost(s)
        repo_costs[s["repo"]][0] += 1
        repo_costs[s["repo"]][1] += c
    repo_total_for_share = sum(rc[1] for rc in repo_costs.values()) or 1.0
    cost_by_repo = [
        {"name": rr, "sessions": int(repo_costs[rr][0]),
         "cost_usd": repo_costs[rr][1],
         "share": repo_costs[rr][1] / repo_total_for_share}
        for rr in sorted(repo_costs, key=lambda k: -repo_costs[k][1])
    ]

    # ---- cost_by_branch ----
    branch_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in repo_pool:
        c = _cost(s)
        bkey = s.get("branch") or "(unknown)"
        branch_costs[bkey][0] += 1
        branch_costs[bkey][1] += c
    branch_total_for_share = sum(bc[1] for bc in branch_costs.values()) or 1.0
    cost_by_branch = [
        {"name": b, "sessions": int(branch_costs[b][0]),
         "cost_usd": branch_costs[b][1],
         "share": branch_costs[b][1] / branch_total_for_share}
        for b in sorted(branch_costs, key=lambda k: -branch_costs[k][1])
    ]

    # ---- cost_by_worktree (per-session view) + cost_by_worktree_rows (JSON shape) ----
    worktree_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in repo_pool:
        c = _cost(s)
        wkey = s.get("worktree") or "(unknown)"
        worktree_costs[wkey][0] += 1
        worktree_costs[wkey][1] += c
    worktree_total_for_share = sum(wc[1] for wc in worktree_costs.values()) or 1.0
    cost_by_worktree = [
        {"name": w, "sessions": int(worktree_costs[w][0]),
         "cost_usd": worktree_costs[w][1],
         "share": worktree_costs[w][1] / worktree_total_for_share}
        for w in sorted(worktree_costs, key=lambda k: -worktree_costs[k][1])
    ]
    # The canonical worktree row shape (uses wt_meta to seed disk-only
    # rows so the panel surfaces every worktree dir, not just the ones
    # with sessions). Same data as ``_aggregate_worktree_rows`` —
    # the JSON sink uses this list verbatim.
    cost_by_worktree_rows = _aggregate_worktree_rows(sessions, wt_meta)

    # ---- cost_by_tool ----
    tool_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in scored:
        for name, n in s[0]["tool_counts"].items():
            est = n * _DEFAULT_DUP_READ_TOKENS() * _pricing_for(s[0]["model"])["in"] / 1_000_000
            tool_costs[name][0] += n
            tool_costs[name][1] += est
    total_tool_cost = sum(c[1] for c in tool_costs.values()) or 1.0
    cost_by_tool = [
        {"name": name, "calls": int(calls),
         "cost_usd": cost,
         "share": cost / total_tool_cost}
        for name, (calls, cost) in sorted(tool_costs.items(), key=lambda kv: -kv[1][1])
    ]

    # ---- cost_by_model ----
    model_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
    # accumulator: [sessions, total_tokens, cost]
    for s, c in zip(sessions, session_costs):
        tokens = s["input_tokens"] + s["output_tokens"] + s["cache_write_tokens"] + s["cache_read_tokens"]
        key = s["model"] or "(unknown)"
        model_costs[key][0] += 1
        model_costs[key][1] += tokens
        model_costs[key][2] += c
    model_total_for_share = sum(mc[2] for mc in model_costs.values()) or 1.0
    cost_by_model = [
        {"name": m, "sessions": int(model_costs[m][0]),
         "tokens": int(model_costs[m][1]),
         "cost_usd": model_costs[m][2],
         "share": model_costs[m][2] / model_total_for_share}
        for m in sorted(model_costs, key=lambda k: -model_costs[k][2])
    ]

    # ---- cache_ttl ----
    ttl_read = sum(s["cache_read_tokens"] for s in sessions)
    ttl_5m = sum(s.get("ephemeral_5m", 0) for s in sessions)
    ttl_1h = sum(s.get("ephemeral_1h", 0) for s in sessions)
    ttl_legacy = max(0, sum(
        s["cache_write_tokens"] - s.get("ephemeral_5m", 0) - s.get("ephemeral_1h", 0)
        for s in sessions
    ))
    ttl_miss = sum(s["input_tokens"] for s in sessions)
    ttl_writes_total = ttl_5m + ttl_1h + ttl_legacy
    ttl_total = (ttl_read + ttl_writes_total + ttl_miss) or 1
    if ttl_writes_total == 0:
        ttl_state = "empty"
    elif ttl_5m == 0 and ttl_1h == 0:
        ttl_state = "legacy"
    else:
        ttl_state = "split"
    cache_ttl = {
        "ttl_read": ttl_read, "ttl_5m": ttl_5m, "ttl_1h": ttl_1h,
        "ttl_legacy": ttl_legacy, "ttl_miss": ttl_miss,
        "read_pct": ttl_read / ttl_total * 100,
        "write5m_pct": ttl_5m / ttl_total * 100,
        "write1h_pct": ttl_1h / ttl_total * 100,
        "legacy_pct": ttl_legacy / ttl_total * 100,
        "miss_pct": ttl_miss / ttl_total * 100,
        "state": ttl_state,
    }

    # ---- cache_decay (F1) ----
    # Per-turn cache-hit-ratio curve, bucketed by session length so the
    # dashboard can show "do long sessions keep a stable prefix, or does
    # the curve fall toward 0% mid-stream?" Iron Law 3 in
    # rules/session-hygiene.md is what we're measuring here — a sudden
    # drop in cache_decay between adjacent turns means the prefix was
    # invalidated (volatile content reached the head, or a hook injected
    # new state).
    cache_decay = _bucket_cache_decay(sessions)

    # ---- active / inactive ----
    active_count = sum(1 for s in sessions
                       if s.get("worktree_state") not in _stale_worktree_states())
    inactive_count = len(sessions) - active_count

    # ---- warnings ----
    flat_warnings: list[dict] = []
    for (s, _), warns in zip(scored, warnings_per_session):
        for w in warns:
            flat_warnings.append({
                "code": w.code,
                "level": w.level,
                "estimated_save_usd": w.estimated_save_usd,
                "reclaim_axis": w.reclaim_axis,
                "priority": w.priority,
                "evidence": w.evidence,
                "session_id": s["session_id"],
                "branch": s.get("branch", ""),
                "worktree": s.get("worktree", ""),
                "worktree_state": s.get("worktree_state", ""),
            })

    return {
        "cost_by_repo": cost_by_repo,
        "cost_by_branch": cost_by_branch,
        "cost_by_worktree": cost_by_worktree,
        "cost_by_worktree_rows": cost_by_worktree_rows,
        "cost_by_tool": cost_by_tool,
        "cost_by_model": cost_by_model,
        "cache_ttl": cache_ttl,
        "cache_decay": cache_decay,
        "totals": {
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "input_tokens": sum(s["input_tokens"] for s in sessions),
            "output_tokens": sum(s["output_tokens"] for s in sessions),
            "cache_read_tokens": ttl_read,
            "cache_write_tokens": sum(s["cache_write_tokens"] for s in sessions),
            "session_count": len(scored),
            "repos_named": len({s["repo"] for s in sessions}),
            "avg_score": avg_score,
            "avg_grade": avg_grade,
            "avg_cache_hit": avg_cache_hit,
            "avg_cache_hit_unweighted": avg_cache_hit_unweighted,
            "avg_cache": avg_cache,
            "avg_density": avg_density,
            "avg_redundancy": avg_redundancy,
            "avg_economy": avg_economy,
        },
        "active_count": active_count,
        "inactive_count": inactive_count,
        "stale_cost": stale_cost,
        "stale_pct": stale_pct,
        "estimated": {
            "cache_miss": estimated["cache_miss"],
            "dup_read": estimated["dup_read"],
            "model_downgrade": estimated["model_downgrade"],
            "total": estimated["total"],
        },
        "cost_gate": {
            "status": cost_gate[0],
            "violations": list(cost_gate[1]),
        },
        "unknown_models": sorted(unknown_models) if unknown_models else [],
        "warnings": flat_warnings,
    }


