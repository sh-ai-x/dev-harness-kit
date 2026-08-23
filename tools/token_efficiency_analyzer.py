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
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Allow `import llm_pricing` — tools/ lives next to lib/ but stdlib does
# not auto-add parent dirs to sys.path. The shared loader reads
# docs/llm-info/<provider>.json so this analyzer and lib/cost_gate.py
# stay in sync without re-typing numbers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import llm_pricing  # noqa: E402 — shared SSOT pricing loader (see rules/token-pricing.md)

# `.worktrees/` is client-neutral. Keep legacy roots discoverable so older
# Claude/Codex sessions remain visible after the migration.
WORKTREE_ROOT_NAMES = (".worktrees", ".claude/worktrees", ".codex/worktrees")


def _repository_label(repo_root: Path) -> str:
    """Match save_log.py's safe repository segment for external telemetry."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_root.name).strip("-")
    return (label or "detached")[:120]


def _external_logs_dir(repo_root: Path | None) -> Path | None:
    configured = os.environ.get("AGENT_LOG_ROOT", "").strip()
    if not configured or repo_root is None:
        return None
    root = Path(configured).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    return root / _repository_label(repo_root)


#: Hard cap on how deep ``_walk_all_worktree_logs`` recurses into nested
#: worktree roots. The production case is a single layer (sessions captured
#: from inside any worktree-roots worktree). Depth 2 covers the test
#: fixture that adds a worktree-of-a-worktree under ``.claude/worktrees/wt-x/.claude/worktrees/wt-y``.
#: Going deeper than 2 on slow CI runners costs minutes per scanner.
WORKTREE_LOG_WALK_DEPTH = 2


def _worktree_marker(parts: tuple[str, ...]) -> tuple[str, int] | None:
    for i, part in enumerate(parts):
        if part == ".worktrees" and i + 1 < len(parts):
            return parts[i + 1], i
        if part in {".claude", ".codex"} and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return parts[i + 2], i
    return None


# ---------------------------------------------------------------------------
# Pricing model (USD per 1M tokens).
#
# As of 2026-07-17 the inline PRICING dict has been replaced by a single
# shared loader: ``lib.llm_pricing``. That module reads
# ``docs/llm-info/<provider>.json`` (the SSOT refreshed via
# ``/dev-kit:llm-refresh``) so that ``tools/token_efficiency_analyzer.py``
# and ``lib/cost_gate.py`` cannot drift independently. The inline
# fallback below is kept for installs where docs/llm-info/ does not yet
# exist (partial / strict clones) — new code MUST go through
# ``lib.llm_pricing`` (see rules/token-pricing.md for the citation rule).
#
# Source-of-truth URLs (re-verify every release):
#   * Anthropic  https://platform.claude.com/docs/en/about-claude/pricing
#     Prompt-cache multipliers are 5m write=1.25x, 1h write=2.0x,
#     cache read=0.10x; documented universal across the Claude family.
#   * OpenAI     https://developers.openai.com/api/docs/pricing
#     OpenAI has a single cache-read discount (~50% of base input) and no
#     separate TTL for cache writes; both cache_write_5m and
#     cache_write_1h mirror base input pricing.
#   * MiniMax    https://platform.minimaxi.com/docs/guides/pricing-paygo.md
#     One cache-write rate (single TTL); mirror that into both buckets.
#   * DeepSeek   https://api-docs.deepseek.com/quick_start/pricing
#     Dedicated cache-hit rate per model; read directly from the JSON.
#
# Substring matcher in ``pricing_for()`` resolves any variant. The order
# is longest-prefix-first so ``gpt-5.6-sol`` matches BEFORE ``gpt-5``
# (otherwise gpt-5 silently steals 5.6-* ids at the cheaper legacy rate —
# the lesson from `rules/token-pricing.md: lessons we already paid for`).
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, float]] = dict(llm_pricing.LEGACY_FALLBACK)
"""Module-level PRICING snapshot.

