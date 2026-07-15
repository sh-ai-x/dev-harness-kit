#!/usr/bin/env python3
"""Token efficiency analyzer for AI agent (Claude Code / Codex) session logs.

Reads JSONL session transcripts under ``<logs_dir>/<source>/<session>.jsonl``
(default: ``./logs/{claude-code,codex}``), aggregates per-session token and
tool usage for a given repository over the last N days, scores each session
0-100 against four dimensions (Cache Utilization, Output Density, Read
Redundancy, Tool Economy), fires anti-pattern warnings, and emits an HTML
dashboard with embedded CSS.

The dashboard is an index page plus a sibling ``<out>.assets/`` directory of
per-session transcript sidecar pages (``<out>.assets/<worktree>/<session>.html``)
linked from a "Transcript Index" section. Navigation is plain ``<a href>`` —
a worktree's transcripts are only loaded by the browser when clicked, so the
index stays small and each transcript loads lazily under ``file://`` (no JS,
no fetch, no server). Pass ``--no-transcripts`` for an index-only run.

Usage::

    python tools/token_efficiency_analyzer.py --repo "my-project" --days 30

Stdlib only (``json``, ``html``, ``os``, ``sys``, ``collections``, ``argparse``,
``datetime``, ``pathlib``, ``statistics``).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable

#: Worktrees newer than this many seconds with HEAD == origin/main SHA and
#: zero unique commits classify as ``state="fresh"`` instead of ``"merged"``.
#: Without this branch, a freshly-cut worktree (no commits yet) is
#: indistinguishable from a rebase-merged branch (also HEAD == origin/main),
#: so the dashboard marks it ``merged`` + ``stale`` and the user thinks their
#: brand-new worktree is dead weight. 1 hour covers "open dashboard right
#: after `git worktree add`" without overstaying — a forgotten worktree
#: degrades to ``merged`` the next day.
FRESH_WORKTREE_MAX_AGE_SECONDS = 3600


# ---------------------------------------------------------------------------
# Pricing model (USD per 1M tokens).
#
# Two providers are tracked:
#   * Anthropic Claude (opus / sonnet / haiku) — rates match
#     https://platform.claude.com/docs/en/docs/about-claude/pricing
#     current as of 2026-07-11. Prompt-cache multipliers are
#     5m write = 1.25x, 1h write = 2.0x, cache read = 0.1x — these
#     multipliers are documented as universal for the Claude family.
#   * MiniMax (minimax) — MiniMax-M3 standard tier (≤512k input) and
#     MiniMax-M2.7 from https://platform.minimax.io/docs/guides/pricing-paygo
#     current as of 2026-07-11. MiniMax publishes only a single cache-write
#     rate (1.25x base input) — same multiplier Anthropic uses for 5m TTL.
#     We mirror that as cache_write_5m; cache_write_1h is set equal since
#     no separate 1h rate is published for MiniMax.
#
# Cache *write* TTL split (Anthropic): prompt-cache TTL is either 5 minutes
# or 1 hour. The 5-minute write costs 1.25x base input (a one-time priming
# premium that recovers over a few re-uses within the window). The 1-hour
# write costs 2.0x base input — roughly double, since the cache stays valid
# 12x longer. We read both buckets from ``message.usage.cache_creation``
# (``ephemeral_5m_input_tokens`` and ``ephemeral_1h_input_tokens``) so a
# session that pins long-lived context (CLAUDE.md, architecture maps)
# is priced correctly.
#
# Cache *read* is ~10% of base input (Anthropic) or $0.06/M (MiniMax) and
# recovers the miss on subsequent turns. The 0.85 cache-hit threshold in
# the scoring rubric is set just above the typical Anthropic-recommended
# 80% to leave a margin.
#
# Substring matcher in ``pricing_for()`` resolves any variant:
#   "minimax"  → PRICING["minimax"]  (matched BEFORE claude tiers)
#   "opus"     → PRICING["opus"]
#   "sonnet"   → PRICING["sonnet"]
#   "haiku"    → PRICING["haiku"]
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, float]] = {
    "opus":   {"in":  5.00, "out": 25.00, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    "sonnet": {"in":  3.00, "out": 15.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "haiku":  {"in":  1.00, "out":  5.00, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "cache_read": 0.10},
    # MiniMax — M3 standard tier (≤512k input) and M2.7.
    # "Permanent 50% off" price (the strike-through $0.60/$2.40 is the list rate).
    "minimax": {"in": 0.30, "out": 1.20, "cache_write_5m": 0.375, "cache_write_1h": 0.375, "cache_read": 0.06},
}
DEFAULT_PRICING_KEY = "sonnet"
DEFAULT_CACHE_HIT_TARGET = 0.85   # score = 100 at this ratio; below 0.50 = critical warning
DEFAULT_DUP_READ_TOKENS  = 2000   # heuristic: each duplicate Read = ~2K token waste

# Cost Gate thresholds (per-session). Anything above either fires a stderr WARN.
DEFAULT_COST_GATE_TOKENS = 200_000   # input + cache_read in one session
DEFAULT_COST_GATE_USD    = 5.00      # dollar cost in one session

#: Worktree states that mean "this session's branch is done" (merged into
#: origin/main or the worktree dir is already gone). A session stamped with
#: one of these states is bucketed as "Inactive" on the dashboard and counted
#: into Stale Cost -- everything else (main/live/fresh/unknown) is "Active".
STALE_WORKTREE_STATES = frozenset({"merged", "gone"})

# Recommendation strings keyed by warning code. Rendered into the
# "Recommended Optimizations" block in the dashboard.
WARNING_RECOMMENDATIONS: dict[str, str] = {
    "CACHE_HIT_LOW":     "단기 세션 캐시 적중률 저조: 세션 도중 CLAUDE.md 수정을 금지하고 자주 변하는 데이터(날짜/시간)는 프롬프트 맨 뒤로 옮기세요.",
    "READ_HEAVY":        "툴 호출 과다: architecture.md에 구조 지도를 추가하여 탐색 시간을 절감하고 큰 파일은 한 번만 읽어 캐시에 핀(Pin)하세요.",
    "WRITE_NOT_REUSED":  "비효율적 프리픽스 캐싱: 5분 안에 2~3번 재사용되지 않을 데이터는 캐시 앞단에 두지 마세요. 첫 호출(Write)이 25%(5m) 또는 100%(1h) 더 비쌉니다.",
    "HEAVY_CONTEXT":     "장기 세션 컨텍스트 비대화: 무거운 탐색은 서브에이전트에 위임하거나 /compact로 적시에 압축하세요.",
    "MODEL_OVERSPEC":    "모델 오버스펙: 단순 typo 수정/탐색 작업에는 Opus 대신 Sonnet/Haiku로 다운그레이드해 절감하세요.",
    "REPEATED_USER_MSG": "반복된 사용자 메시지: 막힐 때마다 새 세션을 만들거나 이미 끝난 작업 노드를 컨텍스트에서 즉시 제거하세요.",
}

# "Don't do" strings keyed by warning code. Rendered into the same block,
# listing anti-patterns that were *not* observed (so the user sees the
# contrast and remembers what to keep avoiding).
WARNING_DONT: dict[str, str] = {
    "CACHE_HIT_LOW":     "세션 중간에 모델이나 CLAUDE.md를 바꾸지 마세요. 한 토큰만 엇갈려도 전체 캐시가 무효화됩니다.",
    "READ_HEAVY":        "매 턴 큰 파일을 처음부터 다시 읽지 마세요. 카르토그래피(구조 지도)를 한 번 만들고 진입점만 재사용하세요.",
    "WRITE_NOT_REUSED":  "재사용 빈도 낮은 컨텐츠(날짜/시간/임시 ID)를 prefix 앞에 두지 마세요.",
    "HEAVY_CONTEXT":     "/clear 후 새 세션으로 도망가지 마세요. /compact와 서브에이전트 위임을 우선 검토하세요.",
    "MODEL_OVERSPEC":    "설계든 typo 수정이라도 무조건 Opus를 쓰지 마세요. 작업 성격에 맞춰 모델을 선택하세요.",
    "REPEATED_USER_MSG": "이미 캐시된 컨텍스트를 user message로 반복 주입하지 마세요.",
}

# Cache TTL caveat text. Rendered under the Cache TTL Mix panel.
CACHE_TTL_CAVEAT = (
    "5m TTL 쓰기는 base input의 1.25배, 1h TTL 쓰기는 2.0배입니다. "
    "재사용 간격이 1h를 넘는 데이터만 1h로 캐시하세요. "
    "그 외에는 5m 또는 캐시 없음이 더 저렴합니다. "
    "(MiniMax는 별도 1h 요금을 공개하지 않아 5m과 동일한 요금이 적용됩니다.)"
)


def load_pricing_override(path: Path | None) -> None:
    """Merge a JSON pricing override into the module-level PRICING dict.

    The JSON shape mirrors PRICING: ``{"opus": {"in": ..., "out": ..., ...}, ...}``.
    A non-existent file is a no-op (CLI flag is optional).
    """
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for tier, row in data.items():
        if not isinstance(row, dict):
            continue
        existing = PRICING.setdefault(tier, {})
        for k, v in row.items():
            if isinstance(v, (int, float)):
                existing[k] = float(v)


def pricing_for(model_id: str, *,
                _unknown_models: set[str] | None = None) -> dict[str, float]:
    """Pick the pricing row whose key appears in the model id (case-insensitive).

    Order matters: ``minimax`` is checked before the Claude tiers so a
    hypothetical ``minimax-sonnet`` variant does not get misrouted to Sonnet
    pricing (Sonnet input is 10x more expensive than MiniMax-M3 input).

    If ``_unknown_models`` is provided, ids that match no tier are added to
    the set so the caller can warn on stderr (instead of silently falling
    back to sonnet pricing — which under-counts Opus sessions and over-
    counts Haiku/MiniMax ones).
    """
    if not model_id:
        return PRICING[DEFAULT_PRICING_KEY]
    mid = model_id.lower()
    for key in ("minimax", "opus", "sonnet", "haiku"):
        if key in mid:
            return PRICING[key]
    if _unknown_models is not None:
        _unknown_models.add(model_id)
    return PRICING[DEFAULT_PRICING_KEY]


def cost_usd(model_id: str, *, input_tokens: int, output_tokens: int,
             cache_write_tokens: int = 0, cache_read_tokens: int = 0,
             cache_write_5m_tokens: int = 0, cache_write_1h_tokens: int = 0,
             _unknown_models: set[str] | None = None) -> float:
    """Dollar cost of one assistant turn.

    ``input_tokens`` is the *non-cached* input (what the cache missed on).
    Cached input is billed separately under cache_read. The cache write
    surcharge reflects the TTL-based premium: 5m at 1.25x and 1h at 2.0x
    base input. If only the legacy flat ``cache_write_tokens`` is passed
    (no 5m/1h split), it is priced at the 5m rate for backwards compat.
    """
    p = pricing_for(model_id, _unknown_models=_unknown_models)
    legacy_write = max(0, cache_write_tokens - cache_write_5m_tokens - cache_write_1h_tokens)
    return (
        input_tokens             * p["in"]              / 1_000_000
        + output_tokens          * p["out"]             / 1_000_000
        + cache_write_5m_tokens  * p["cache_write_5m"] / 1_000_000
        + cache_write_1h_tokens  * p["cache_write_1h"] / 1_000_000
        + legacy_write           * p["cache_write_5m"] / 1_000_000
        + cache_read_tokens      * p["cache_read"]      / 1_000_000
    )


# ---------------------------------------------------------------------------
# Log discovery + session aggregation
# ---------------------------------------------------------------------------

def _discover_one_logs_dir(logs_dir: Path) -> list[Path]:
    """Walk ``<logs_dir>/(claude-code|codex)/**`` and return every .jsonl.

    Recurses so per-branch subdirs (``logs/<tool>/<branch>/<sid>.jsonl``) are
    picked up alongside legacy flat files left over from before the branch
    layout existed. Returns [] when ``logs_dir`` does not exist.
    """
    if not logs_dir.exists():
        return []
    out: list[Path] = []
    for sub in ("claude-code", "codex"):
        d = logs_dir / sub
        if d.exists():
            out.extend(sorted(d.rglob("*.jsonl")))
    return out


def discover_logs(logs_dir: Path, *, repo_root: Path | None = None) -> list[Path]:
    """Return every .jsonl under ``<logs_dir>/<source>/**``.

    Walked recursively so per-branch subdirs (``logs/<tool>/<branch>/<sid>.jsonl``)
    are picked up alongside any legacy flat files left over from before the
    branch layout existed.

    When ``repo_root`` is provided, also walk every sibling worktree at
    ``<repo_root>/.claude/worktrees/*/logs/`` so sessions run in any worktree
    are visible from a single ``/dev-kit:token-analyzer`` invocation in the
    main checkout (worktree logs are gitignored and live in separate dirs).
    """
    out = _discover_one_logs_dir(logs_dir)
    if repo_root is not None:
        wt_root = repo_root / ".claude" / "worktrees"
        if wt_root.exists():
            for sub_wt in sorted(wt_root.iterdir()):
                out.extend(_walk_all_worktree_logs(sub_wt))
    return out


def _walk_all_worktree_logs(wt_root: Path, _seen: set | None = None) -> list:
    """Walk <wt_root>/logs/ and recurse into any nested .claude/worktrees/.

    Sessions captured from inside a worktree-created-from-a-worktree (nested
    layout like .claude/worktrees/A/.claude/worktrees/B/) are still real
    sessions and must reach the dashboard. Symlink cycles are bounded by a
    _seen set of resolved paths; the walk stops there instead of looping.
    """
    seen = _seen if _seen is not None else set()
    real = wt_root.resolve()
    if real in seen or not wt_root.exists():
        return []
    seen.add(real)
    out: list = _discover_one_logs_dir(wt_root / "logs")
    nested = wt_root / ".claude" / "worktrees"
    if nested.exists():
        for sub in sorted(nested.iterdir()):
            out.extend(_walk_all_worktree_logs(sub, seen))
    return out


def _dedupe_by_session(file_paths: list[Path]) -> list[Path]:
    """Keep one copy per sessionId across dual-write files.

    ``save_log.py`` dual-writes each capture to both the main checkout's
    ``logs/`` and the worktree's own ``logs/`` (#173). A session that
    moves between main and a worktree across turns also yields two
    capture calls with different content, leaving each file with a
    different snapshot of the same sessionId. Without dedup,
    ``aggregate_session`` runs per-file, so each duplicate contributes
    separately -> cost is double-counted, branch attribution drifts
    (the first-seen file's ``gitBranch`` wins, which can be the stale
    main-side copy), and ``Cost by Worktree`` shows inflated cost.

    Strategy: read each file once, count its ``assistant`` records (proxy
    for snapshot completeness), and prefer the file with the highest
    count. Ties break toward the worktree-side copy (more specific path
    -> correct worktree attribution). Files without a parseable
    ``sessionId`` are kept as-is (legacy flat layout).
    """
    if not file_paths:
        return file_paths

    def _stats(path: Path) -> tuple[int, int]:
        # (assistant_record_count, is_worktree_side)
        assistants = 0
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"type":"assistant"' in line:
                    assistants += 1
        except OSError:
            pass
        return (assistants, 1 if "/.claude/worktrees/" in str(path) else 0)

    chosen: dict[str, Path] = {}
    for p in file_paths:
        sid = None
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                sid = obj.get("sessionId") or obj.get("session_id")
                if sid:
                    break
        except OSError:
            continue
        if not sid:
            continue
        cur = chosen.get(sid)
        if cur is None or _stats(p) > _stats(cur):
            chosen[sid] = p

    if not chosen:
        return file_paths

    keep = set(chosen.values())
    keep_paths = set()
    for p in file_paths:
        if p in keep:
            keep_paths.add(p)
            continue
        # Keep legacy flat files (no sessionId in any line) untouched.
        try:
            any_sid = False
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("sessionId") or obj.get("session_id"):
                    any_sid = True
                    break
            if not any_sid:
                keep_paths.add(p)
        except OSError:
            keep_paths.add(p)
    return [p for p in file_paths if p in keep_paths]


_KNOWN_SOURCES = ("claude-code", "codex")


def _source_for(path: Path) -> str:
    """Identify the source tool subdir for ``path``.

    Walks the path's parts looking for a known source name (``claude-code`` or
    ``codex``). Falls back to ``path.parent.name`` for legacy flat-layout
    files where the immediate parent IS the tool subdir.
    """
    parts = path.parts
    for sub in _KNOWN_SOURCES:
        if sub in parts:
            return sub
    return path.parent.name


def parse_iso(ts: str) -> datetime | None:
    """Best-effort ISO-8601 parser; returns None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def repo_from_cwd(cwd: str | None) -> str:
    """Derive the project root label from ``cwd``.

    For a session running inside a worktree (``.claude/worktrees/<name>/``)
    the project's logical name is the segment immediately above that
    marker, not the worktree dir itself. Walking up lets a single
    ``--repo <project>`` invocation surface sessions from every checkout
    instead of only the main one.
    """
    if not cwd:
        return ""
    parts = Path(cwd).parts
    for i, part in enumerate(parts):
        if (part == ".claude" and i + 2 < len(parts)
                and parts[i + 1] == "worktrees" and i >= 1):
            return parts[i - 1]
    return Path(cwd).name