Initially populated from the legacy fallback so imports during tests that
do not touch the filesystem still work. On first call to ``pricing_for``,
the loader is invoked and any JSON-loaded rows are overlaid on top of the
legacy rows (so the matched key wins). A subsequent ``--pricing-override``
file is overlaid on top of BOTH layers.
"""


def _reload_pricing_from_ssot() -> None:
    """Refresh the module-level PRICING from docs/llm-info/*.json.

    Idempotent: drops any previous JSON-loaded rows, then re-adds them.
    Tests call this to assert the SSOT is wired correctly.
    """
    json_pricing, _ = llm_pricing.load_pricing()
    # Reset to legacy fallback, then overlay JSON rows on top.
    PRICING.clear()
    PRICING.update(llm_pricing.LEGACY_FALLBACK)
    PRICING.update(json_pricing)
    llm_pricing.clear_cache()


_reload_pricing_from_ssot()


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

#: Worktree mtime freshness threshold (seconds) for the ``fresh`` state.
#: A worktree whose HEAD matches ``origin/main`` AND whose directory mtime
#: is at most this many seconds old is classified as ``fresh``. Older
#: worktrees with the same HEAD are classified as ``merged``. The value
#: matches the FRESH_WORKTREE_MAX_AGE_SECONDS documented in earlier
#: analyzer revisions.
WORKTREE_FRESH_MAX_AGE_SECONDS = 3600

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

    If the override key matches a JSON-loaded row (e.g.
    ``claude-opus-4-8``), the CLI value wins so the operator can fix a
    single bad number without editing docs/llm-info/.
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


def reload_pricing_from_ssot() -> None:
    """Re-read docs/llm-info/*.json into the module-level PRICING.

    Called by the dashboard before each report run so a pricing refresh
    via ``/dev-kit:llm-refresh`` is visible without a server restart.
    """
    _reload_pricing_from_ssot()


def pricing_for(model_id: str, *,
                _unknown_models: set[str] | None = None) -> dict[str, float]:
    """Pick the pricing row whose key appears in the model id (case-insensitive).

    Longest-prefix-first substring match against the merged PRICING
    (loaded from docs/llm-info/*.json with legacy fallback rows
    underneath). JSON keys ``claude-opus-4-8`` / ``gpt-5-5-pro`` etc.
    always win over legacy tier names because sorting by length puts
    the most specific keys first.

    If ``_unknown_models`` is provided, ids that match no tier are added to
    the set so the caller can warn on stderr.
    """
    if not model_id:
        return PRICING[DEFAULT_PRICING_KEY]
    mid = model_id.lower()

    def _norm(s: str) -> str:
        return s.replace("-", "").replace(".", "").replace("_", "")

    norm_mid = _norm(mid)
    if mid in PRICING:
        return PRICING[mid]
    # Longest-prefix-first substring on the normalized key.
    for key in sorted(PRICING.keys(), key=len, reverse=True):
        if key and _norm(key) in norm_mid:
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


def session_cost(s: dict, *, model: str | None = None) -> float:
    """Cost of a session dict in USD. Optional `model` overrides s['model'].

    Pulls input/output/cache_read/cache_write_5m/cache_write_1h from the
    session dict (with .get fallback to 0 for the cache buckets which are
    not always present). Centralizes the kwargs shape so the dashboard's
    10+ cost_usd call sites stay in lockstep when a new token bucket lands.
    """
    return cost_usd(
        model if model is not None else s["model"],
        input_tokens=s["input_tokens"],
        output_tokens=s["output_tokens"],
        cache_write_5m_tokens=s.get("ephemeral_5m", 0),
        cache_write_1h_tokens=s.get("ephemeral_1h", 0),
        cache_write_tokens=s.get("cache_write_tokens", 0),
        cache_read_tokens=s["cache_read_tokens"],
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

    When ``repo_root`` is provided, also walk every sibling worktree at the
    canonical or legacy worktree roots so sessions run in any worktree
    are visible from a single ``/dev-kit:token-analyzer`` invocation in the
    main checkout (worktree logs are gitignored and live in separate dirs).
    """
    out = _discover_one_logs_dir(logs_dir)
    if repo_root is not None:
        external_dir = _external_logs_dir(repo_root)
        if external_dir is not None and external_dir != logs_dir:
            out.extend(_discover_one_logs_dir(external_dir))
        for root_name in WORKTREE_ROOT_NAMES:
            wt_root = repo_root / root_name
            if wt_root.exists():
                for sub_wt in sorted(wt_root.iterdir()):
                    out.extend(_walk_all_worktree_logs(sub_wt))
    return out


def _walk_all_worktree_logs(wt_root: Path, _seen: set | None = None,
                              _depth: int = 0) -> list:
    """Walk ``<wt_root>/logs/`` and recurse into any nested worktree roots.

    Sessions captured from inside a worktree-created-from-a-worktree
    (nested layout like ``.claude/worktrees/A/.claude/worktrees/B/``) are
    still real sessions and must reach the dashboard.

    Two safety bounds keep this fast on slow shared CI filesystems:

    1. **Symlink cycles** — a ``_seen`` set of resolved paths stops the
       walk from looping when one worktree is symlinked from inside another.
    2. **Depth cap** — ``WORKTREE_LOG_WALK_DEPTH`` (default 2) bounds the
       recursion depth. The production case is a single ``.claude/worktrees/<wt>``
       layer; nesting deeper than that is rare and adds seconds-to-minutes of
       ``os.scandir`` calls on shared CI runners where each ``iterdir()`` is a
       slow syscall. Tests live at depth 2 still works; deeper layers are
       truncated and any capture that lives deeper would have to be moved
       to a top-level worktree directory (a separate refactor).
    """
    seen = _seen if _seen is not None else set()
    real = wt_root.resolve()
    if real in seen or not wt_root.exists():
        return []
    seen.add(real)
    out: list = _discover_one_logs_dir(wt_root / "logs")
    if _depth >= WORKTREE_LOG_WALK_DEPTH:
        return out
    for root_name in WORKTREE_ROOT_NAMES:
        nested = wt_root / root_name
        if nested.exists():
            for sub in sorted(nested.iterdir()):
                out.extend(_walk_all_worktree_logs(sub, seen, _depth + 1))
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

    # Single-pass scan: cache per-path stats so each JSONL is read at most
    # once. Returns (assistants_count, is_worktree_side, has_sessionId,
    # first_sessionId_or_None).
    stats_cache: dict[int, tuple[int, int, bool, str | None]] = {}

    def _scan(path: Path) -> tuple[int, int, bool, str | None]:
        cached = stats_cache.get(id(path))
        if cached is not None:
            return cached
        assistants = 0
        has_sid = False
        sid: str | None = None
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"type":"assistant"' in line:
                    assistants += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                line_sid = obj.get("sessionId") or obj.get("session_id")
                if line_sid:
                    has_sid = True
                    if sid is None:
                        sid = line_sid
        except OSError:
            pass
        result = (assistants, 1 if _worktree_marker(path.parts) else 0, has_sid, sid)
        stats_cache[id(path)] = result
        return result

    chosen: dict[str, Path] = {}
    for p in file_paths:
        _, _, has_sid, sid = _scan(p)
        if not has_sid or sid is None:
            continue
        cur = chosen.get(sid)
        if cur is None or _scan(p)[:2] > _scan(cur)[:2]:
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
        _, _, has_sid, _ = _scan(p)
        if not has_sid:
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
    """Best-effort ISO-8601 parser; returns None on failure.

    Normalizes a naive result (no 'Z' suffix, no UTC offset) to UTC-aware
    so it stays comparable to the tz-aware cutoff used by
    ``filter_sessions`` instead of raising ``TypeError`` on comparison.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _codex_nested_field(record: dict, field: str):
    """Read an explicit Codex field from a record or nested payload object."""
    value = record.get(field)
    if value is not None:
        return value
    payload = record.get("payload")
    while isinstance(payload, dict):
        value = payload.get(field)
        if value is not None:
            return value
        payload = payload.get("payload")
    return None


def repo_from_cwd(cwd: str | None) -> str:
    """Derive the project root label from ``cwd``.

    For a session running inside a worktree (``.worktrees/<name>/`` or a legacy root)
    the project's logical name is the segment immediately above that
    marker, not the worktree dir itself. Walking up lets a single
    ``--repo <project>`` invocation surface sessions from every checkout
    instead of only the main one.
    """
    if not cwd:
        return ""
    parts = Path(cwd).parts
    marker = _worktree_marker(parts)
    if marker and marker[1] >= 1:
        return parts[marker[1] - 1]
    return Path(cwd).name


def worktree_from_cwd(cwd: str | None) -> str:
    """Derive the git worktree dir name from ``cwd``.

    A worktree in this repo lives under ``<repo>/.worktrees/<name>/``
    (project convention enforced by ``.claude/rules/git-workflow.md``).
    Returns ``(main)`` when ``cwd`` is the main checkout, the worktree
    basename when ``cwd`` sits under a canonical or legacy worktree root, and
    ``(unknown)`` when ``cwd`` is missing. The literal bucket names keep
    the Cost by Worktree panel populated even when only the main checkout
    has been used.
    """
    if not cwd:
        return "(unknown)"
    parts = Path(cwd).parts
    marker = _worktree_marker(parts)
    if marker:
        return marker[0]
    return "(main)"


def worktree_from_path(path: Path | str | None) -> str:
    """Derive the git worktree dir name from a JSONL file path.

    Returns the basename of the worktree when ``path`` sits under a canonical
    or legacy worktree root; otherwise ``(main)``.

    Path-based resolution is authoritative because the ``cwd`` recorded
    in a session transcript often points at the parent checkout, not the
    worktree the session actually ran in. When the JSONL is captured from
    inside a worktree dir but ``cwd`` says main, only the file path knows
    the truth — the session belongs to that worktree's bucket.
    """
    if not path:
        return "(main)"
    metadata_path = Path(path).with_suffix(".meta.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        metadata = None
    if isinstance(metadata, dict) and metadata.get("worktree"):
        return Path(str(metadata["worktree"])).name or "(main)"
    parts = Path(path).parts
    marker = _worktree_marker(parts)
    if marker:
        return marker[0]
    return "(main)"


def _branch_name_for_porcelain_path(porcelain: str, target: Path) -> str:
    """Return the ``refs/heads/<name>`` of the worktree whose
    ``worktree <path>`` line matches ``target``.

    ``git worktree list --porcelain`` emits one blank-line-separated
    record per worktree; each record begins with ``worktree <abs path>``
    and contains either ``branch refs/heads/<name>`` or ``detached``.
    Scanning the whole output for the first ``branch `` line returns
    the main checkout's branch for every entry (Issue #494, PR review).
    Walk block-by-block and return only the branch of the block whose
    ``worktree`` line matches ``target``.
    """
    target_str = str(target)
    current_path: str | None = None
    current_branch: str | None = None
    for line in (porcelain or "").splitlines():
        if not line:
            # Block boundary — flush whatever we accumulated for the
            # current block before advancing.
            if current_path == target_str and current_branch:
                return current_branch
            current_path = None
            current_branch = None
            continue
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ").strip()
        elif line.startswith("branch "):
            current_branch = line.removeprefix("branch refs/heads/").strip()
    # Final block (porcelain may omit the trailing blank line).
    if current_path == target_str and current_branch:
        return current_branch
    return ""


def probe_working_tree_clean(
    wt_path: Path,
    *,
    git_runner=subprocess.run,
    timeout: int = 5,
) -> dict:
    """Inspect the worktree's working tree for the
    uncommitted-change safety check.

    Reviewer finding on PR #494 (🟠 major): ``classify_worktree_dir``
    never inspects working-tree status, so a ``merged``/``fresh``
    worktree with active local edits is misclassified relative to the
    agent's hard "uncommitted work is always needs-human-check" rule.
    This helper is opt-in — callers (e.g. worktree-janitor's
    orchestrator) pre-flight each batch before dispatch so the agent's
    hard constraint has the data it needs.

    Returns:
      - ``working_tree_clean`` (bool | None): True iff
        ``git status --porcelain`` is empty; None when the probe
        failed (worktree gone, no HEAD, timeout).
      - ``uncommitted_count`` (int): number of porcelain-status lines
        (excluding untracked ``?? `` entries).
      - ``untracked_count`` (int): number of ``?? `` porcelain lines.
      - ``porcelain`` (str): raw output capped at 4000 chars to bound
        the response if a hostile tree suddenly has tens of thousands
        of untracked files.

    Key name is ``working_tree_clean`` (NOT just ``clean``) so the
    orchestrator that hands this dict to the ``worktree-janitor``
    subagent can merge ``classify_worktree_dir(...)`` output and
    this helper's output into one ``context`` payload without a
    remap step. PR #494 review M-1 (major): the agent spec reads
    ``context.working_tree_clean`` and would ``KeyError`` on a
    naive merge.

    Never raises — every git call is wrapped and falls back to
    ``{working_tree_clean: None}`` on error.
    """
    empty = {
        "working_tree_clean": None,
        "uncommitted_count": 0,
        "untracked_count": 0,
        "porcelain": "",
    }
    try:
        proc = git_runner(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return dict(empty)
    if proc.returncode != 0:
        return dict(empty)
    raw = proc.stdout or ""
    porcelain = raw[:4000]
    lines = [ln for ln in raw.splitlines() if ln]
    untracked = sum(1 for ln in lines if ln.startswith("?? "))
    uncommitted = len(lines) - untracked
    return {
        "working_tree_clean": uncommitted == 0 and untracked == 0,
        "uncommitted_count": uncommitted,
        "untracked_count": untracked,
        "porcelain": porcelain,
    }


def _run_probe(args, git_runner, timeout):
    """Run a single git probe and swallow the failure modes that should
    fall back to ``state="unknown"`` for one worktree.

    The dashboard never wants a single slow / broken dir to crash the
    whole run, so ``subprocess.TimeoutExpired``, ``CalledProcessError``,
    and ``OSError`` (e.g. a deleted worktree dir between the iterdir
    and the probe) all collapse to ``None``. Anything else — including
    a non-zero ``returncode`` — is returned verbatim so the existing
    branch logic can keep treating ``returncode != 0`` as a probe
    failure (e.g. missing ``origin/main``).
    """
    try:
        return git_runner(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
        return None


def classify_worktree_dir(
    wt_path: Path,
    repo_root: Path,
    *,
    git_runner=subprocess.run,
    timeout: int = 5,
    precomputed_porcelain: tuple[str, bool] | None = None,
    precomputed_origin_main: str | None = None,
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

    Probes 1 and 4 are repo-wide (the porcelain result is the same for
    every worktree, and ``origin/main`` is a single SHA). Callers that
    iterate many worktrees — i.e. ``classify_all_worktrees`` — should
    hoist those probes and pass them in via ``precomputed_porcelain`` /
    ``precomputed_origin_main`` so they only run once per dashboard
    rather than once per dir. Direct callers (single-dir, tests) can
    leave them ``None`` and the function will run them itself.

    ``precomputed_porcelain`` is a ``(stdout, is_ok)`` tuple where
    ``is_ok`` is ``False`` when the upstream ``git worktree list`` call
    itself failed (treat as "dir not listed"). ``precomputed_origin_main``
    is the raw SHA string; pass ``""`` when the upstream call failed.

    Returned dict keys:

    - ``state``                   one of the 5 state strings above.
    - ``worktree_listed``         True iff the dir appears in
                                  ``git worktree list`` (i.e. still registered).
    - ``branch_merged_into_main`` True iff ``log origin/main..HEAD`` is empty.
    - ``is_fresh``                True iff branch is merged AND the dir mtime
                                  is within ``FRESH_WORKTREE_MAX_AGE_SECONDS``.
    - ``branch_tip``              short HEAD SHA (empty when HEAD is unreadable).
    - ``branch_name``             ``<type>/<slug>`` of the branch (empty when
                                  the worktree is not in ``git worktree list``).

    The function never raises — every probe is wrapped so a missing
    ``origin/main``, a timed-out ``git worktree list``, a deleted
    branch, or any other subprocess failure falls back to
    ``state="unknown"`` instead of crashing the dashboard run.
    """
    # Issue #310: every probe below is REAL — was previously short-circuited
    # to ``state="live"`` because the cumulative subprocess spawn cost on
    # slow shared CI runners was blowing past the 30-second test budget.
    # Tests pin each branch with a fake ``git_runner`` (see
    # ``test_classify_worktree_dir_real_probes_for_each_state``).
    # Issue #timeout-sweep: probes are now batched when this function is
    # driven by ``classify_all_worktrees`` (see
    # ``test_classify_all_worktrees_batches_shared_probes``) and every
    # remaining probe is wrapped in ``_run_probe`` so a single slow dir
    # can no longer crash the whole dashboard (see
    # ``test_classify_worktree_dir_swallows_timeout``).

    # Probe 1: is the dir still registered as a git worktree?
    # Hoisted in classify_all_worktrees — only re-run when called directly.
    if precomputed_porcelain is not None:
        porcelain_stdout, porcelain_ok = precomputed_porcelain
    else:
        porcelain_proc = _run_probe(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            git_runner, timeout,
        )
        if porcelain_proc is None:
            porcelain_stdout, porcelain_ok = "", False
        else:
            porcelain_stdout = porcelain_proc.stdout or ""
            porcelain_ok = porcelain_proc.returncode == 0
    is_listed = porcelain_ok and str(wt_path) in porcelain_stdout

    # Probe 2: branch tip short SHA (used for the ``branch_tip`` field).
    tip_proc = _run_probe(
        ["git", "-C", str(wt_path), "rev-parse", "--short", "HEAD"],
        git_runner, timeout,
    )
    branch_tip = (tip_proc.stdout or "").strip() if tip_proc is not None and tip_proc.returncode == 0 else ""

    # Probe 3: full HEAD SHA — compared against origin/main for fresh detect.
    head_proc = _run_probe(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
        git_runner, timeout,
    )
    head_full = (head_proc.stdout or "").strip() if head_proc is not None and head_proc.returncode == 0 else ""

    # Probe 4: origin/main SHA — used for fresh detect AND merged detect.
    # Hoisted in classify_all_worktrees — only re-run when called directly
    # (or when the upstream call failed, signalled by an empty string).
    if precomputed_origin_main is not None:
        main_full = precomputed_origin_main
        main_ok = bool(precomputed_origin_main)
    else:
        main_proc = _run_probe(
            ["git", "-C", str(repo_root), "rev-parse", "origin/main"],
            git_runner, timeout,
        )
        if main_proc is None or main_proc.returncode != 0:
            # Cannot compare without origin/main — surface "unknown" so the
            # dashboard can warn on stderr instead of silently mis-bucketing.
            return {
                "state": "unknown",
                "worktree_listed": is_listed,
                "branch_merged_into_main": False,
                "is_fresh": False,
                "branch_tip": branch_tip,
                "branch_name": "",
            }
        main_full = (main_proc.stdout or "").strip()
        main_ok = True
    if not main_ok:
        return {
            "state": "unknown",
            "worktree_listed": is_listed,
            "branch_merged_into_main": False,
            "is_fresh": False,
            "branch_tip": branch_tip,
            "branch_name": "",
        }

    # Probe 5: commits on the branch not yet in origin/main.
    log_proc = _run_probe(
        ["git", "-C", str(wt_path), "log", "origin/main..HEAD", "--oneline"],
        git_runner, timeout,
    )
    unique_commits = (
        (log_proc.stdout or "").strip()
        if log_proc is not None and log_proc.returncode == 0
        else ""
    )
    branch_merged = not unique_commits

    # Derive branch_name from the worktree list porcelain (the
    # ``branch refs/heads/<name>`` line). Empty when the dir is gone.
    # Issue #494 (PR review): the previous global scan always returned
    # the main checkout's branch — a per-block lookup is required.
    branch_name = _branch_name_for_porcelain_path(
        porcelain_stdout if is_listed else "", wt_path,
    )

    # Compute state from the probes. ``unknown`` only when HEAD could
    # not be read at all (everything else is a valid classification).
    if not is_listed:
        state = "gone"
        is_fresh = False
    elif branch_merged and head_full and head_full == main_full:
        # Branch is at origin/main and has no unique commits → fresh
        # iff the dir mtime is recent enough. Older than the threshold
        # → stale merged (treat as ``"merged"`` so the dashboard's
        # stale-cost tile catches it).
        try:
            mtime = wt_path.stat().st_mtime
            import time as _time
            age = _time.time() - mtime
        except OSError:
            age = WORKTREE_FRESH_MAX_AGE_SECONDS + 1
        if age <= WORKTREE_FRESH_MAX_AGE_SECONDS:
            state = "fresh"
            is_fresh = True
        else:
            state = "merged"
            is_fresh = False
    elif branch_merged:
        state = "merged"
        is_fresh = False
    else:
        state = "live"
        is_fresh = False

    return {
        "state": state,
        "worktree_listed": is_listed,
        "branch_merged_into_main": branch_merged,
        "is_fresh": is_fresh,
        "branch_tip": branch_tip,
        "branch_name": branch_name,
    }


def classify_all_worktrees(
    repo_root: Path,
    *,
    git_runner=subprocess.run,
    timeout: int = 5,
) -> dict[str, dict]:
    """Classify every canonical or legacy worktree directory.

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
    # Hoist the two repo-wide probes out of the per-dir loop. With ~360
    # worktrees on this checkout, the previous per-dir design spawned
    # ~1800 git subprocesses; the porcelain + origin/main probes are
    # identical for every dir, so they now run ONCE per dashboard.
    porcelain_proc = _run_probe(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        git_runner, timeout,
    )
    if porcelain_proc is not None and porcelain_proc.returncode == 0:
        precomputed_porcelain = (porcelain_proc.stdout or "", True)
    else:
        precomputed_porcelain = ("", False)
    main_proc = _run_probe(
        ["git", "-C", str(repo_root), "rev-parse", "origin/main"],
        git_runner, timeout,
    )
    if main_proc is not None and main_proc.returncode == 0:
        precomputed_origin_main = (main_proc.stdout or "").strip()
    else:
        precomputed_origin_main = ""
    for root_name in WORKTREE_ROOT_NAMES:
        wt_root = Path(repo_root) / root_name
        if not wt_root.exists() or not wt_root.is_dir():
            continue
        for child in sorted(wt_root.iterdir()):
            if not child.is_dir():
                continue
            meta[child.name] = classify_worktree_dir(
                child, Path(repo_root),
                git_runner=git_runner, timeout=timeout,
                precomputed_porcelain=precomputed_porcelain,
                precomputed_origin_main=precomputed_origin_main,
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


@dataclass
class SessionAggregate:
    """Typed accumulator for one JSONL walk — ``aggregate_session``'s
    in-flight state (issue #321 / smell-9).

    Replaces the previous dict-of-strings accumulator. Each field is
    named and typed so:
      * typo'd field accesses surface immediately under a type checker
        instead of silently producing zero totals,
      * ``parse_errors`` (Counter) gives the dashboard a real signal
        when a record carries a malformed token field (e.g.
        ``usage.input_tokens = "unknown"``) — the walker SKIPS the
        malformed field, counts it, and continues instead of crashing
        the whole session.
    """
    session_id: str | None = None
    repo: str = ""
    source: str = ""
    models: Counter = None  # type: ignore[assignment]  # filled in __post_init__
    latest_model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    ephemeral_5m: int = 0
    ephemeral_1h: int = 0
    tool_counts: Counter = None  # type: ignore[assignment]
    read_files: Counter = None  # type: ignore[assignment]
    user_texts: list = None  # type: ignore[assignment]
    branch_counts: Counter = None  # type: ignore[assignment]
    worktree_counts: Counter = None  # type: ignore[assignment]
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    # Skipped / malformed-record counters — issue #321 smell-9.
    # ``parse_errors`` is exposed on the final aggregate via a JSON-safe
    # plain dict (Counters don't json.dumps without a converter).
    parse_errors: Counter = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Mutable defaults must be constructed per-instance.
        self.models = Counter()
        self.tool_counts = Counter()
        self.read_files = Counter()
        self.user_texts = []
        self.branch_counts = Counter()
        self.worktree_counts = Counter()
        self.parse_errors = Counter()


def _new_session_state(source: str) -> SessionAggregate:
    """Build a fresh ``SessionAggregate`` accumulator for one JSONL walk.

    The accumulator is a typed dataclass (issue #321 / smell-9) so the
    per-record handlers can mutate it via attribute access (``agg.x = v``)
    without a stringly-typed surface area; type checkers validate every
    field the walker / finalizer reads. ``parse_errors`` starts at zero
    and counts malformed token fields the walker skipped.
    """
    return SessionAggregate(source=source)


def _harvest_common(rec: dict, st: SessionAggregate, fallback_session_id: str) -> None:
    """Common per-record harvest shared by both providers.

    Mutates ``st`` (a :class:`SessionAggregate`) to update timestamps,
    session id, repo, branch counter, and worktree counter.
    Provider-specific record handling is dispatched AFTER this harvest
    so ``st`` is always coherent.
    """
    # codex puts timestamp inside the payload — not at the record top
    # level. Harvest that too when the top-level field is empty so
    # filter_sessions(date_window) works.
    _ts_raw = rec.get("timestamp")
    if not _ts_raw:
        _rc_payload = rec.get("payload")
        if isinstance(_rc_payload, dict):
            _ts_raw = _rc_payload.get("timestamp")
    ts = parse_iso(_ts_raw or "")
    if ts is not None:
        if st.first_ts is None or ts < st.first_ts:
            st.first_ts = ts
        if st.last_ts is None or ts > st.last_ts:
            st.last_ts = ts

    if st.session_id is None:
        st.session_id = (
            rec.get("sessionId")
            or rec.get("session_id")
            or _codex_nested_field(rec, "sessionId")
            or _codex_nested_field(rec, "session_id")
            or fallback_session_id
        )
    if not st.repo:
        # codex rollouts put cwd at session_meta.payload.cwd or
        # turn_context.payload.cwd — NOT at the record top level.
        _rcwd = _codex_nested_field(rec, "cwd")
        st.repo = repo_from_cwd(_rcwd)
    gb = rec.get("gitBranch")
    if isinstance(gb, str) and gb.strip():
        st.branch_counts[gb.strip()] += 1
    cwd_raw = _codex_nested_field(rec, "cwd")
    if isinstance(cwd_raw, str) and cwd_raw.strip():
        st.worktree_counts[worktree_from_cwd(cwd_raw)] += 1


def _safe_int(value, *, counter: Counter, label: str) -> int:
    """Coerce a token-usage field to int, skipping malformed values.

    Returns 0 for ``None`` / empty / non-int-castable input and
    increments ``counter[label]`` for each skipped value so the
    dashboard can surface a 'N records skipped' signal instead of
    silently swallowing bad data (issue #321 / smell-9 follow-up).
    """
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        counter[label] += 1
        return 0


def _handle_claude_record(rec: dict, st: SessionAggregate) -> None:
    """Apply one claude-code JSONL record to ``st``.

    Handles ``assistant`` (model + usage + tool_use) and ``user``
    (text content) record types. Other record types are ignored.
    Malformed token-usage fields are SKIPPED + counted on
    ``st.parse_errors`` (issue #321 / smell-9) so a stray
    ``usage.input_tokens = "unknown"`` never crashes the walker.
    """
    msg = rec.get("message") or {}
    rec_type = rec.get("type")
    if rec_type == "assistant":
        m = msg.get("model")
        if m:
            st.models[m] += 1
            st.latest_model = m
        u = msg.get("usage") or {}
        # input_tokens = non-cached input (cache missed)
        st.input_tokens       += _safe_int(u.get("input_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_input_tokens")
        st.output_tokens      += _safe_int(u.get("output_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_output_tokens")
        st.cache_write_tokens += _safe_int(u.get("cache_creation_input_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_cache_creation_input_tokens")
        st.cache_read_tokens  += _safe_int(u.get("cache_read_input_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_cache_read_input_tokens")
        cc = u.get("cache_creation") or {}
        st.ephemeral_5m += _safe_int(cc.get("ephemeral_5m_input_tokens"),
                                      counter=st.parse_errors,
                                      label="malformed_ephemeral_5m")
        st.ephemeral_1h += _safe_int(cc.get("ephemeral_1h_input_tokens"),
                                      counter=st.parse_errors,
                                      label="malformed_ephemeral_1h")

        for blk in (msg.get("content") or []):
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use":
                name = blk.get("name") or "?"
                st.tool_counts[name] += 1
                if name == "Read":
                    inp = blk.get("input") or {}
                    fp = inp.get("file_path") or inp.get("path") or ""
                    if fp:
                        st.read_files[fp] += 1
    elif rec_type == "user":
        c = msg.get("content")
        if isinstance(c, str):
            if c.strip():
                st.user_texts.append(c.strip())
        elif isinstance(c, list):
            parts: list[str] = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    t = blk.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            joined = "\n".join(parts).strip()
            if joined:
                st.user_texts.append(joined)


def _handle_codex_record(rec: dict, st: SessionAggregate) -> None:
    """Apply one codex JSONL record to ``st``.

    Handles ``turn_context`` (model), ``event_msg`` (cumulative
    ``token_count`` snapshot + ``user_message`` text), and
    ``response_item`` (function_call / custom_tool_call) record
    types. Other record types (e.g. ``session_meta``) are ignored
    here — they're handled by ``_harvest_common`` for sid / cwd.
    """
    rec_type = rec.get("type")
    if rec_type == "turn_context":
        # codex: model lives on the per-turn context line.
        tc_payload = rec.get("payload") or {}
        if isinstance(tc_payload, dict):
            m = _codex_nested_field(rec, "model")
            if isinstance(m, str) and m:
                st.models[m] += 1
                st.latest_model = m
    elif rec_type == "event_msg":
        # codex: emit `token_count` (cumulative total_token_usage) and
        # `user_message` events. We overwrite (not accumulate) the token
        # snapshot, because each `token_count` event is a monotonically-
        # growing cumulative figure, not a delta.
        em_payload = rec.get("payload") or {}
        if isinstance(em_payload, dict):
            ptype = em_payload.get("type")
            if ptype == "token_count":
                info = em_payload.get("info") or {}
                if isinstance(info, dict):
                    tot = info.get("total_token_usage") or {}
                    if isinstance(tot, dict):
                        in_raw = _safe_int(tot.get("input_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_input_tokens")
                        cached = _safe_int(tot.get("cached_input_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_cached_input_tokens")
                        out_raw = _safe_int(tot.get("output_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_output_tokens")
                        reason = _safe_int(tot.get("reasoning_output_tokens"),
                                            counter=st.parse_errors,
                                            label="malformed_reasoning_output_tokens")
                        st.input_tokens = max(in_raw - cached, 0)
                        st.cache_read_tokens = cached
                        st.output_tokens = out_raw + reason
                        st.cache_write_tokens = 0
            elif ptype == "user_message":
                msg_text = em_payload.get("message")
                if isinstance(msg_text, str) and msg_text.strip():
                    st.user_texts.append(msg_text.strip())
    elif rec_type == "response_item":
        # codex tool calls: function_call + custom_tool_call.
        ri_payload = rec.get("payload") or {}
        if isinstance(ri_payload, dict):
            rtype = ri_payload.get("type")
            if rtype in ("function_call", "custom_tool_call"):
                name = ri_payload.get("name") or "?"
                st.tool_counts[name] += 1
                if name == "Read":
                    inp = ri_payload.get("input") or {}
                    fp = inp.get("file_path") or inp.get("path") or ""
                    if fp:
                        st.read_files[fp] += 1


def _resolve_branch(st: SessionAggregate, path: Path) -> str:
    """Branch fallback chain shared by both providers.

    Prefer the wire-format ``gitBranch`` (most-common across lines).
    Fall back to the immediate parent dir name. Legacy flat files
    have ``path.parent.name == "claude-code"`` or ``"codex"`` (the
    tool subdir itself) — those bucket under ``"main"`` so they
    aren't mis-attributed to a tool dir.
    """
    if st.branch_counts:
        return st.branch_counts.most_common(1)[0][0]
    parent = path.parent.name
    return "main" if parent in _KNOWN_SOURCES else (parent or "main")


def _resolve_worktree(st: SessionAggregate, path: Path) -> str:
    """Worktree fallback chain shared by both providers.

    Prefer the file path (authoritative — cwd can misattribute when
    the JSONL was captured from a parent checkout but lives under a
    sibling worktree's logs dir). Fall back to the cwd-derived
    Counter so legacy flat-layout files (no worktree segment in the
    path) keep working.
    """
    worktree = worktree_from_path(path)
    if worktree == "(main)":
        return st.worktree_counts.most_common(1)[0][0] if st.worktree_counts else "(unknown)"
    return worktree


def _finalize_session(st: SessionAggregate, *, source: str, log_path: Path) -> dict | None:
    """Build the final aggregate_session dict from the accumulator.

    Returns None when ``session_id`` is still None (the JSONL had no
    parseable records at all). Otherwise returns the canonical
    ``{session_id, source, repo, branch, worktree, model, first_ts,
    last_ts, input_tokens, output_tokens, cache_write_tokens,
    cache_read_tokens, ephemeral_5m, ephemeral_1h, tool_counts,
    read_files, user_texts, parse_errors, log_path}`` dict.
    """
    if st.session_id is None:
        return None
    branch = _resolve_branch(st, log_path)
    worktree = _resolve_worktree(st, log_path)
    return {
        "session_id": st.session_id,
        "source": source,
        "repo": st.repo or log_path.stem.split("__")[0],
        "branch": branch,
        "worktree": worktree,
        # A session can switch models. The dashboard's Active Sessions row
        # should show the model that handled the latest turn, not the model
        # that appeared most often earlier in the session.
        "model": st.latest_model or (st.models.most_common(1)[0][0] if st.models else ""),
        "first_ts": st.first_ts,
        "last_ts": st.last_ts,
        "input_tokens": st.input_tokens,
        "output_tokens": st.output_tokens,
        "cache_write_tokens": st.cache_write_tokens,
        "cache_read_tokens": st.cache_read_tokens,
        "ephemeral_5m": st.ephemeral_5m,
        "ephemeral_1h": st.ephemeral_1h,
        "tool_counts": st.tool_counts,
        "read_files": st.read_files,
        "user_texts": st.user_texts,
        # Issue #321 / smell-9: surface skipped malformed fields to the
        # caller (JSON-serializable plain dict, not a Counter).
        "parse_errors": dict(st.parse_errors),
        "log_path": str(log_path),
    }


def aggregate_session(path: Path) -> dict | None:
    """Walk one JSONL file once and return per-session aggregates, or None.

    Issue #310: split by provider record type. The walker dispatches
    each parsed record to ``_handle_claude_record`` or
    ``_handle_codex_record`` (chosen from ``_source_for(path)``) so
    adding a new record type only touches one place per provider.

    The walker shares the common per-record harvest
    (``_harvest_common``) — timestamps, sid, repo, branch counter,
    worktree counter — before dispatching, so a new provider only needs
    to implement its record-type handlers.
    """
    source = _source_for(path)
    st = _new_session_state(source=source)
    fallback_session_id = path.stem
    handler = _handle_codex_record if source == "codex" else _handle_claude_record

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

                _harvest_common(rec, st, fallback_session_id)
                handler(rec, st)
    except OSError:
        return None

    return _finalize_session(st, source=source, log_path=path)


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

    # 1. Cache hit < CACHE_HIT_WARN (50%) — prefix misalignment suspected.
    if total_input > 0 and cache_hit < CACHE_HIT_WARN:
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
        tool_costs[name] = n * DEFAULT_DUP_READ_TOKENS * pricing_for(s["model"])["in"] / 1_000_000
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
        opus_cost = session_cost(s)
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
        cost = session_cost(s)
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
# Per-panel renderers — small pure functions, each consuming a slice of the
# view_model. ``render_dashboard`` is the orchestrator that joins them.
# Issue #321 / smell-10: every per-panel aggregation lives in
# ``build_view_model``; the HTML path now reads panels instead of
# recomputing them, so JSON + HTML cannot drift.
# ---------------------------------------------------------------------------


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


def _render_cost_by_repo_panel(rows: list[dict]) -> str:
    """Render Cost by Repository table rows from ``view_model['cost_by_repo']``."""
    return "".join(
        f"<tr><td>{html.escape(r['name'])}</td>"
        f"<td style='text-align:right'>{r['sessions']}</td>"
        f"<td style='text-align:right'>${r['cost_usd']:.2f}</td>"
        f"<td><div class='bar'><span style='width:{r['share'] * 100:.1f}%'></span></div></td></tr>"
        for r in rows
    )


def _render_cost_by_branch_panel(rows: list[dict]) -> str:
    """Render Cost by Branch table rows from ``view_model['cost_by_branch']``."""
    return "".join(
        f"<tr><td>{html.escape(r['name'])}</td>"
        f"<td style='text-align:right'>{r['sessions']}</td>"
        f"<td style='text-align:right'>${r['cost_usd']:.2f}</td>"
        f"<td><div class='bar'><span style='width:{r['share'] * 100:.1f}%'></span></div></td></tr>"
        for r in rows
    )


def _render_cost_by_worktree_panel(rows: list[dict]) -> str:
    """Render Cost by Worktree table rows from
    ``view_model['cost_by_worktree_rows']`` (which already carries
    ``branch_tip`` / ``branch_name`` from ``wt_meta`` and a state column
    driven by session stamps + ``wt_meta`` fallback).
    """
    parts: list[str] = []
    for r in rows:
        state = r.get("state", "unknown")
        parts.append(
            f"<tr><td>{html.escape(r['name'])}</td>"
            f"<td style='text-align:right'>{r['sessions']}</td>"
            f"<td style='text-align:right'>${r['cost_usd']:.2f}</td>"
            f"<td><div class='bar'><span style='width:{r.get('cost_share', 0) * 100:.1f}%'></span></div></td>"
            f"<td>{_worktree_state_pill(state)}</td></tr>"
        )
    return "".join(parts)


def _render_cost_by_tool_panel(rows: list[dict]) -> tuple[str, str]:
    """Render Cost by Tool table + the "Read is #1" warning chip.

    Reads from ``view_model['cost_by_tool']``. Returns
    ``(rows_html, read_warning_html)`` — the warning chip is empty when
    Read isn't the top tool.
    """
    rows_html = "".join(
        f"<tr><td>{html.escape(r['name'])}</td>"
        f"<td style='text-align:right'>{r['calls']}</td>"
        f"<td style='text-align:right'>${r['cost_usd']:.2f}</td>"
        f"<td><div class='bar'><span style='width:{r['share'] * 100:.1f}%'></span></div></td></tr>"
        for r in rows
    )
    read_warning_html = ""
    if rows and rows[0]["name"] == "Read":
        read_warning_html = (
            '<div class="warning warn" style="margin-top:12px">'
            '🚨 Read 툴이 툴 비용 1위입니다 — 대용량 파일 반복 읽기를 의심하세요.'
            '</div>'
        )
    return rows_html, read_warning_html


def _render_cost_by_model_panel(
    rows: list[dict], unknown_models: list[str],
) -> tuple[str, str]:
    """Render Cost by Model table + the "unknown model id" warning chip.

    Reads from ``view_model['cost_by_model']`` plus the
    ``unknown_models`` list. Returns
    ``(rows_html, unknown_model_html)``.
    """
    rows_html = "".join(
        f"<tr><td>{html.escape(r['name'])}</td>"
        f"<td style='text-align:right'>{r['sessions']}</td>"
        f"<td style='text-align:right'>{r['tokens']:,}</td>"
        f"<td style='text-align:right'>${r['cost_usd']:.2f}</td>"
        f"<td><div class='bar'><span style='width:{r['share'] * 100:.1f}%'></span></div></td></tr>"
        for r in rows
    )
    unknown_model_html = ""
    if unknown_models:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in unknown_models)
        unknown_model_html = (
            '<div class="warning warn" style="margin-top:12px">'
            '⚠ Unknown model id(s) — falling back to Sonnet pricing:'
            f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>'
        )
    return rows_html, unknown_model_html


def _render_cache_ttl_panel(cache_ttl: dict) -> str:
    """Render the Cache TTL mix panel's write-rows block.

    Reads from ``view_model['cache_ttl']``. Three render states:

      a) ``cache_ttl['state'] == 'empty'`` — no cache-write activity
      b) ``cache_ttl['state'] == 'legacy'`` — legacy unsplit bucket only
      c) ``cache_ttl['state'] == 'split'`` — 5m + 1h buckets present
    """
    ttl_5m = cache_ttl["ttl_5m"]
    ttl_1h = cache_ttl["ttl_1h"]
    ttl_legacy = cache_ttl["ttl_legacy"]
    ttl_state = cache_ttl["state"]
    if ttl_state == "empty":
        return (
            '<div class="ttl-name">cache_write</div>'
            '<div class="ttl-empty" '
            'style="background:var(--panel-2);border-radius:4px;'
            'padding:6px 10px;color:var(--muted);font-size:11px">'
            'no cache-write activity captured this period'
            '</div><div class="ttl-pct">—</div>'
        )
    if ttl_state == "legacy":
        return (
            '<div class="ttl-name">cache_write (TTL unspecified, priced at 5m)</div>'
            f'<div class="bar writelegacy"><span style="width:{cache_ttl["legacy_pct"]:.1f}%"></span></div>'
            f'<div class="ttl-pct">{ttl_legacy:,}</div>'
        )
    return (
        '<div class="ttl-name">write 5m TTL</div>'
        f'<div class="bar write5m"><span style="width:{cache_ttl["write5m_pct"]:.1f}%"></span></div>'
        f'<div class="ttl-pct">{ttl_5m:,}</div>'
        '<div class="ttl-name">write 1h TTL</div>'
        f'<div class="bar write1h"><span style="width:{cache_ttl["write1h_pct"]:.1f}%"></span></div>'
        f'<div class="ttl-pct">{ttl_1h:,}</div>'
    )


def _render_cost_gate_banner(status: str, violations: list[dict]) -> str:
    """Render the Cost Gate banner."""
    if not violations:
        return (
            f'<div class="cost-gate {status}">'
            f'<span class="label">Cost Gate:</span> all sessions within '
            f'tokens/cost thresholds.'
            f'</div>'
        )
    items = "".join(
        f"<li><code>{html.escape(v['session_id'][:8])}</code> — {html.escape(v['reason'])} "
        f"(cost=${v['cost']:.2f})</li>"
        for v in violations
    )
    return (
        f'<div class="cost-gate {status}">'
        f'<span class="label">Cost Gate: {status.upper()}</span>'
        f'{len(violations)} session(s) exceeded thresholds:'
        f'<ul>{items}</ul></div>'
    )


def _render_session_row(
    session: dict, score: dict, warns: list["Warning"],
) -> str:
    """Render one Active/Inactive session row."""
    cost = _session_cost(session)
    hit = score["cache_hit_ratio"]
    started = session["first_ts"].strftime("%Y-%m-%d %H:%M") if session["first_ts"] else "—"
    sscore = score["total"]
    grade = score["grade"]
    pill_cls = "pill-good" if sscore >= 75 else ("pill-warn" if sscore >= 50 else "pill-bad")
    total_tools = sum(session["tool_counts"].values())
    warn_chips = " ".join(
        f"<span class='pill {'pill-bad' if w.level == 'critical' else 'pill-warn'}'>{html.escape(w.code)}</span>"
        for w in warns
    ) or "<span class='muted'>—</span>"
    wt_state = session.get("worktree_state", "unknown")
    wt_cell = f"{html.escape(session.get('worktree') or '—')} {_worktree_state_pill(wt_state)}"
    return (
        f"<tr><td><code>{html.escape(session['session_id'][:8])}</code></td>"
        f"<td>{html.escape(session.get('branch') or '—')}</td>"
        f"<td>{wt_cell}</td>"
        f"<td>{html.escape(session['model'] or '?')}</td>"
        f"<td class='muted'>{html.escape(started)}</td>"
        f"<td style='text-align:right'>{session['input_tokens']:,}</td>"
        f"<td style='text-align:right'>{session['output_tokens']:,}</td>"
        f"<td style='text-align:right'>{total_tools:,}</td>"
        f"<td style='text-align:right'>{hit:.0%}</td>"
        f"<td style='text-align:right'>${cost:.2f}</td>"
        f"<td style='text-align:right'><span class='pill {pill_cls}'>{sscore:.0f}</span>"
        f"<span class='grade grade-{grade}'>{grade}</span></td>"
        f"<td>{warn_chips}</td></tr>"
    )


def _split_sessions_into_active_inactive(
    scored: list[tuple[dict, dict]],
    warnings_per_session: list[list["Warning"]],
) -> tuple[list, list, int, int]:
    """Split (scored, warns) pairs into Active vs Inactive blocks.

    Filters zero-turn sessions out of both blocks (they carry no signal
    but stay in ``scored`` for the Transcript Index). Returns
    ``(active_pairs, inactive_pairs, active_count, inactive_count)``.
    """
    active: list = []
    inactive: list = []
    for (s, sc), warns in zip(scored, warnings_per_session):
        if _is_zero_turn_session(s):
            continue
        if s.get("worktree_state") in STALE_WORKTREE_STATES:
            inactive.append(((s, sc), warns))
        else:
            active.append(((s, sc), warns))
    return active, inactive, len(active), len(inactive)


def _render_session_rows(pairs: list) -> str:
    """Render one block of (session, score, warns) pairs as <tr> rows."""
    if not pairs:
        return "<tr><td colspan='12' class='muted'>No sessions.</td></tr>"
    parts = [
        _render_session_row(s, sc, warns)
        for ((s, sc), warns) in pairs
    ]
    return "\n".join(parts)


def _render_transcript_index(  # noqa: E402 — uses lazy-loaded view_model helpers
    scored: list[tuple[dict, dict]], transcripts_dirname: str,
) -> str:
    """Render the Transcript Index table — one row per worktree."""
    ti_costs: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for s, _ in scored:
        wt = s.get("worktree") or "(unknown)"
        ti_costs[wt][0] += 1
        ti_costs[wt][1] += _session_cost(s)
    ti_sorted = sorted(ti_costs, key=lambda k: -ti_costs[k][1])
    if "(main)" in ti_sorted:
        ti_sorted.remove("(main)")
        ti_sorted = ["(main)"] + ti_sorted
    rows: list[str] = []
    for wt in ti_sorted:
        if transcripts_dirname:
            href = f"{transcripts_dirname}/{_safe_seg_via_vm(wt)}/index.html"
            open_cell = f"<a href='{html.escape(href)}'>open →</a>"
        else:
            open_cell = "<span class='muted'>—</span>"
        rows.append(
            f"<tr><td>{html.escape(wt)}</td>"
            f"<td style='text-align:right'>{int(ti_costs[wt][0])}</td>"
            f"<td style='text-align:right'>${ti_costs[wt][1]:.2f}</td>"
            f"<td>{open_cell}</td></tr>"
        )
    return "".join(rows) or "<tr><td colspan='4' class='muted'>No sessions.</td></tr>"


def _render_warnings_panel(
    warnings_per_session: list[list["Warning"]],
) -> tuple[str, set[str]]:
    """Render the warnings list (deduped by code) + return the fired codes.

    Returns ``(warnings_html, fired_codes)`` — ``fired_codes`` is also
    consumed by ``_render_optimization_items`` to mark recommendations
    as "do" vs "don't".
    """
    seen_codes: set[str] = set()
    blocks: list[str] = []
    for warns in warnings_per_session:
        for w in warns:
            if w.code in seen_codes:
                continue
            seen_codes.add(w.code)
            css = "warning" if w.level == "critical" else "warning warn"
            blocks.append(f'<div class="{css}">{html.escape(w.message)}</div>')
    warnings_html = "\n".join(blocks) or '<div class="muted">No anti-patterns detected.</div>'
    return warnings_html, seen_codes


def _render_roi_actions(
    warnings_per_session: list[list["Warning"]],
) -> str:
    """Render the ROI Actions ranked list (top 20 by $ save)."""
    candidates: list[tuple[float, str, str, str, int]] = []
    for warns in warnings_per_session:
        for w in warns:
            if w.estimated_save_usd > 0:
                candidates.append(
                    (w.estimated_save_usd, w.code, w.session_id,
                     w.evidence, w.priority),
                )
    candidates.sort(key=lambda x: -x[0])
    if not candidates:
        return ('<li class="muted">No reclaimable savings detected — '
                'cache hit and tool usage are within targets.</li>')
    return "".join(
        f"<li title='{html.escape(WARNING_RECOMMENDATIONS.get(code, ''))}'>"
        f"<span class='rank'>#{i+1}</span>"
        f"<span class='save'>${save:.2f}</span>"
        f"<span class='code'>{html.escape(code)}</span>"
        f"<span class='sid'><code>{html.escape(sid[:8]) if sid else '—'}</code></span>"
        f"<span class='evidence'>{html.escape(evidence) if evidence else '—'}</span>"
        f"<span class='muted'>(P{prio})</span></li>"
        for i, (save, code, sid, evidence, prio) in enumerate(candidates[:20])
    )


def _render_optimization_items(fired_codes: set[str]) -> str:
    """Render the Recommended Optimizations do/don't list."""
    opt_items: list[str] = []
    for code in WARNING_RECOMMENDATIONS:
        if code in fired_codes:
            opt_items.append(f'<li class="do">{html.escape(WARNING_RECOMMENDATIONS[code])}</li>')
        else:
            opt_items.append(f'<li class="dont muted-item">{html.escape(WARNING_DONT.get(code, ""))}</li>')
    return "\n".join(opt_items)


def _build_dashboard_subtitle(
    repo: str, days: int, scored: list[tuple[dict, dict]],
    active_count: int, inactive_count: int, worktree_filter: str,
) -> str:
    """Build the dashboard subtitle line."""
    subtitle_parts = [
        html.escape(repo),
        f"last {days} days",
        f"{len(scored)} sessions ({active_count} active · {inactive_count} inactive)",
        f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if worktree_filter:
        subtitle_parts.insert(1, f"worktree={html.escape(worktree_filter)}")
    return " · ".join(subtitle_parts)


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
                     transcripts_dirname: str = "",
                     *, view_model: dict | None = None) -> str:
    """Compose the HTML dashboard.

    Issue #321 / smell-10: when ``view_model`` is supplied, every
    per-panel number is read from it (no recomputation) — JSON + HTML
    cannot drift on per-panel totals. When ``view_model`` is omitted,
    the function falls back to building one inline so existing direct
    callers stay compatible. ``main()`` always passes the snapshot's
    ``view_model``.

    Per-panel aggregation lives in :func:`build_view_model` (single
    source of truth). This function is a thin orchestrator over the
    per-panel renderers above.
    """
    if isinstance(estimated, (int, float)):
        estimated = {
            "cache_miss": float(estimated), "dup_read": 0.0,
            "model_downgrade": 0.0, "total": float(estimated),
        }

    # Legacy fallback: build the view-model inline so direct callers
    # without ``view_model=`` (e.g. older tests) still work.
    if view_model is None:
        view_model = build_view_model(
            repo=repo, days=days,
            sessions=sessions, scored=scored,
            warnings_per_session=warnings_per_session,
            estimated=estimated,
            cost_gate=cost_gate,
            all_sessions_in_window=all_sessions_in_window,
            unknown_models=unknown_models,
            wt_meta=wt_meta,
            stale_cost=stale_cost,
            stale_pct=stale_pct,
        )

    vm = view_model
    totals = vm["totals"]
    unknown_models_list = vm["unknown_models"]
    gate_status = vm["cost_gate"]["status"]
    gate_violations = vm["cost_gate"]["violations"]

    # Per-panel renderers — each reads from vm (no recomputation).
    repo_rows = _render_cost_by_repo_panel(vm["cost_by_repo"])
    branch_rows = _render_cost_by_branch_panel(vm["cost_by_branch"])
    worktree_rows = _render_cost_by_worktree_panel(vm["cost_by_worktree_rows"])
    tool_rows, read_warning_html = _render_cost_by_tool_panel(vm["cost_by_tool"])
    model_rows, unknown_model_html = _render_cost_by_model_panel(
        vm["cost_by_model"], unknown_models_list,
    )
    ttl_middle_html = _render_cache_ttl_panel(vm["cache_ttl"])
    cost_gate_banner = _render_cost_gate_banner(gate_status, gate_violations)

    # Session table split — derived from scored/warnings_per_session
    # (view_model only carries the COUNT split, not the row-level split).
    active_pairs, inactive_pairs, active_count, inactive_count = (
        _split_sessions_into_active_inactive(scored, warnings_per_session)
    )
    active_session_rows = _render_session_rows(active_pairs)
    inactive_session_rows = _render_session_rows(inactive_pairs)

    # Warnings / ROI / optimizations — derived from warnings_per_session.
    warnings_html, fired_codes = _render_warnings_panel(warnings_per_session)
    roi_items = _render_roi_actions(warnings_per_session)
    optimize_items = _render_optimization_items(fired_codes)

    # Transcript Index — derived from scored (transcripts are per-session).
    transcript_index_rows = _render_transcript_index(scored, transcripts_dirname)

    subtitle = _build_dashboard_subtitle(
        repo, days, scored, active_count, inactive_count, worktree_filter,
    )

    return HTML_TEMPLATE.format(
        repo=html.escape(repo),
        days=days,
        session_count=totals["session_count"],
        repos_named=totals["repos_named"],
        active_count=active_count,
        inactive_count=inactive_count,
        total_cost=totals["total_cost"],
        total_tokens=totals["total_tokens"],
        avg_score=totals["avg_score"],
        avg_grade=totals["avg_grade"],
        avg_cache=totals["avg_cache"],
        avg_density=totals["avg_density"],
        avg_redundancy=totals["avg_redundancy"],
        avg_economy=totals["avg_economy"],
        avg_cache_hit=totals["avg_cache_hit"],
        stale_cost=stale_cost,
        stale_pct=stale_pct,
        cost_gate_banner=cost_gate_banner,
        repo_rows=repo_rows,
        branch_rows=branch_rows,
        worktree_rows=worktree_rows,
        tool_rows=tool_rows,
        read_warning_html=read_warning_html,
        model_rows=model_rows,
        unknown_model_html=unknown_model_html,
        ttl_read_tokens=vm["cache_ttl"]["ttl_read"],
        ttl_5m_tokens=vm["cache_ttl"]["ttl_5m"],
        ttl_1h_tokens=vm["cache_ttl"]["ttl_1h"],
        ttl_miss_tokens=vm["cache_ttl"]["ttl_miss"],
        ttl_read_pct=vm["cache_ttl"]["read_pct"],
        ttl_5m_pct=vm["cache_ttl"]["write5m_pct"],
        ttl_1h_pct=vm["cache_ttl"]["write1h_pct"],
        ttl_miss_pct=vm["cache_ttl"]["miss_pct"],
        ttl_middle_html=ttl_middle_html,
        ttl_caveat=html.escape(CACHE_TTL_CAVEAT),
        active_session_rows=active_session_rows,
        inactive_session_rows=inactive_session_rows,
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


@dataclass
class AnalysisRequest:
    """Filter set the user requested on the CLI.

    Mirrors the relevant CLI flags without dragging the full argparse
    Namespace into the snapshot — a snapshot is built once and consumed
    by JSON + HTML sinks; tests can also build one directly.
    """
    repo: str
    days: int
    logs_dir: Path
    branch: str = ""
    worktree: str = ""
    cost_gate_tokens: int = DEFAULT_COST_GATE_TOKENS
    cost_gate_usd: float = DEFAULT_COST_GATE_USD
    pricing_override: Path | None = None
    include_worktree_logs: bool = True


@dataclass
class AnalysisSnapshot:
    """Single immutable artifact shared by JSON + HTML sinks (issue #321 / smell-21).

    The previous shape ran every aggregation inside ``main()`` and again
    inside ``render_dashboard()``; the two walks could drift on any future
    edit. With this snapshot, every per-panel number (totals, per-repo /
    branch / worktree / tool / model breakdowns, active vs. inactive split,
    cost gate status, warnings, view-model) is computed ONCE and read by
    both sinks. Drift becomes impossible by construction.

    Field groups:

      * ``request``           — the CLI filter set, echoed into the JSON sink
      * ``files``             — every JSONL the scanner discovered + deduped
      * ``windowed``          — sessions in the (repo, days, worktree) window
                                — feeds the per-repo / branch / worktree
                                  panels (which intentionally surface other
                                  branches too, so a ``--branch`` filter
                                  does not collapse the panel to one row)
      * ``selected``          — sessions also matching ``--branch``
                                — feeds the per-session table + JSON totals
      * ``scored``            — ``[(session, score_dict)]`` for selected
      * ``warnings_per_session`` — Warning list per selected session
      * ``reclaim_*``         — per-session reclaim axes (cache / dup /
                                model_downgrade) for the savings estimate
      * ``estimated``         — ``estimated_savings(scored)``
      * ``gate_*``            — cost-gate evaluation
      * ``unknown_models``    — set populated during pricing
      * ``wt_meta``           — per-worktree classification
      * ``total_cost``        — pre-computed sum across selected (echoes to
                                ``[ok]`` stdout line + JSON ``total_cost_usd``)
      * ``stale_*``           — stale-cost aggregate
      * ``view_model``        — the pre-aggregated dashboard data fed to
                                ``render_dashboard``
    """
    request: AnalysisRequest
    files: list
    sessions: list
    windowed: list
    selected: list
    scored: list
    warnings_per_session: list
    reclaim_cache: list
    reclaim_dup: list
    reclaim_downgrade: list
    estimated: dict
    gate_status: str
    gate_violations: list
    unknown_models: set
    wt_meta: dict
    total_cost: float
    total_tokens: int
    stale_cost: float
    stale_pct: float
    view_model: dict


def build_analysis_snapshot(
    *,
    repo: str,
    days: int,
    logs_dir: Path,
    branch: str = "",
    worktree: str = "",
    cost_gate_tokens: int = DEFAULT_COST_GATE_TOKENS,
    cost_gate_usd: float = DEFAULT_COST_GATE_USD,
    pricing_override: Path | None = None,
    include_worktree_logs: bool = True,
    repo_root: Path | None = None,
) -> AnalysisSnapshot:
    """Build the single AnalysisSnapshot consumed by JSON + HTML sinks.

    ``main()`` calls this once and feeds the result to both sinks; tests
    can also call it directly with a constructed request. The helper owns
    the full pipeline:

      1. Apply pricing override (CLI flag)
      2. Discover JSONL logs (auto-walk worktrees when requested)
      3. Dedup dual-write sessionId collisions
      4. Per-file ``aggregate_session`` → ``sessions``
      5. Stamp ``worktree_state`` from ``wt_meta``
      6. Filter to ``windowed`` (repo+days+worktree) and ``selected``
         (repo+days+branch+worktree)
      7. Score, evaluate warnings, estimate savings, enforce cost gate
      8. Walk once for unknown-model ids
      9. Compute totals + stale_cost + stale_pct
     10. Build the view-model

    Returns an :class:`AnalysisSnapshot` — see its docstring for the
    field set. The caller is responsible for warning-line emission
    (unknown models / unknown worktrees / cost-gate stderr lines).
    """
    request = AnalysisRequest(
        repo=repo, days=days, logs_dir=logs_dir,
        branch=branch, worktree=worktree,
        cost_gate_tokens=cost_gate_tokens, cost_gate_usd=cost_gate_usd,
        pricing_override=pricing_override,
        include_worktree_logs=include_worktree_logs,
    )

    # Apply pricing override before any pricing call.
    load_pricing_override(pricing_override)

    resolved_logs_dir = logs_dir.resolve()
    resolved_repo_root = (Path.cwd().resolve() if include_worktree_logs
                          else None)
    files = discover_logs(resolved_logs_dir, repo_root=resolved_repo_root)
    # Dual-write (#173) places the same sessionId in two files; dedup to
    # one snapshot per sessionId so cost and branch attribution are not
    # double-counted or skewed by the stale main-side copy.
    files = _dedupe_by_session(files)

    # Worktree classification (per project canonical/legacy worktree dir, vs
    # `git worktree list` + ancestor-of-origin/main check). Skipped when
    # --no-include-worktree-logs is in effect to avoid surprising git walks.
    wt_meta: dict[str, dict] = (
        classify_all_worktrees(resolved_repo_root) if resolved_repo_root is not None else {}
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

    # --repo scopes ALL aggregate panels (Cost by Repository / Branch /
    # Worktree / Tool / Model + sessions tables) to the focused project —
    # not just the per-session total. Pass ``repo`` so the window
    # matches the selection; an empty string here would let other repos'
    # sessions bleed into the per-repo / per-branch / per-worktree rows.
    windowed = filter_sessions(sessions, repo, days, worktree=worktree)
    selected = filter_sessions(sessions, repo, days, branch, worktree)

    scored: list[tuple[dict, dict]] = [(s, score_session(s)) for s in selected]
    reclaim_cache = cache_miss_reclaim(scored)
    reclaim_dup = dup_read_reclaim(scored)
    reclaim_dn = model_downgrade_reclaim(scored)
    warnings_per_session = [
        evaluate_warnings(s, sc, rc, rd, rdn)
        for (s, sc), rc, rd, rdn in zip(scored, reclaim_cache, reclaim_dup, reclaim_dn)
    ]
    estimated = estimated_savings(scored)

    gate_status, gate_violations = enforce_cost_gate(
        scored, cost_gate_tokens, cost_gate_usd,
    )

    # Detect unknown model ids (collect during scoring). Re-walk once.
    unknown_models: set[str] = set()
    for s in selected:
        pricing_for(s["model"], _unknown_models=unknown_models)

    # Pre-compute total_cost / total_tokens / stale_cost / stale_pct from
    # the SAME `selected` set the JSON total_cost_usd + HTML Total Cost
    # cell will read. Both sinks pull these from the snapshot so they
    # cannot drift (issue #321 / smell-21).
    session_costs: list[float] = []
    for s in selected:
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
        for s in selected
    )

    stale_cost = sum(
        c for c, s in zip(session_costs, selected)
        if s.get("worktree_state") in STALE_WORKTREE_STATES
    )
    stale_pct = (stale_cost / total_cost) if total_cost > 0 else 0.0

    # Build the view-model ONCE — both JSON and HTML sinks consume it
    # (issue #310). Adding a new panel now touches one aggregation site
    # instead of two (the old ``main()`` / ``render_dashboard()``
    # duplicated the cost-by-X loops).
    view_model = build_view_model(
        repo=repo,
        days=days,
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
    )

    return AnalysisSnapshot(
        request=request,
        files=files,
        sessions=sessions,
        windowed=windowed,
        selected=selected,
        scored=scored,
        warnings_per_session=warnings_per_session,
        reclaim_cache=reclaim_cache,
        reclaim_dup=reclaim_dup,
        reclaim_downgrade=reclaim_dn,
        estimated=estimated,
        gate_status=gate_status,
        gate_violations=gate_violations,
        unknown_models=unknown_models,
        wt_meta=wt_meta,
        total_cost=total_cost,
        total_tokens=total_tokens,
        stale_cost=stale_cost,
        stale_pct=stale_pct,
        view_model=view_model,
    )


def _emit_snapshot_warnings(snap: AnalysisSnapshot) -> None:
    """Emit stderr warnings derived from the snapshot.

    Mirrors the historical stderr lines ``main()`` printed before HTML
    write / JSON print so the Iron Law ``[ok]`` stdout contract and the
    stderr warning lines stay in the same order regardless of which sink
    runs.
    """
    if not snap.selected:
        warn_target = f"repo='{snap.request.repo}'"
        if snap.request.branch:
            warn_target += f" branch='{snap.request.branch}'"
        if snap.request.worktree:
            warn_target += f" worktree='{snap.request.worktree}'"
        print(f"[warn] No sessions matched {warn_target} "
              f"within {snap.request.days} days.", file=sys.stderr)

    # Unknown-model warnings.
    for m in sorted(snap.unknown_models):
        print(f"WARN: unknown model '{m}' — using sonnet fallback pricing",
              file=sys.stderr)

    # Worktree classification fallback warnings.
    for wt_name, meta in sorted(snap.wt_meta.items()):
        if meta.get("state") == "unknown" and wt_name != "(main)":
            print(
                f"WARN: worktree '{wt_name}' classification failed "
                f"(branch tip={meta.get('branch_tip','') or '?'}); "
                f"check that origin/main is fetchable from this repo.",
                file=sys.stderr,
            )

    # Cost Gate stderr lines.
    for line in cost_gate_stderr_lines(snap.gate_violations):
        print(line, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — thin dispatch over an :class:`AnalysisSnapshot`.

    Issue #321 / smell-21: every aggregation lives in
    :func:`build_analysis_snapshot`; ``main()`` parses CLI flags,
    builds the snapshot once, then forks to the JSON or HTML sink
    using only the snapshot's pre-computed fields. Both sinks consume
    the same numbers, so per-panel totals cannot drift (the bug that
    motivated the split).
    """
    parser = argparse.ArgumentParser(description="Token efficiency analyzer + HTML dashboard.")
    parser.add_argument("--repo", required=True, help="Repository name to filter (matches basename of cwd).")
    parser.add_argument("--days", type=int, default=30, help="Look-back window in days (default 30).")
    parser.add_argument(
        "--logs-dir",
        default=None,
        help="Logs root directory (default: ./logs). When set explicitly, "
             "sibling-worktree auto-discovery is disabled.",
    )
    parser.add_argument("--include-worktree-logs", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Auto-discover logs from .worktrees/*/logs/ and legacy worktree roots (default: True). "
                             "Pass --no-include-worktree-logs to disable. Implicitly disabled when --logs-dir "
                             "is set explicitly.")
    parser.add_argument("--out", default=None, help="Output HTML path (default: docs/observability/dashboard-<repo>-<days>d.html).")
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

    # Pre-snapshot guard: surface "no logs found" as exit 2 BEFORE we
    # build a snapshot (no point building one for an empty scan).
    explicit_logs_dir = args.logs_dir is not None
    logs_dir = Path(args.logs_dir) if explicit_logs_dir else Path("logs")
    include_worktree_logs = args.include_worktree_logs and not explicit_logs_dir
    # Cheap probe — does the user-supplied logs dir contain any JSONL?
    # ``build_analysis_snapshot`` does the heavy walk; we only need to
    # confirm a JSONL exists under the explicit logs dir (the worktree
    # auto-walk in ``build_analysis_snapshot`` may find more, but the
    # CLI was pointing at a specific dir — silence stderr noise).
    probe_target = logs_dir / "claude-code" if (logs_dir / "claude-code").exists() else logs_dir
    has_files = probe_target.exists() and any(probe_target.rglob("*.jsonl"))
    if not has_files:
        # Mirror the historical stderr message; keep exit code 2.
        print(f"[error] No JSONL logs found under {logs_dir}/(claude-code|codex)/"
              f"{' (including sibling-worktree logs)' if include_worktree_logs else ''}",
              file=sys.stderr)
        return 2

    # Build the snapshot ONCE — both JSON and HTML sinks consume it.
    snap = build_analysis_snapshot(
        repo=args.repo,
        days=args.days,
        logs_dir=logs_dir,
        branch=args.branch,
        worktree=args.worktree,
        cost_gate_tokens=args.cost_gate_tokens,
        cost_gate_usd=args.cost_gate_usd,
        pricing_override=Path(args.pricing_override) if args.pricing_override else None,
        include_worktree_logs=include_worktree_logs,
    )

    # Empty-but-valid case: no error, but no selected sessions either.
    # ``build_analysis_snapshot`` doesn't fail-fast on empty; honor the
    # historical exit-0 (e.g. ``test_main_mixed_flat_and_nested``) but
    # still allow downstream JSON/HTML to render with empty panels.
    _emit_snapshot_warnings(snap)

    if args.json:
        return _emit_json(snap)

    out_path = Path(args.out) if args.out else Path(
        f"docs/observability/dashboard-{snap.request.repo}-{snap.request.days}d.html"
    )
    transcripts_written = _render_html(snap, out_path, transcripts_enabled=args.transcripts)

    # Console summary (stdout — Iron Law contract). All numbers come
    # from the snapshot; no recomputation here, so JSON+HTML+stdout
    # cannot drift on totals.
    print(f"[ok] sessions={len(snap.selected)}  files_scanned={len(snap.files)}  "
          f"total_cost=${snap.total_cost:.2f}  "
          f"estimated_savings=${snap.estimated['total']:.2f}  "
          f"stale_cost=${snap.stale_cost:.2f}  "
          f"transcripts={transcripts_written}")
    print(f"[ok] dashboard -> {out_path}")
    return 0