def worktree_from_cwd(cwd: str | None) -> str:
    """Derive the git worktree dir name from ``cwd``.

    A worktree in this repo lives under ``<repo>/.claude/worktrees/<name>/``
    (project convention enforced by ``.claude/rules/git-workflow.md``).
    Returns ``(main)`` when ``cwd`` is the main checkout, the worktree
    basename when ``cwd`` sits under ``.claude/worktrees/<name>/``, and
    ``(unknown)`` when ``cwd`` is missing. The literal bucket names keep
    the Cost by Worktree panel populated even when only the main checkout
    has been used.
    """
    if not cwd:
        return "(unknown)"
    parts = Path(cwd).parts
    for i, part in enumerate(parts):
        if part == ".claude" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return parts[i + 2]
    return "(main)"


def worktree_from_path(path: Path | str | None) -> str:
    """Derive the git worktree dir name from a JSONL file path.

    Returns the basename of the worktree when ``path`` sits under
    ``<repo>/.claude/worktrees/<name>/logs/``; otherwise ``(main)``.

    Path-based resolution is authoritative because the ``cwd`` recorded
    in a session transcript often points at the parent checkout, not the
    worktree the session actually ran in. When the JSONL is captured from
    inside a worktree dir but ``cwd`` says main, only the file path knows
    the truth — the session belongs to that worktree's bucket.
    """
    if not path:
        return "(main)"
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part == ".claude" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return parts[i + 2]
    return "(main)"


def classify_worktree_dir(
    wt_path: Path,
    repo_root: Path,
    *,
    git_runner=subprocess.run,
    timeout: int = 5,
) -> dict:
    """Classify one worktree dir as live / fresh / merged / gone / unknown.

    State set:
      - ``"fresh"``   fresh worktree (HEAD == origin/main SHA, log empty,
                      worktree dir mtime within ``FRESH_WORKTREE_MAX_AGE_SECONDS``)
      - ``"merged"``  branch's commits are all in ``origin/main`` and the
                      worktree is not fresh (rebase-merge, squash-merge with
                      empty log, or stale fresh worktree past the age threshold)
      - ``"live"``    branch has unique commits not in ``origin/main``
      - ``"gone"``    dir exists on disk but not in ``git worktree list``
      - ``"unknown"`` git call failed (timeout, no origin/main, OS error)

    Git calls (all wrapped in try/except — never raises):

    1. ``git -C <repo_root> worktree list --porcelain`` — whether the dir is
       still registered (``is_listed=True``).
    2. ``git -C <wt_path> rev-parse --short HEAD`` — branch tip short SHA.
    3. ``git -C <wt_path> rev-parse HEAD`` — full HEAD SHA (for fresh detect).
    4. ``git -C <repo_root> rev-parse origin/main`` — full origin/main SHA.
    5. ``git -C <wt_path> log origin/main..HEAD --oneline`` — empty iff every
       commit on the branch is reachable from ``origin/main``.

    Returned dict keys:

    - ``state``               ∈ ``{"live","fresh","merged","gone","unknown"}``
    - ``worktree_listed``     bool — was the dir in ``git worktree list``
    - ``branch_merged_into_main`` bool — only meaningful when ``state=="merged"``
    - ``is_fresh``            bool — True iff ``state=="fresh"`` (mirrors state)
    - ``branch_tip``          str — short SHA, empty on failure
    - ``branch_name``         str — branch refs/heads/<name>, or empty
    """
    def _safe_run(argv: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return git_runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            return None

    # 1. Is the dir in `git worktree list`?
    porcelain_cp = _safe_run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"]
    )
    is_listed = False
    listed_branch_ref = ""
    porcelain = porcelain_cp.stdout if porcelain_cp and porcelain_cp.stdout else ""
    cur_path_str: str | None = None
    wt_resolved = wt_path.resolve()
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            cur_path_str = line.split(" ", 1)[1].strip()
        elif line.startswith("branch ") and cur_path_str:
            try:
                if Path(cur_path_str).resolve() == wt_resolved:
                    is_listed = True
                    listed_branch_ref = line.split(" ", 1)[1].strip()
            except OSError:
                pass
        elif not line.strip():
            cur_path_str = None

    if not is_listed:
        return {
            "state": "gone",
            "worktree_listed": False,
            "branch_merged_into_main": False,
            "is_fresh": False,
            "branch_tip": "",
            "branch_name": "",
        }

    # 2. Branch tip (best-effort).
    tip_cp = _safe_run(
        ["git", "-C", str(wt_path), "rev-parse", "--short", "HEAD"]
    )
    tip = (
        (tip_cp.stdout or "").strip()
        if tip_cp and tip_cp.returncode == 0
        else ""
    )

    # 2b. Full HEAD SHA + origin/main SHA. Used to tell "fresh worktree"
    #     apart from "rebase-merged branch" — both have an empty
    #     ``log origin/main..HEAD``, but only the fresh case has
    #     HEAD == origin/main. (We additionally gate on worktree mtime
    #     because HEAD can also equal origin/main after a fast-forward merge.)
    head_full_cp = _safe_run(["git", "-C", str(wt_path), "rev-parse", "HEAD"])
    head_full = (head_full_cp.stdout or "").strip() if head_full_cp and head_full_cp.returncode == 0 else ""
    main_full_cp = _safe_run(["git", "-C", str(repo_root), "rev-parse", "origin/main"])
    main_full = (main_full_cp.stdout or "").strip() if main_full_cp and main_full_cp.returncode == 0 else ""

    # 3. Empty-diff check. Uniform across linear-, squash-, and rebase-merge:
    #    a worktree is "merged" iff its branch has zero commits not in
    #    origin/main. Replaces the previous ``merge-base --is-ancestor``
    #    test, which failed on every squash/rebase merge (PR #158 itself
    #    is a squash-merge: branch tip 52f4d23 is not an ancestor of
    #    9dca0ee, so the old test mis-classified the merged worktree as
    #    ``live`` and stale_cost was $0.00).
    log_cp = _safe_run(
        ["git", "-C", str(wt_path), "log", "origin/main..HEAD", "--oneline"]
    )
    is_fresh = False
    if log_cp is None or log_cp.returncode < 0 or log_cp.returncode >= 2:
        state = "unknown"
        merged = False
    elif log_cp.returncode == 0:
        unique = [l for l in (log_cp.stdout or "").splitlines() if l.strip()]
        if not unique:
            # Distinguish fresh vs rebase-merged: HEAD == origin/main SHA AND
            # the worktree dir is recent. Otherwise (rebase-merge, fast-forward
            # merge, or stale forgotten fresh worktree) it's "merged".
            recent = False
            try:
                wt_mtime = wt_path.stat().st_mtime
                recent = (time.time() - wt_mtime) < FRESH_WORKTREE_MAX_AGE_SECONDS
            except OSError:
                recent = False
            if head_full and main_full and head_full == main_full and recent:
                state = "fresh"
                merged = False
                is_fresh = True
            else:
                state = "merged"
                merged = True
        else:
            state = "live"
            merged = False
    else:
        state = "unknown"
        merged = False

    return {
        "state": state,
        "worktree_listed": True,
        "branch_merged_into_main": merged,
        "is_fresh": is_fresh,
        "branch_tip": tip,
        "branch_name": listed_branch_ref.removeprefix("refs/heads/"),
    }


def classify_all_worktrees(
    repo_root: Path,
    *,
    git_runner=subprocess.run,
    timeout: int = 5,
) -> dict[str, dict]:
    """Classify every ``<repo_root>/.claude/worktrees/*`` dir.

    Always includes the sentinel key ``"(main)"`` mapped to
    ``{"state": "main", ...}`` so consumers can dereference it without a
    separate branch.

    Returns ``{dirname: meta}`` where ``dirname`` matches ``worktree_from_cwd``
    output (basename of the worktree dir, or ``"(main)"``).
    Silently skips any worktree dir whose classification comes back ``unknown``
    via the exception path (it is still included in the result with
    ``state="unknown"``).
    """
    meta: dict[str, dict] = {
        "(main)": {
            "state": "main",
            "worktree_listed": True,
            "branch_merged_into_main": False,
            "is_fresh": False,
            "branch_tip": "",
            "branch_name": "",
        },
    }
    wt_root = Path(repo_root) / ".claude" / "worktrees"
    if not wt_root.exists() or not wt_root.is_dir():
        return meta
    for child in sorted(wt_root.iterdir()):
        if not child.is_dir():
            continue
        meta[child.name] = classify_worktree_dir(
            child, Path(repo_root), git_runner=git_runner, timeout=timeout
        )
    return meta


def _aggregate_worktree_rows(
    selected: list[dict],
    wt_meta: dict[str, dict] | None,
) -> list[dict]:
    """Build the JSON ``worktrees`` payload.

    Each session's ``worktree_state`` stamp is authoritative for that
    worktree. ``wt_meta`` backfills branch-tip metadata into session rows
    AND seeds zero-cost rows for any disk worktree that did not run a
    session inside the window — otherwise those dirs disappear from the
    dashboard and stale worktrees with past spend go unnoticed. Works
    correctly when ``wt_meta`` is empty / ``None`` (e.g. a non-worktree
    cwd): session-derived rows still appear.
    """
    wt_meta = wt_meta or {}
    by_label: dict[str, dict] = {}
    for s in selected:
        wt = s.get("worktree") or "(unknown)"
        meta = by_label.setdefault(wt, {
            "name": wt,
            "state": s.get("worktree_state", "unknown"),
            "sessions": 0,
            "cost_usd": 0.0,
            "branch_merged_into_main": False,
            "branch_tip": "",
            "branch_name": "",
        })
        meta["sessions"] += 1
        meta["cost_usd"] += cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )
    # Backfill cross-check info from wt_meta (only updates fields the
    # session-derived state didn't authoritatively set).
    for label, meta in by_label.items():
        wt = wt_meta.get(label)
        if wt:
            meta["branch_merged_into_main"] = bool(
                wt.get("branch_merged_into_main", meta["branch_merged_into_main"])
            )
            if not meta["branch_tip"]:
                meta["branch_tip"] = wt.get("branch_tip", "")
            if not meta["branch_name"]:
                meta["branch_name"] = wt.get("branch_name", "")
    # Seed zero-cost rows for disk-only worktrees so the panel surfaces
    # every worktree dir (live / merged / gone). State comes straight
    # from wt_meta — no session can override it for these rows. Without
    # this loop, a repo whose sessions all ran in (main) shows a one-row
    # panel and hides every stale / orphan worktree on disk.
    for label, wt in wt_meta.items():
        if label in by_label:
            continue
        by_label[label] = {
            "name": label,
            "state": wt.get("state", "unknown"),
            "sessions": 0,
            "cost_usd": 0.0,
            "branch_merged_into_main": bool(wt.get("branch_merged_into_main", False)),
            "branch_tip": wt.get("branch_tip", ""),
            "branch_name": wt.get("branch_name", ""),
        }
    # Sort: (main) first, then by cost desc.
    rows = sorted(
        by_label.values(),
        key=lambda r: (r["name"] != "(main)", -r["cost_usd"]),
    )
    return [{**r, "cost_usd": round(r["cost_usd"], 4)} for r in rows]


def aggregate_session(path: Path) -> dict | None:
    """Walk one JSONL file once and return per-session aggregates, or None."""
    session_id: str | None = None
    repo = ""
    source = _source_for(path)
    models: Counter[str] = Counter()
    input_tokens = 0
    output_tokens = 0
    cache_write_tokens = 0
    cache_read_tokens = 0
    ephemeral_5m = 0
    ephemeral_1h = 0
    tool_counts: Counter[str] = Counter()
    read_files: Counter[str] = Counter()
    user_texts: list[str] = []
    branch_counts: Counter[str] = Counter()
    worktree_counts: Counter[str] = Counter()
    first_ts: datetime | None = None
    last_ts: datetime | None = None

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

                ts = parse_iso(rec.get("timestamp") or "")
                if ts is not None:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts

                if session_id is None:
                    session_id = rec.get("sessionId") or rec.get("session_id") or path.stem
                if not repo:
                    repo = repo_from_cwd(rec.get("cwd"))
                gb = rec.get("gitBranch")
                if isinstance(gb, str) and gb.strip():
                    branch_counts[gb.strip()] += 1
                cwd_raw = rec.get("cwd")
                if isinstance(cwd_raw, str) and cwd_raw.strip():
                    worktree_counts[worktree_from_cwd(cwd_raw)] += 1

                msg = rec.get("message") or {}
                rec_type = rec.get("type")

                if rec_type == "assistant":
                    m = msg.get("model")
                    if m:
                        models[m] += 1
                    u = msg.get("usage") or {}
                    # input_tokens = non-cached input (cache missed)
                    input_tokens       += int(u.get("input_tokens") or 0)
                    output_tokens      += int(u.get("output_tokens") or 0)
                    cache_write_tokens += int(u.get("cache_creation_input_tokens") or 0)
                    cache_read_tokens  += int(u.get("cache_read_input_tokens") or 0)
                    cc = u.get("cache_creation") or {}
                    ephemeral_5m += int(cc.get("ephemeral_5m_input_tokens") or 0)
                    ephemeral_1h += int(cc.get("ephemeral_1h_input_tokens") or 0)

                    for blk in (msg.get("content") or []):
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "tool_use":
                            name = blk.get("name") or "?"
                            tool_counts[name] += 1
                            if name == "Read":
                                inp = blk.get("input") or {}
                                fp = inp.get("file_path") or inp.get("path") or ""
                                if fp:
                                    read_files[fp] += 1

                elif rec_type == "user":
                    c = msg.get("content")
                    if isinstance(c, str):
                        if c.strip():
                            user_texts.append(c.strip())
                    elif isinstance(c, list):
                        parts: list[str] = []
                        for blk in c:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                t = blk.get("text")
                                if isinstance(t, str):
                                    parts.append(t)
                        joined = "\n".join(parts).strip()
                        if joined:
                            user_texts.append(joined)

    except OSError:
        return None

    if session_id is None:
        return None

    # Branch: prefer wire-format ``gitBranch`` (the most-common value across
    # all lines, since users can switch branches mid-session in theory).
    # Fall back to the immediate parent dir name. Legacy flat files have
    # ``path.parent.name == "claude-code"`` or ``"codex"`` (the tool subdir
    # itself) — those bucket under ``"main"`` so they aren't mis-attributed
    # to a tool dir.
    if branch_counts:
        branch = branch_counts.most_common(1)[0][0]
    else:
        parent = path.parent.name
        branch = "main" if parent in _KNOWN_SOURCES else (parent or "main")

    # Worktree: prefer the file path (authoritative — cwd can misattribute
    # when the JSONL was captured from a parent checkout but lives under a
    # sibling worktree's logs dir). Fall back to the cwd-derived Counter so
    # legacy flat-layout files (no worktree segment in the path) keep working.
    worktree = worktree_from_path(path)
    if worktree == "(main)":
        worktree = worktree_counts.most_common(1)[0][0] if worktree_counts else "(unknown)"

    return {
        "session_id": session_id,
        "source": source,
        "repo": repo or path.stem.split("__")[0],
        "branch": branch,
        "worktree": worktree,
        "model": models.most_common(1)[0][0] if models else "",
        "first_ts": first_ts,
        "last_ts": last_ts,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_read_tokens": cache_read_tokens,
        "ephemeral_5m": ephemeral_5m,
        "ephemeral_1h": ephemeral_1h,
        "tool_counts": tool_counts,
        "read_files": read_files,
        "user_texts": user_texts,
        "log_path": str(path),
    }