def _emit_json(snap: AnalysisSnapshot) -> int:
    """Render the JSON sink from the snapshot.

    Pulls every per-panel number from the snapshot's ``view_model`` so
    JSON + HTML agree bit-for-bit (issue #321 / smell-21 fix).
    """
    request = snap.request
    vm = snap.view_model
    out = {
        "repo": request.repo,
        "branch": request.branch,
        "branch_filter_active": bool(request.branch),
        "worktree": request.worktree,
        "worktree_filter_active": bool(request.worktree),
        "days": request.days,
        "files_scanned": len(snap.files),
        "sessions": len(snap.selected),
        "active_sessions": vm["active_count"],
        "inactive_sessions": vm["inactive_count"],
        # ``total_cost_usd`` derives from ``snap.total_cost`` (the same
        # figure HTML echoes) — both sinks read from the snapshot.
        "total_cost_usd": round(snap.total_cost, 4),
        "stale_cost_usd": round(snap.stale_cost, 4),
        "stale_pct": round(snap.stale_pct, 4),
        "estimated_savings_usd": vm["estimated"],
        "cost_gate": {
            "status": snap.gate_status,
            "tokens_threshold": request.cost_gate_tokens,
            "usd_threshold": request.cost_gate_usd,
            "violations": [
                {k: (round(v[k], 4) if isinstance(v[k], float) else v[k]) for k in v}
                for v in snap.gate_violations
            ],
        },
        "warnings": vm["warnings"],
        "unknown_models": vm["unknown_models"],
        "worktrees": vm["cost_by_worktree_rows"],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if snap.gate_status == "bad":
        return 3
    return 0


def _render_html(snap: AnalysisSnapshot, out_path: Path, *,
                 transcripts_enabled: bool) -> int:
    """Render the HTML dashboard + (optional) transcript sidecars.

    Returns the count of transcript sidecar pages written so
    ``main()`` can echo it on the ``[ok]`` stdout line.
    """
    transcripts_dirname = (out_path.stem + ".assets") if transcripts_enabled else ""

    html_out = render_dashboard(
        repo=snap.request.repo,
        days=snap.request.days,
        sessions=snap.selected,
        scored=snap.scored,
        warnings_per_session=snap.warnings_per_session,
        estimated=snap.estimated,
        cost_gate=(snap.gate_status, snap.gate_violations),
        all_sessions_in_window=snap.windowed,
        unknown_models=snap.unknown_models,
        wt_meta=snap.wt_meta,
        stale_cost=snap.stale_cost,
        stale_pct=snap.stale_pct,
        worktree_filter=snap.request.worktree,
        transcripts_dirname=transcripts_dirname,
        view_model=snap.view_model,
    )
    out_path.write_text(html_out, encoding="utf-8")

    transcripts_written = 0
    if transcripts_enabled:
        transcripts_written = _write_transcript_sidecars(
            snap.selected, out_path,
        )
    return transcripts_written


def _write_transcript_sidecars(selected: list[dict], out_path: Path) -> int:
    """Write per-worktree transcript sidecar pages; return the count.

    One dir per worktree, one page per session, plus a per-worktree
    index. Navigation is <a href> only, so nothing is loaded until the
    user clicks.
    """
    assets_dir = out_path.with_name(out_path.stem + ".assets")
    dash_href = f"../../{out_path.name}"
    sessions_by_wt: dict[str, list[dict]] = defaultdict(list)
    for s in selected:
        sessions_by_wt[s.get("worktree") or "(unknown)"].append(s)
    written = 0
    for wt, wt_sessions in sessions_by_wt.items():
        wt_dir = assets_dir / _safe_seg_via_vm(wt)
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
            written += 1
    return written


# ---------------------------------------------------------------------------
# Backward-compatibility shim (PR-D): re-export view_model symbols.
# The 870-line filter_sessions + build_view_model monster now lives in
# tools/te_analyzer/view_model.py. Existing `from token_efficiency_analyzer
# import build_view_model` calls keep working via this eager import that
# populates the module's globals before the script entry point runs.
# ---------------------------------------------------------------------------
from te_analyzer.view_model import (  # noqa: E402,F401,I001
    CSS,
    HTML_TEMPLATE,
    SIDECAR_CSS,
    SIDECAR_TEMPLATE,
    _is_zero_turn_session,
    _safe_seg as _safe_seg_via_vm,
    _session_cost,
    _sid_file,
    build_view_model,
    filter_sessions,
    render_transcript_page,
    render_worktree_index,
)

if __name__ == "__main__":
    raise SystemExit(main())