# ---------------------------------------------------------------------------
# Scoring rubric (per session, 0-100 weighted)
#
# Weights: cache 0.40, density 0.20, redundancy 0.20, economy 0.20.
# Total is the weighted sum; each dim is reported alongside.
#
# Cache Utilization uses a *stepped* curve so a 0.85 hit ratio scores the
# full 100 (Anthropic's recommended minimum). Below 0.50 the score is
# steeply penalized (1:1 with the ratio) — sessions that never hit cache
# get 0; sessions at 0.50 hit get 50. Between 0.50 and 0.85 the slope
# softens (≈142.86 per unit) so partial credit is awarded.
# ---------------------------------------------------------------------------

WEIGHT_CACHE       = 0.40
WEIGHT_DENSITY     = 0.20
WEIGHT_REDUNDANCY  = 0.20
WEIGHT_ECONOMY     = 0.20
assert round(WEIGHT_CACHE + WEIGHT_DENSITY + WEIGHT_REDUNDANCY + WEIGHT_ECONOMY, 6) == 1.0

CACHE_HIT_FULL = 0.85
CACHE_HIT_WARN = 0.50

GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (90.0, "A"), (80.0, "B"), (70.0, "C"), (60.0, "D"),
)


def grade_for(total_score: float) -> str:
    """A: 90+, B: 80+, C: 70+, D: 60+, F: <60."""
    for threshold, letter in GRADE_BANDS:
        if total_score >= threshold:
            return letter
    return "F"


def score_cache_utilization(cache_hit: float) -> float:
    """Stepped curve: 0 -> 0, 0.50 -> 50, 0.85 -> 100, >0.85 -> 100.

    Below 0.50: slope 1:1 (1 unit of ratio = 1 unit of score).
    0.50 -> 0.85: slope ≈ 142.86 (50 units of score across 0.35 ratio).
    >= 0.85: capped at 100 (avoid over-rewarding marginal gains).
    """
    if cache_hit >= CACHE_HIT_FULL:
        return 100.0
    if cache_hit >= CACHE_HIT_WARN:
        return round(50.0 + (cache_hit - CACHE_HIT_WARN) * (50.0 / (CACHE_HIT_FULL - CACHE_HIT_WARN)), 1)
    return round(cache_hit * 100.0, 1)


def score_session(s: dict) -> dict:
    """Apply the 4-dim rubric. Returns a dict of dim scores + a weighted total.

    Cache Utilization (weight 0.40)
        cache_read / (input + cache_read). Stepped curve, full marks at 0.85.
        Low ratio = critical penalty because the prompt prefix is misaligned
        and we keep re-priming.

    Output Density (weight 0.20)
        output / (input + cache_read). Sessions that only read without ever
        producing artifacts score near 0; sessions that ship a lot of output
        relative to input score high.

    Read Redundancy (weight 0.20)
        Penalty scales with the worst repeated file read (max count of any
        single file in read_files). Reading the same 5000-line file 8 times
        is a cartography failure.

    Tool Economy (weight 0.20)
        tool_calls per 1K output tokens. Excess calls for thin output = wasted
        spend (often a confused agent thrashing).
    """
    total_input = s["input_tokens"] + s["cache_read_tokens"]
    cache_hit = (s["cache_read_tokens"] / total_input) if total_input else 0.0
    s_cache = score_cache_utilization(cache_hit)

    out_density = s["output_tokens"] / total_input if total_input else 0.0
    # Map density: 0 -> 0, 0.10 -> 50, 0.25+ -> 100 (cap)
    s_density = round(min(100.0, max(0.0, out_density) * 400.0), 1)

    max_repeat = max(s["read_files"].values(), default=0)
    # 1 reread ok, 5+ is bad; cap at 0 score for >= 10.
    s_redundancy = round(max(0.0, 100.0 - (max_repeat - 1) * 12.5), 1)

    total_tools = sum(s["tool_counts"].values())
    tools_per_1k_out = total_tools / max(1.0, s["output_tokens"] / 1000.0)
    # 0 tools/1k -> 100; 50+ tools/1k -> 0
    s_economy = round(max(0.0, 100.0 - tools_per_1k_out * 2.0), 1)

    total = round(
        WEIGHT_CACHE       * s_cache
        + WEIGHT_DENSITY   * s_density
        + WEIGHT_REDUNDANCY * s_redundancy
        + WEIGHT_ECONOMY   * s_economy,
        1,
    )
    return {
        "cache": s_cache,
        "density": s_density,
        "redundancy": s_redundancy,
        "economy": s_economy,
        "total": total,
        "grade": grade_for(total),
        "cache_hit_ratio": cache_hit,
        "max_repeat_reads": max_repeat,
        "tools_per_1k_out": tools_per_1k_out,
    }


# ---------------------------------------------------------------------------
# Warning engine (anti-pattern detection) with $ attribution.
#
# Each trigger maps to one of the messages from the meta-prompt. Messages
# are prefixed with the emoji already; we keep them intact so the dashboard
# can render them verbatim.
#
# Each Warning is tagged with ``reclaim_axis`` (cache_miss / dup_read /
# model_downgrade) so the dashboard can sum per-axis $ attribution and
# rank ROI actions by descending savings.
# ---------------------------------------------------------------------------

@dataclass
class Warning:
    """A single anti-pattern finding attached to one session."""
    level: str                # "critical" | "warn"
    code: str                 # "CACHE_HIT_LOW", "READ_HEAVY", ...
    message: str              # full emoji-prefixed Korean message
    estimated_save_usd: float = 0.0   # populated by evaluate_warnings
    priority: int = 0                  # 1 = highest; set from reclaim axis
    reclaim_axis: str = ""             # cache_miss | dup_read | model_downgrade | ""
    session_id: str = ""               # which session this instance fired on
    evidence: str = ""                 # concrete number/file/text that drove the $ estimate


def evaluate_warnings(s: dict, score: dict,
                      reclaim_cache_miss: float = 0.0,
                      reclaim_dup_read: float = 0.0,
                      reclaim_downgrade: float = 0.0) -> list[Warning]:
    """Return a list of Warning instances with $ attribution populated.

    The three ``reclaim_*`` floats are the *session-attributable* dollar
    savings from cache_miss_reclaim / dup_read_reclaim / model_downgrade_reclaim.
    Each warning that maps to an axis inherits its share so the dashboard
    can rank by ``estimated_save_usd`` descending.
    """
    warnings: list[Warning] = []
    total_input = s["input_tokens"] + s["cache_read_tokens"]
    cache_hit = score["cache_hit_ratio"]

    # 1. Cache hit < 50% — prefix misalignment suspected.
    if total_input > 0 and cache_hit < 0.50:
        warnings.append(Warning(
            level="critical",
            code="CACHE_HIT_LOW",
            message=(
                "🚨 캐시 적중률 50% 미만: 프리픽스(Prefix) 정렬이 깨졌을 "
                "확률이 높습니다. 자주 변하는 데이터(날짜, 시간 등)는 프롬프트 "
                "맨 뒤로 빼고, 세션 중간에 모델이나 CLAUDE.md를 변경하지 마세요. "
                "한 토큰만 엇갈려도 전체 캐시가 무효화됩니다."
            ),
            estimated_save_usd=round(reclaim_cache_miss, 2),
            priority=1,
            reclaim_axis="cache_miss",
            session_id=s["session_id"],
            evidence=f"cache hit {cache_hit:.0%} (target {DEFAULT_CACHE_HIT_TARGET:.0%})",
        ))

    # 2. Read tool cost >= 40% of total tool-imputed cost.
    #
    # We impute tool cost as: ``n_calls * 2K_tokens * base_input_price``.
    # This is a heuristic proxy for "context the tool surfaces back to the
    # model," not a billing-API call. Real Anthropic billing does not
    # break out per-tool spend, so this is the best approximation.
    tool_costs: dict[str, float] = {}
    for name, n in s["tool_counts"].items():
        tool_costs[name] = n * 2000 * pricing_for(s["model"])["in"] / 1_000_000
    total_tool_cost = sum(tool_costs.values()) or 1.0
    read_share = tool_costs.get("Read", 0.0) / total_tool_cost
    if read_share >= 0.40 and s["tool_counts"].get("Read", 0) > 0:
        if s["read_files"]:
            top_file, top_n = max(s["read_files"].items(), key=lambda kv: kv[1])
            read_evidence = f"'{top_file}' read {top_n}x"
        else:
            read_evidence = f"Read = {read_share:.0%} of tool cost"
        warnings.append(Warning(
            level="critical",
            code="READ_HEAVY",
            message=(
                "🚨 대용량 파일 Turn Read 의심: 파일을 반복해서 읽고 있습니다. "
                "큰 파일은 한 번 읽어 캐시에 고정(Pin)하고, 아키텍처 지도"
                "(Cartography)를 만들어 에이전트가 진입점을 바로 찾게 하세요."
            ),
            estimated_save_usd=round(reclaim_dup_read, 2),
            priority=2,
            reclaim_axis="dup_read",
            session_id=s["session_id"],
            evidence=read_evidence,
        ))

    # 3. Context growth > 500K (one session accumulating a lot of input).
    if total_input > 500_000:
        warnings.append(Warning(
            level="warn",
            code="HEAVY_CONTEXT",
            message=(
                "💡 무거운 탐색 위임 권고: 무거운 탐색은 Sub-agent에게 위임하고, "
                "메인 세션에는 요약본만 넘기세요. 장기 세션의 경우 /compact "
                "명령으로 적시에 컨텍스트를 압축해야 합니다."
            ),
            estimated_save_usd=round(reclaim_cache_miss, 2),
            priority=3,
            reclaim_axis="cache_miss",
            session_id=s["session_id"],
            evidence=f"{total_input:,} input tokens (> 500,000)",
        ))

    # 4. Opus on low-density simple work.
    is_opus = "opus" in (s["model"] or "").lower()
    if is_opus and score["density"] < 20.0 and s["output_tokens"] > 0:
        warnings.append(Warning(
            level="warn",
            code="MODEL_OVERSPEC",
            message=(
                "💡 모델 오버스펙: 단순 타이포 수정이나 간단한 로직에는 작업 "
                "성격에 맞춰 하위 모델(Sonnet/Haiku)로 다운그레이드 하세요."
            ),
            estimated_save_usd=round(reclaim_downgrade, 2),
            priority=4,
            reclaim_axis="model_downgrade",
            session_id=s["session_id"],
            evidence=f"opus + density {score['density']:.0f}/100 (< 20)",
        ))

    # 5. Cache writes high but reads < 2 per write on average.
    writes = s["cache_write_tokens"]
    if writes > 50_000 and s["cache_read_tokens"] < 2 * writes:
        warnings.append(Warning(
            level="critical",
            code="WRITE_NOT_REUSED",
            message=(
                "🚨 비효율적 프리픽스 캐싱: 첫 호출(Write)은 25% 더 비쌉니다. "
                "5분 안에 2~3번 이상 재사용되지 않을 데이터는 캐시 앞단에 "
                "두지 마세요."
            ),
            estimated_save_usd=round(reclaim_cache_miss, 2),
            priority=2,
            reclaim_axis="cache_miss",
            session_id=s["session_id"],
            evidence=f"{writes:,} cache-write vs {s['cache_read_tokens']:,} cache-read tokens",
        ))

    # 6. Repeated user messages (same text appears >= 2 times).
    repeats = [(t, n) for t, n in Counter(s["user_texts"]).items() if n >= 2 and len(t) > 5]
    if repeats:
        top_text, top_n = max(repeats, key=lambda tn: tn[1])
        truncated = top_text[:50] + ("…" if len(top_text) > 50 else "")
        warnings.append(Warning(
            level="critical",
            code="REPEATED_USER_MSG",
            message=(
                "🚨 안티패턴 감지: 막힐 때마다 세션을 새로 파거나, 이미 캐시된 "
                "컨텍스트를 유저 메시지로 반복 주입하지 마세요. 끝난 작업의 "
                "노드는 컨텍스트에서 즉시 제거하세요."
            ),
            estimated_save_usd=round(reclaim_cache_miss, 2),
            priority=3,
            reclaim_axis="cache_miss",
            session_id=s["session_id"],
            evidence=f"“{truncated}” × {top_n}",
        ))

    return warnings


# ---------------------------------------------------------------------------
# Estimated savings — split into three reclaim axes.
#
# Conservative model: for each session, compute the *delta* between the
# actual cost and an "optimized" cost. The three axes map 1:1 to the
# dashboard's ROI Actions and Warning.reclaim_axis tags:
#
#   1. cache_miss_reclaim      — what we save by hitting the target cache ratio
#                                (default 85% — Anthropic's recommended minimum).
#                                Tokens are *shifted* from billable input into
#                                cache_read until the session hits the target.
#                                Saved = shifted * (input_price - cache_read_price).
#
#   2. dup_read_reclaim        — waste from re-reading the same file in one
#                                session. Each duplicate Read above 1 is assumed
#                                to surface ~2K tokens of context that could
#                                otherwise be served from cache. Priced at base
#                                input.
#
#   3. model_downgrade_reclaim — what we save by swapping Opus sessions
#                                (low density < 20) to Sonnet for the SAME
#                                token volume. Difference in total cost.
#
# Each function takes a list of (session, score) tuples AND returns a
# parallel list of per-session $ values — so evaluate_warnings() can
# attribute axis share to its warnings. The aggregate sums feed the
# dashboard's "Estimated Savings" panel and the JSON output.
# ---------------------------------------------------------------------------

def cache_miss_reclaim(scored: list[tuple[dict, dict]],
                       target_hit: float = DEFAULT_CACHE_HIT_TARGET
                      ) -> list[float]:
    """Per-session USD saved if cache_hit reached ``target_hit``."""
    out: list[float] = []
    for s, sc in scored:
        current_hit = sc["cache_hit_ratio"]
        total_input = s["input_tokens"] + s["cache_read_tokens"]
        if current_hit < target_hit and total_input > 0:
            shift = (target_hit - current_hit) * total_input
            p = pricing_for(s["model"])
            out.append(round(shift * (p["in"] - p["cache_read"]) / 1_000_000, 2))
        else:
            out.append(0.0)
    return out


def dup_read_reclaim(scored: list[tuple[dict, dict]],
                     tokens_per_dup: int = DEFAULT_DUP_READ_TOKENS) -> list[float]:
    """Per-session USD saved if no file were read more than once."""
    out: list[float] = []
    for s, _ in scored:
        dup_tokens = sum(max(0, n - 1) for n in s["read_files"].values()) * tokens_per_dup
        out.append(round(dup_tokens * pricing_for(s["model"])["in"] / 1_000_000, 2))
    return out


def model_downgrade_reclaim(scored: list[tuple[dict, dict]]) -> list[float]:
    """Per-session USD saved if Opus sessions with density<20 swapped to Sonnet.

    Other models: $0 (already at the bottom of the tier ladder, or already
    on the right model). We intentionally do NOT recommend Sonnet→Haiku
    here — too aggressive a downgrade for the heuristic to make blindly.
    """
    out: list[float] = []
    for s, sc in scored:
        is_opus = "opus" in (s["model"] or "").lower()
        if not (is_opus and sc["density"] < 20.0 and s["output_tokens"] > 0):
            out.append(0.0)
            continue
        opus_cost = cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_read_tokens=s["cache_read_tokens"],
        )
        sonnet_cost = cost_usd(
            "sonnet",
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_read_tokens=s["cache_read_tokens"],
        )
        out.append(round(max(0.0, opus_cost - sonnet_cost), 2))
    return out


def estimated_savings(scored: list[tuple[dict, dict]]) -> dict[str, float]:
    """Return total $ savings split by reclaim axis. Backwards-compatible:
    callers that used the old single-float API should switch to
    ``estimated_savings(...)["total"]``.
    """
    cache_miss = sum(cache_miss_reclaim(scored))
    dup_read   = sum(dup_read_reclaim(scored))
    downgrade  = sum(model_downgrade_reclaim(scored))
    return {
        "cache_miss":      round(cache_miss, 2),
        "dup_read":        round(dup_read, 2),
        "model_downgrade": round(downgrade, 2),
        "total":           round(cache_miss + dup_read + downgrade, 2),
    }


def enforce_cost_gate(scored: list[tuple[dict, dict]],
                      gate_tokens: int, gate_usd: float
                     ) -> tuple[str, list[dict]]:
    """Apply per-session Cost Gate. Returns (status, violations).

    status is one of: ``"ok"`` (no violations), ``"warn"`` (some violations),
    ``"bad"`` (gate exceeded by a large margin on at least one session — used
    by --json to set exit code 3).

    A session violates the gate if either ``input_tokens + cache_read_tokens``
    exceeds ``gate_tokens`` OR its USD cost exceeds ``gate_usd``. Each
    violation is a dict ``{session_id, total_input, cost, reason}``.
    """
    violations: list[dict] = []
    for s, _ in scored:
        total_input = s["input_tokens"] + s["cache_read_tokens"]
        cost = cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_read_tokens=s["cache_read_tokens"],
        )
        if total_input > gate_tokens:
            violations.append({
                "session_id": s["session_id"],
                "total_input": total_input,
                "cost": cost,
                "reason": f"input={total_input:,} > {gate_tokens:,}",
            })
        elif cost > gate_usd:
            violations.append({
                "session_id": s["session_id"],
                "total_input": total_input,
                "cost": cost,
                "reason": f"cost=${cost:.2f} > ${gate_usd:.2f}",
            })
    if not violations:
        return "ok", violations
    # "bad" if any single session blew past both thresholds OR the total
    # spend is 3x the USD gate — i.e., it's not just a noisy outlier.
    total_spend = sum(v["cost"] for v in violations)
    if any(v["total_input"] > 3 * gate_tokens or v["cost"] > 3 * gate_usd for v in violations):
        return "bad", violations
    if total_spend > 3 * gate_usd:
        return "bad", violations
    return "warn", violations


def cost_gate_stderr_lines(violations: list[dict]) -> list[str]:
    """Render one WARN line per violation (human-readable, stderr-only)."""
    return [
        f"WARN: session {v['session_id'][:8]} {v['reason']} (cost=${v['cost']:.2f})"
        for v in violations
    ]


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
    <div class="panel metric"><div class="label">Cache Hit Ratio</div><div class="value">{avg_cache_hit:.0%}</div><div class="delta">cache_read / total_input</div></div>
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


def _session_cost(s: dict) -> float:
    """USD cost for one aggregated session (same inputs as the dashboard panels)."""
    return cost_usd(
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
    f = session.get("first_ts")
    l = session.get("last_ts")
    if not f:
        return "—"
    fs = f.strftime("%Y-%m-%d %H:%M")
    ls = l.strftime("%H:%M") if l else ""
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


def render_dashboard(repo: str, days: int, sessions: list[dict],
                     scored: list[tuple[dict, dict]],
                     warnings_per_session: list[list["Warning"]],
                     estimated: dict[str, float] | float,
                     cost_gate: tuple[str, list[dict]] = ("ok", []),
                     all_sessions_in_window: list[dict] | None = None,
                     unknown_models: set[str] | None = None,
                     wt_meta: dict[str, dict] | None = None,
                     stale_cost: float = 0.0,
                     stale_pct: float = 0.0,
                     worktree_filter: str = "",
                     transcripts_dirname: str = "") -> str:
    """Compose the HTML dashboard. Inputs are pre-filtered to ``repo``+``days``.

    ``estimated`` may be a legacy single float (sum) or a dict from the new
    ``estimated_savings()`` with ``cache_miss / dup_read / model_downgrade / total``.
    ``cost_gate`` is ``(status, violations)`` from ``enforce_cost_gate()``.
    ``all_sessions_in_window`` is the unfiltered-by-repo set used for the
    per-repo panel — fixes the collapse-to-one-row bug.

    ``wt_meta`` is the per-worktree classification map (dirname → {state, ...})
    used to populate the State column on the worktree panel and to drive the
    stale-chip prefix on Sessions rows. ``stale_cost`` + ``stale_pct`` are
    pre-computed in ``main()`` and feed the 5th Overview tile.
    ``worktree_filter`` is echoed onto the subtitle when non-empty.
    """
    wt_meta = wt_meta or {}
    if isinstance(estimated, (int, float)):
        estimated = {
            "cache_miss": float(estimated), "dup_read": 0.0,
            "model_downgrade": 0.0, "total": float(estimated),
        }

    session_costs: list[float] = []
    for s, _ in scored:
        session_costs.append(cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        ))
    total_cost = sum(session_costs)
    total_tokens = sum(
        s["input_tokens"] + s["output_tokens"]
        + s["cache_write_tokens"] + s["cache_read_tokens"]
        for s, _ in scored
    )

    avg_score = mean(sc["total"] for _, sc in scored) if scored else 0.0
    avg_cache = mean(sc["cache"] for _, sc in scored) if scored else 0.0
    avg_density = mean(sc["density"] for _, sc in scored) if scored else 0.0
    avg_redundancy = mean(sc["redundancy"] for _, sc in scored) if scored else 0.0
    avg_economy = mean(sc["economy"] for _, sc in scored) if scored else 0.0
    avg_cache_hit = mean(sc["cache_hit_ratio"] for _, sc in scored) if scored else 0.0
    avg_grade = grade_for(avg_score)

    # Cost by repo — use the unfiltered-by-repo session set so the panel
    # is not a single self-row.
    repo_pool = all_sessions_in_window if all_sessions_in_window is not None else sessions
    repo_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in repo_pool:
        c = cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )
        repo_costs[s["repo"]][0] += 1
        repo_costs[s["repo"]][1] += c
    repo_total_for_share = sum(rc[1] for rc in repo_costs.values()) or 1.0
    repo_rows_html = "".join(
        f"<tr><td>{html.escape(rr)}</td><td style='text-align:right'>{int(repo_costs[rr][0])}</td>"
        f"<td style='text-align:right'>${repo_costs[rr][1]:.2f}</td>"
        f"<td><div class='bar'><span style='width:{(repo_costs[rr][1] / repo_total_for_share * 100):.1f}%'></span></div></td></tr>"
        for rr in sorted(repo_costs, key=lambda k: -repo_costs[k][1])
    )

    # Cost by branch — mirrors the repo panel, sourced from the unfiltered-by-repo
    # window so a --branch filter doesn't collapse this to one self-row.
    branch_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in repo_pool:
        c = cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )
        bkey = s.get("branch") or "(unknown)"
        branch_costs[bkey][0] += 1
        branch_costs[bkey][1] += c
    branch_total_for_share = sum(bc[1] for bc in branch_costs.values()) or 1.0
    branch_rows_html = "".join(
        f"<tr><td>{html.escape(b)}</td><td style='text-align:right'>{int(branch_costs[b][0])}</td>"
        f"<td style='text-align:right'>${branch_costs[b][1]:.2f}</td>"
        f"<td><div class='bar'><span style='width:{(branch_costs[b][1] / branch_total_for_share * 100):.1f}%'></span></div></td></tr>"
        for b in sorted(branch_costs, key=lambda k: -branch_costs[k][1])
    )

    # Cost by worktree — same source as the branch panel (unfiltered-by-repo
    # window) so a --worktree filter doesn't collapse it to one self-row.
    worktree_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s in repo_pool:
        c = cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )
        wkey = s.get("worktree") or "(unknown)"
        worktree_costs[wkey][0] += 1
        worktree_costs[wkey][1] += c
    worktree_total_for_share = sum(wc[1] for wc in worktree_costs.values()) or 1.0

    # Per-worktree state — derived from the per-session stamp first
    # (authoritative: each session carries its own worktree_state), with the
    # wt_meta map as a fallback for worktrees seen via cost but not in the
    # scored window (rare; defensive).
    per_wt_state: dict[str, str] = {}
    for s, _ in scored:
        wt = s.get("worktree") or "(unknown)"
        per_wt_state.setdefault(wt, s.get("worktree_state", "unknown"))

    def _worktree_state_pill(state: str) -> str:
        """State column pill — visually distinct from the warning chips.

        Fresh worktrees are "good" (neutral blue/info tone via ``pill-good``)
        because they are the user's most useful state, not a cleanup target.
        Merged + gone are cleanup candidates (warn / bad). Unknown is bad
        because the user can't tell what state the worktree is in.
        """
        cls = {
            "main": "pill-good",
            "live": "pill-good",
            "fresh": "pill-good",
            "merged": "pill-warn",
            "gone": "pill-bad",
            "unknown": "pill-bad",
        }.get(state, "pill-bad")
        return f"<span class='pill {cls}'>{html.escape(state)}</span>"

    def _state_for(wkey: str) -> str:
        return (
            per_wt_state.get(wkey)
            or (wt_meta.get(wkey) or {}).get("state", "unknown")
            or "unknown"
        )

    # Union of session-seen worktrees + every disk worktree (from wt_meta)
    # so no worktree dir is hidden just because no session landed in it
    # during the window. Zero-cost rows are how live/merged/gone worktrees
    # the user forgot about show up. (main) pinned to the top.
    all_wts = set(worktree_costs) | set(wt_meta)
    sorted_wts = sorted(
        all_wts,
        key=lambda k: -worktree_costs.get(k, [0, 0.0])[1],
    )
    if "(main)" in sorted_wts:
        sorted_wts.remove("(main)")
        sorted_wts = ["(main)"] + sorted_wts
    worktree_rows_html = "".join(
        f"<tr><td>{html.escape(w)}</td>"
        f"<td style='text-align:right'>{int(worktree_costs.get(w, [0, 0.0])[0])}</td>"
        f"<td style='text-align:right'>${worktree_costs.get(w, [0, 0.0])[1]:.2f}</td>"
        f"<td><div class='bar'><span style='width:{(worktree_costs.get(w, [0, 0.0])[1] / worktree_total_for_share * 100):.1f}%'></span></div></td>"
        f"<td>{_worktree_state_pill(_state_for(w))}</td></tr>"
        for w in sorted_wts
    )

    # Cost by tool (imputed — see evaluate_warnings comment for the heuristic)
    tool_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s, _ in scored:
        for name, n in s["tool_counts"].items():
            est = n * 2000 * pricing_for(s["model"])["in"] / 1_000_000
            tool_costs[name][0] += n
            tool_costs[name][1] += est
    total_tool_cost = sum(c[1] for c in tool_costs.values()) or 1.0
    sorted_tools = sorted(tool_costs.items(), key=lambda kv: -kv[1][1])
    tool_rows_html = "".join(
        f"<tr><td>{html.escape(name)}</td><td style='text-align:right'>{int(calls)}</td>"
        f"<td style='text-align:right'>${cost:.2f}</td>"
        f"<td><div class='bar'><span style='width:{(cost / total_tool_cost * 100):.1f}%'></span></div></td></tr>"
        for name, (calls, cost) in sorted_tools
    )
    read_warning_html = ""
    if sorted_tools and sorted_tools[0][0] == "Read":
        read_warning_html = (
            '<div class="warning warn" style="margin-top:12px">'
            '🚨 Read 툴이 툴 비용 1위입니다 — 대용량 파일 반복 읽기를 의심하세요.'
            '</div>'
        )

    # Cost by Model — group sessions by their dominant model id.
    model_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
    # accumulator: [sessions, total_tokens, cost]
    for s, c in zip(sessions, [cost_usd(
        s["model"],
        input_tokens=s["input_tokens"],
        output_tokens=s["output_tokens"],
        cache_write_5m_tokens=s.get("ephemeral_5m", 0),
        cache_write_1h_tokens=s.get("ephemeral_1h", 0),
        cache_write_tokens=s["cache_write_tokens"],
        cache_read_tokens=s["cache_read_tokens"],
    ) for s in sessions]):
        tokens = s["input_tokens"] + s["output_tokens"] + s["cache_write_tokens"] + s["cache_read_tokens"]
        model_costs[s["model"] or "(unknown)"][0] += 1
        model_costs[s["model"] or "(unknown)"][1] += tokens
        model_costs[s["model"] or "(unknown)"][2] += c
    model_total_for_share = sum(mc[2] for mc in model_costs.values()) or 1.0
    model_rows_html = "".join(
        f"<tr><td>{html.escape(m)}</td>"
        f"<td style='text-align:right'>{int(model_costs[m][0])}</td>"
        f"<td style='text-align:right'>{int(model_costs[m][1]):,}</td>"
        f"<td style='text-align:right'>${model_costs[m][2]:.2f}</td>"
        f"<td><div class='bar'><span style='width:{(model_costs[m][2] / model_total_for_share * 100):.1f}%'></span></div></td></tr>"
        for m in sorted(model_costs, key=lambda k: -model_costs[k][2])
    )
    unknown_model_html = ""
    if unknown_models:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in sorted(unknown_models))
        unknown_model_html = (
            f'<div class="warning warn" style="margin-top:12px">'
            f'⚠ Unknown model id(s) — falling back to Sonnet pricing:'
            f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>'
        )

    # Cache TTL mix panel
    ttl_read  = sum(s["cache_read_tokens"] for s, _ in scored)
    ttl_5m    = sum(s.get("ephemeral_5m", 0) for s, _ in scored)
    ttl_1h    = sum(s.get("ephemeral_1h", 0) for s, _ in scored)
    # Legacy bucket = cache_write tokens that the upstream provider did not
    # break down into 5m vs 1h. Priced at the 5m rate by cost_usd; shown
    # here so the dashboard doesn't silently swallow it.
    ttl_legacy = max(0, sum(
        s["cache_write_tokens"] - s.get("ephemeral_5m", 0) - s.get("ephemeral_1h", 0)
        for s, _ in scored
    ))
    ttl_miss  = sum(s["input_tokens"] for s, _ in scored)
    ttl_writes_total = ttl_5m + ttl_1h + ttl_legacy
    ttl_total = (ttl_read + ttl_writes_total + ttl_miss) or 1
    ttl_read_pct = ttl_read / ttl_total * 100
    ttl_5m_pct   = ttl_5m   / ttl_total * 100
    ttl_1h_pct   = ttl_1h   / ttl_total * 100
    ttl_legacy_pct = ttl_legacy / ttl_total * 100
    ttl_miss_pct = ttl_miss / ttl_total * 100

    # Three render states for the write-rows of the TTL mix panel:
    #   a) ttl_writes_total == 0             -> single annotation row
    #   b) ttl_5m == ttl_1h == 0, legacy > 0 -> single combined "TTL unspecified" bar
    #   c) any 5m/1h bucket populated        -> existing 4-bar layout
    if ttl_writes_total == 0:
        ttl_middle_html = (
            '<div class="ttl-name">cache_write</div>'
            '<div class="ttl-empty" '
            'style="background:var(--panel-2);border-radius:4px;'
            'padding:6px 10px;color:var(--muted);font-size:11px">'
            'no cache-write activity captured this period'
            '</div><div class="ttl-pct">—</div>'
        )
    elif ttl_5m == 0 and ttl_1h == 0:
        ttl_middle_html = (
            '<div class="ttl-name">cache_write (TTL unspecified, priced at 5m)</div>'
            f'<div class="bar writelegacy"><span style="width:{ttl_legacy_pct:.1f}%"></span></div>'
            f'<div class="ttl-pct">{ttl_legacy:,}</div>'
        )
    else:
        ttl_middle_html = (
            '<div class="ttl-name">write 5m TTL</div>'
            f'<div class="bar write5m"><span style="width:{ttl_5m_pct:.1f}%"></span></div>'
            f'<div class="ttl-pct">{ttl_5m:,}</div>'
            '<div class="ttl-name">write 1h TTL</div>'
            f'<div class="bar write1h"><span style="width:{ttl_1h_pct:.1f}%"></span></div>'
            f'<div class="ttl-pct">{ttl_1h:,}</div>'
        )

    # Cost Gate banner
    gate_status, gate_violations = cost_gate
    if not gate_violations:
        cost_gate_banner = (
            f'<div class="cost-gate {gate_status}">'
            f'<span class="label">Cost Gate:</span> all sessions within '
            f'tokens/cost thresholds.'
            f'</div>'
        )
    else:
        items = "".join(
            f"<li><code>{html.escape(v['session_id'][:8])}</code> — {html.escape(v['reason'])} "
            f"(cost=${v['cost']:.2f})</li>"
            for v in gate_violations
        )
        cost_gate_banner = (
            f'<div class="cost-gate {gate_status}">'
            f'<span class="label">Cost Gate: {gate_status.upper()}</span>'
            f'{len(gate_violations)} session(s) exceeded thresholds:'
            f'<ul>{items}</ul></div>'
        )

    # Session rows — split into Active (main/live/fresh) vs Inactive
    # (merged/gone, i.e. the branch's work is done) so stale work stops
    # crowding the same table as sessions still worth acting on. The
    # Worktree cell reuses _worktree_state_pill (already built for the
    # Cost by Worktree panel above) instead of a redundant boolean chip.
    def _session_rows_html(pairs: list[tuple[tuple[dict, dict], list["Warning"]]]) -> str:
        parts: list[str] = []
        for (s, sc), warns in pairs:
            cost = _session_cost(s)
            hit = sc["cache_hit_ratio"]
            started = s["first_ts"].strftime("%Y-%m-%d %H:%M") if s["first_ts"] else "—"
            score = sc["total"]
            grade = sc["grade"]
            pill_cls = "pill-good" if score >= 75 else ("pill-warn" if score >= 50 else "pill-bad")
            total_tools = sum(s["tool_counts"].values())
            warn_chips = " ".join(
                f"<span class='pill {'pill-bad' if w.level=='critical' else 'pill-warn'}'>{html.escape(w.code)}</span>"
                for w in warns
            ) or "<span class='muted'>—</span>"
            wt_state = s.get("worktree_state", "unknown")
            wt_cell = f"{html.escape(s.get('worktree') or '—')} {_worktree_state_pill(wt_state)}"
            parts.append(
                f"<tr><td><code>{html.escape(s['session_id'][:8])}</code></td>"
                f"<td>{html.escape(s.get('branch') or '—')}</td>"
                f"<td>{wt_cell}</td>"
                f"<td>{html.escape(s['model'] or '?')}</td>"
                f"<td class='muted'>{html.escape(started)}</td>"
                f"<td style='text-align:right'>{s['input_tokens']:,}</td>"
                f"<td style='text-align:right'>{s['output_tokens']:,}</td>"
                f"<td style='text-align:right'>{total_tools:,}</td>"
                f"<td style='text-align:right'>{hit:.0%}</td>"
                f"<td style='text-align:right'>${cost:.2f}</td>"
                f"<td style='text-align:right'><span class='pill {pill_cls}'>{score:.0f}</span>"
                f"<span class='grade grade-{grade}'>{grade}</span></td>"
                f"<td>{warn_chips}</td></tr>"
            )
        return "\n".join(parts) or "<tr><td colspan='12' class='muted'>No sessions.</td></tr>"

    active_pairs = [
        (sw, warns) for sw, warns in zip(scored, warnings_per_session)
        if sw[0].get("worktree_state") not in STALE_WORKTREE_STATES
    ]
    inactive_pairs = [
        (sw, warns) for sw, warns in zip(scored, warnings_per_session)
        if sw[0].get("worktree_state") in STALE_WORKTREE_STATES
    ]
    active_count = len(active_pairs)
    inactive_count = len(inactive_pairs)
    active_session_rows_html = _session_rows_html(active_pairs)
    inactive_session_rows_html = _session_rows_html(inactive_pairs)

    # Transcript Index — one row per worktree, linking to its sidecar index
    # page (which lazily lists that worktree's sessions). Mirrors the Cost by
    # Worktree ordering: (main) pinned first, then by descending cost. When
    # transcripts are disabled (--no-transcripts) the Open cell is inert.
    ti_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s, _ in scored:
        wt = s.get("worktree") or "(unknown)"
        ti_costs[wt][0] += 1
        ti_costs[wt][1] += _session_cost(s)
    ti_sorted = sorted(ti_costs, key=lambda k: -ti_costs[k][1])
    if "(main)" in ti_sorted:
        ti_sorted.remove("(main)")
        ti_sorted = ["(main)"] + ti_sorted
    ti_rows: list[str] = []
    for wt in ti_sorted:
        if transcripts_dirname:
            href = f"{transcripts_dirname}/{_safe_seg(wt)}/index.html"
            open_cell = f"<a href='{html.escape(href)}'>open →</a>"
        else:
            open_cell = "<span class='muted'>—</span>"
        ti_rows.append(
            f"<tr><td>{html.escape(wt)}</td>"
            f"<td style='text-align:right'>{int(ti_costs[wt][0])}</td>"
            f"<td style='text-align:right'>${ti_costs[wt][1]:.2f}</td>"
            f"<td>{open_cell}</td></tr>"
        )
    transcript_index_rows = "".join(ti_rows) or "<tr><td colspan='4' class='muted'>No sessions.</td></tr>"

    # Warnings list (deduped by code)
    seen_codes: set[str] = set()
    warn_blocks: list[str] = []
    for warns in warnings_per_session:
        for w in warns:
            if w.code in seen_codes:
                continue
            seen_codes.add(w.code)
            css = "warning" if w.level == "critical" else "warning warn"
            warn_blocks.append(f'<div class="{css}">{html.escape(w.message)}</div>')
    warnings_html = "\n".join(warn_blocks) or '<div class="muted">No anti-patterns detected.</div>'

    # ROI Actions ranked by $ save — one row per (session, warning) instance,
    # not a generic per-code paragraph. Each row names the exact session and
    # the concrete number/file/text that drove the estimate (w.evidence),
    # so "what do I actually do" is answerable without opening the session.
    # The full recommendation text rides along as a hover `title` (no JS
    # needed) for anyone who wants the longer explanation.
    roi_candidates: list[tuple[float, str, str, str, int]] = []   # (save, code, session_id, evidence, priority)
    for warns in warnings_per_session:
        for w in warns:
            if w.estimated_save_usd > 0:
                roi_candidates.append((w.estimated_save_usd, w.code, w.session_id, w.evidence, w.priority))
    roi_candidates.sort(key=lambda x: -x[0])
    if roi_candidates:
        roi_items = "".join(
            f"<li title='{html.escape(WARNING_RECOMMENDATIONS.get(code, ''))}'>"
            f"<span class='rank'>#{i+1}</span>"
            f"<span class='save'>${save:.2f}</span>"
            f"<span class='code'>{html.escape(code)}</span>"
            f"<span class='sid'><code>{html.escape(sid[:8]) if sid else '—'}</code></span>"
            f"<span class='evidence'>{html.escape(evidence) if evidence else '—'}</span>"
            f"<span class='muted'>(P{prio})</span></li>"
            for i, (save, code, sid, evidence, prio) in enumerate(roi_candidates[:20])
        )
    else:
        roi_items = '<li class="muted">No reclaimable savings detected — cache hit and tool usage are within targets.</li>'

    # Recommended Optimizations (do/don't) — show do for fired codes, dont for not-fired.
    fired_codes = seen_codes
    all_codes = list(WARNING_RECOMMENDATIONS.keys())
    opt_items: list[str] = []
    for code in all_codes:
        if code in fired_codes:
            opt_items.append(f'<li class="do">{html.escape(WARNING_RECOMMENDATIONS[code])}</li>')
        else:
            opt_items.append(f'<li class="dont muted-item">{html.escape(WARNING_DONT.get(code, ""))}</li>')
    optimize_items = "\n".join(opt_items)

    # Subtitle: existing fields plus optional worktree filter echo.
    subtitle_parts = [
        html.escape(repo),
        f"last {days} days",
        f"{len(scored)} sessions ({active_count} active · {inactive_count} inactive)",
        f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if worktree_filter:
        subtitle_parts.insert(1, f"worktree={html.escape(worktree_filter)}")
    subtitle = " · ".join(subtitle_parts)

    return HTML_TEMPLATE.format(
        repo=html.escape(repo),
        days=days,
        session_count=len(scored),
        repos_named=len({s["repo"] for s, _ in scored}),
        active_count=active_count,
        inactive_count=inactive_count,
        total_cost=total_cost,
        total_tokens=total_tokens,
        avg_score=avg_score,
        avg_grade=avg_grade,
        avg_cache=avg_cache,
        avg_density=avg_density,
        avg_redundancy=avg_redundancy,
        avg_economy=avg_economy,
        avg_cache_hit=avg_cache_hit,
        stale_cost=stale_cost,
        stale_pct=stale_pct,
        cost_gate_banner=cost_gate_banner,
        repo_rows=repo_rows_html,
        branch_rows=branch_rows_html,
        worktree_rows=worktree_rows_html,
        tool_rows=tool_rows_html,
        read_warning_html=read_warning_html,
        model_rows=model_rows_html,
        unknown_model_html=unknown_model_html,
        ttl_read_tokens=ttl_read,
        ttl_5m_tokens=ttl_5m,
        ttl_1h_tokens=ttl_1h,
        ttl_miss_tokens=ttl_miss,
        ttl_read_pct=ttl_read_pct,
        ttl_5m_pct=ttl_5m_pct,
        ttl_1h_pct=ttl_1h_pct,
        ttl_miss_pct=ttl_miss_pct,
        ttl_middle_html=ttl_middle_html,
        ttl_caveat=html.escape(CACHE_TTL_CAVEAT),
        active_session_rows=active_session_rows_html,
        inactive_session_rows=inactive_session_rows_html,
        transcript_index_rows=transcript_index_rows,
        warnings_html=warnings_html,
        estimated_total=estimated["total"],
        estimated_cache_miss=estimated["cache_miss"],
        estimated_dup_read=estimated["dup_read"],
        estimated_downgrade=estimated["model_downgrade"],
        roi_items=roi_items,
        optimize_items=optimize_items,
        css=CSS,
        subtitle=subtitle,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Token efficiency analyzer + HTML dashboard.")
    parser.add_argument("--repo", required=True, help="Repository name to filter (matches basename of cwd).")
    parser.add_argument("--days", type=int, default=30, help="Look-back window in days (default 30).")
    parser.add_argument("--logs-dir", default="logs", help="Logs root directory (default: ./logs).")
    parser.add_argument("--include-worktree-logs", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Auto-discover logs from .claude/worktrees/*/logs/ (default: True). "
                             "Pass --no-include-worktree-logs to disable.")
    parser.add_argument("--out", default=None, help="Output HTML path (default: token-dashboard-<repo>-<days>d.html).")
    parser.add_argument("--cost-gate-tokens", type=int, default=DEFAULT_COST_GATE_TOKENS,
                        help=f"Per-session input+cache_read gate (default {DEFAULT_COST_GATE_TOKENS:,}).")
    parser.add_argument("--cost-gate-usd", type=float, default=DEFAULT_COST_GATE_USD,
                        help=f"Per-session USD gate (default ${DEFAULT_COST_GATE_USD:.2f}).")
    parser.add_argument("--pricing-override", default=None,
                        help="Optional JSON file overriding the PRICING dict (see PRICING docstring for shape).")
    parser.add_argument("--branch", default="",
                        help="Filter to a single branch (case-insensitive substring match on gitBranch). Default: all branches.")
    parser.add_argument("--worktree", default="",
                        help="Filter to a single worktree (case-insensitive substring on derived worktree name). Default: all worktrees.")
    parser.add_argument("--json", action="store_true",
                        help="Emit a machine-readable JSON summary to stdout (skips HTML write). Exit 3 on cost_gate==bad.")
    parser.add_argument("--transcripts", action=argparse.BooleanOptionalAction, default=True,
                        help="Write per-session full-transcript sidecar pages under <out>.assets/ and link "
                             "them from the Transcript Index (default: True). Pass --no-transcripts for an "
                             "index-only run.")
    args = parser.parse_args(argv)

    # Apply pricing override before any pricing call.
    load_pricing_override(Path(args.pricing_override) if args.pricing_override else None)

    logs_dir = Path(args.logs_dir).resolve()
    repo_root = Path.cwd().resolve() if args.include_worktree_logs else None
    files = discover_logs(logs_dir, repo_root=repo_root)
    # Dual-write (#173) places the same sessionId in two files; dedup to
    # one snapshot per sessionId so cost and branch attribution are not
    # double-counted or skewed by the stale main-side copy.
    files = _dedupe_by_session(files)
    if not files:
        print(f"[error] No JSONL logs found under {logs_dir}/(claude-code|codex)/", file=sys.stderr)
        return 2

    unknown_models: set[str] = set()

    # Worktree classification (per project `.claude/worktrees/*/` dir, vs
    # `git worktree list` + ancestor-of-origin/main check). Skipped when
    # --no-include-worktree-logs is in effect to avoid surprising git walks.
    wt_meta: dict[str, dict] = (
        classify_all_worktrees(repo_root) if repo_root is not None else {}
    )

    sessions: list[dict] = []
    for p in files:
        s = aggregate_session(p)
        if s is not None:
            sessions.append(s)

    # Stamp worktree_state on each session so the dashboard can mark stale
    # rows and the summary line can quote the stale-cost total. Always safe
    # — falls back to "(main)" / "(unknown)" sentinels.
    for s in sessions:
        wt = s.get("worktree") or ""
        if wt == "(main)":
            s["worktree_state"] = "main"
        elif wt == "(unknown)":
            s["worktree_state"] = "unknown"
        else:
            entry = wt_meta.get(wt)
            s["worktree_state"] = entry["state"] if entry else "unknown"

    # Time-window only (no repo filter) — feeds the per-repo panel so it
    # shows the full distribution, not a single self-row.
    windowed = filter_sessions(sessions, "", args.days, worktree=args.worktree)
    selected = filter_sessions(sessions, args.repo, args.days, args.branch, args.worktree)
    if not selected:
        warn_target = f"repo='{args.repo}'"
        if args.branch:
            warn_target += f" branch='{args.branch}'"
        if args.worktree:
            warn_target += f" worktree='{args.worktree}'"
        print(f"[warn] No sessions matched {warn_target} within {args.days} days.", file=sys.stderr)

    scored: list[tuple[dict, dict]] = [(s, score_session(s)) for s in selected]
    reclaim_cache = cache_miss_reclaim(scored)
    reclaim_dup   = dup_read_reclaim(scored)
    reclaim_dn    = model_downgrade_reclaim(scored)
    warnings_per_session = [
        evaluate_warnings(s, sc, rc, rd, rdn)
        for (s, sc), rc, rd, rdn in zip(scored, reclaim_cache, reclaim_dup, reclaim_dn)
    ]
    estimated = estimated_savings(scored)

    gate_status, gate_violations = enforce_cost_gate(scored, args.cost_gate_tokens, args.cost_gate_usd)

    # Detect unknown model ids (collect during scoring). Re-walk once.
    for s in selected:
        pricing_for(s["model"], _unknown_models=unknown_models)

    # Warn about unknown models on stderr (not stdout — stdout is the [ok] contract).
    for m in sorted(unknown_models):
        print(f"WARN: unknown model '{m}' — using sonnet fallback pricing", file=sys.stderr)

    # Warn about worktrees whose classification fell back to "unknown"
    # (typically: no `origin/main` ref, or porcelain git call timed out).
    for wt_name, meta in sorted(wt_meta.items()):
        if meta.get("state") == "unknown" and wt_name != "(main)":
            print(
                f"WARN: worktree '{wt_name}' classification failed "
                f"(branch tip={meta.get('branch_tip','') or '?'}); "
                f"check that origin/main is fetchable from this repo.",
                file=sys.stderr,
            )

    # Cost Gate WARN lines (stderr only, after HTML is written so stdout order is stable).
    for line in cost_gate_stderr_lines(gate_violations):
        print(line, file=sys.stderr)

    total_cost = sum(
        cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )
        for s in selected
    )

    # Stale-cost aggregate — sessions whose worktree was merged into
    # origin/main or whose dir was already removed (gauge the spend left
    # behind by stale-but-still-on-disk worktrees).
    stale_cost = sum(
        cost_usd(
            s["model"],
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_write_5m_tokens=s.get("ephemeral_5m", 0),
            cache_write_1h_tokens=s.get("ephemeral_1h", 0),
            cache_write_tokens=s["cache_write_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
        )
        for s in selected
        if s.get("worktree_state") in STALE_WORKTREE_STATES
    )
    stale_pct = (stale_cost / total_cost) if total_cost > 0 else 0.0

    # Active/Inactive session split — mirrors the dashboard's two Sessions
    # tables. "Inactive" = worktree already merged into origin/main or gone
    # from disk (the branch's work is done); everything else is "Active".
    inactive_count = sum(1 for s in selected if s.get("worktree_state") in STALE_WORKTREE_STATES)
    active_count = len(selected) - inactive_count

    if args.json:
        out = {
            "repo": args.repo,
            "branch": args.branch,
            "branch_filter_active": bool(args.branch),
            "worktree": args.worktree,
            "worktree_filter_active": bool(args.worktree),
            "days": args.days,
            "files_scanned": len(files),
            "sessions": len(selected),
            "active_sessions": active_count,
            "inactive_sessions": inactive_count,
            "total_cost_usd": round(total_cost, 4),
            "stale_cost_usd": round(stale_cost, 4),
            "stale_pct": round(stale_pct, 4),
            "estimated_savings_usd": estimated,
            "cost_gate": {
                "status": gate_status,
                "tokens_threshold": args.cost_gate_tokens,
                "usd_threshold": args.cost_gate_usd,
                "violations": [
                    {k: (round(v[k], 4) if isinstance(v[k], float) else v[k]) for k in v}
                    for v in gate_violations
                ],
            },
            "warnings": [
                {
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
                }
                for (s, _), warns in zip(scored, warnings_per_session)
                for w in warns
            ],
            "unknown_models": sorted(unknown_models),
            "worktrees": _aggregate_worktree_rows(selected, wt_meta),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        if gate_status == "bad":
            return 3
        return 0

    out_path = Path(args.out) if args.out else Path(f"token-dashboard-{args.repo}-{args.days}d.html")
    transcripts_dirname = (out_path.stem + ".assets") if args.transcripts else ""

    html_out = render_dashboard(
        repo=args.repo,
        days=args.days,
        sessions=selected,
        scored=scored,
        warnings_per_session=warnings_per_session,
        estimated=estimated,
        cost_gate=(gate_status, gate_violations),
        all_sessions_in_window=windowed,
        unknown_models=unknown_models,
        wt_meta=wt_meta,
        stale_cost=stale_cost,
        stale_pct=stale_pct,
        worktree_filter=args.worktree,
        transcripts_dirname=transcripts_dirname,
    )

    out_path.write_text(html_out, encoding="utf-8")

    # Transcript sidecars — one dir per worktree, one page per session, plus a
    # per-worktree index. Written after the dashboard so stdout order is stable.
    # Navigation is <a href> only, so nothing is loaded until the user clicks.
    transcripts_written = 0
    if args.transcripts:
        assets_dir = out_path.with_name(out_path.stem + ".assets")
        dash_href = f"../../{out_path.name}"
        sessions_by_wt: dict[str, list[dict]] = defaultdict(list)
        for s in selected:
            sessions_by_wt[s.get("worktree") or "(unknown)"].append(s)
        for wt, wt_sessions in sessions_by_wt.items():
            wt_dir = assets_dir / _safe_seg(wt)
            wt_dir.mkdir(parents=True, exist_ok=True)
            (wt_dir / "index.html").write_text(
                render_worktree_index(wt, wt_sessions, main_href=dash_href),
                encoding="utf-8",
            )
            for s in wt_sessions:
                (wt_dir / _sid_file(s["session_id"])).write_text(
                    render_transcript_page(
                        Path(s["log_path"]), s, main_href=dash_href, wt_href="index.html",
                    ),
                    encoding="utf-8",
                )
                transcripts_written += 1

    # Console summary (stdout — Iron Law contract).
    print(f"[ok] sessions={len(selected)}  files_scanned={len(files)}  "
          f"total_cost=${total_cost:.2f}  estimated_savings=${estimated['total']:.2f}  "
          f"stale_cost=${stale_cost:.2f}  transcripts={transcripts_written}")
    print(f"[ok] dashboard -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())