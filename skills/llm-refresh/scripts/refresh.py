#!/usr/bin/env python3
"""refresh.py — refresh docs/llm-info/<provider>.json from each vendor's
official pricing page.

Usage:
    python3 skills/llm-refresh/scripts/refresh.py [--provider ID] [--check] [--json]
    python3 skills/llm-refresh/scripts/refresh.py --fetch-fixture PROVIDER FILE
        # Debug helper: parse a locally-saved HTML/MD page without network.

Why this exists instead of inline WebFetch:
    The dev-kit plugin policy disallows WebFetch (see lib/llm_judge.py for the
    only positive HTTP caller precedent). This script mirrors that pattern:
    `urllib.request.urlopen` with a Mozilla-class User-Agent (some vendor
    CDNs 403 the default urllib UA), wrapped in a single Bash-invoked Python
    entry so the SKILL.md body never sees the network.

Exit codes (sentinel-style, designed for chain audits):
    0 — no change (--check) OR all targets written successfully
    1 — --check was used AND at least one provider produced a diff
    2 — fetch or parse failure for at least one provider
    3 — usage error (unknown provider id, missing sources.json)

The script is intentionally write-restricted unless invoked without --check.
Like bin/set-provider.sh, "auto-rewrite silently inverted user intent" is a
known bug class we do not reintroduce here.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Re-use the plugin's POSIX-atomic write helper (lib/atomic.py).
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parents[3] / "lib"))

from atomic import atomic_write_json, now_iso  # type: ignore  # noqa: E402

# Mozilla-class UA. Some vendor CDNs 403 the default Python UA; this header
# mimics a real desktop browser.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 20
DEFAULT_RETRIES = 1
EXIT_OK = 0
EXIT_DIFF = 1
EXIT_FETCH = 2
EXIT_USAGE = 3


# ---------- project root ----------

def _project_root() -> Path:
    candidate = _THIS.parents[3]
    if (candidate / ".claude-plugin" / "plugin.json").exists():
        return candidate
    cwd_candidate = Path.cwd()
    if (cwd_candidate / ".claude-plugin" / "plugin.json").exists():
        return cwd_candidate
    return candidate


# ---------- fetch ----------

def fetch_url(url: str, *, timeout: int = DEFAULT_TIMEOUT_S, retries: int = DEFAULT_RETRIES) -> str:
    """Fetch `url` via urllib with a Mozilla UA; raise RuntimeError after `retries` failures."""
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/x-md,text/markdown,*/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - official public pricing pages only
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
    raise RuntimeError(f"fetch failed after {retries + 1} attempt(s): {url}: {last}")


# ---------- HTML table extraction ----------
#
# Modern docs sites (Docusaurus / Nextra / Mintlify) render markdown tables
# as real HTML <table> elements. Rather than one bespoke regex per provider,
# we extract every table, then let each provider pick the right one.

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(cell_html: str) -> str:
    """Strip nested tags and collapse whitespace inside a single cell."""
    text = _TAG_RE.sub(" ", cell_html)
    return _WS_RE.sub(" ", text).strip(" |").strip()


def extract_html_tables(html: str) -> List[Dict[str, List[List[str]]]]:
    """Return every HTML <table> on the page as `{headers: [...], rows: [[...]]}`.

    Headers = the first <tr>; body rows = subsequent <tr> entries. Empty cells
    after cleaning are preserved as `""` so column alignment stays stable.
    """
    tables: List[Dict[str, List[List[str]]]] = []
    for t_html in re.findall(r"<table\b[^>]*>(.*?)</table>", html, re.DOTALL | re.IGNORECASE):
        rows_raw = re.findall(r"<tr\b[^>]*>(.*?)</tr>", t_html, re.DOTALL | re.IGNORECASE)
        rows: List[List[str]] = []
        for r in rows_raw:
            cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", r, re.DOTALL | re.IGNORECASE)
            rows.append([_clean(c) for c in cells])
        rows = [r for r in rows if any(c for c in r)]  # drop fully-empty rows
        if not rows:
            continue
        tables.append({"headers": rows[0], "rows": rows[1:]})
    return tables


def find_table(tables: List[Dict[str, List[List[str]]]], *keywords: str) -> Optional[Dict[str, List[List[str]]]]:
    """Return the first table whose joined header row contains every keyword (case-insensitive)."""
    for t in tables:
        joined = " ".join(t["headers"]).lower()
        if all(k.lower() in joined for k in keywords):
            return t
    return None


# ---------- per-cell price parsing ----------

_PRICE_CELL_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)|([0-9]+(?:\.[0-9]+)?)\s*[/]?\s*(?:tokens?|mtok|million)?", re.IGNORECASE)


def first_price_in(cell: str) -> Optional[float]:
    """Pull the first dollar-or-number value from a cell like '$5 / MTok' or '0.435'."""
    if not cell:
        return None
    m = _PRICE_CELL_RE.search(cell)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        return float(raw)
    except ValueError:
        return None


# ---------- parsers ----------
#
# Each parser returns the documented payload:
#   {provider, label, source_url, fetched_at, currency, models[], plans[]}
# Pages are owned by their vendors; if the page structure changes, the parser
# raises ValueError with an explicit message. Like bin/set-provider.sh, we
# refuse to silently fall back to "structure preserved".

def _payload(meta: Dict[str, Any], models: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "provider": meta["id"],
        "label": meta["label"],
        "source_url": meta["url"],
        "fetched_at": now_iso(),
        "currency": meta.get("currency", "USD"),
        "models": models,
        "plans": [],
    }


def parse_anthropic_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic docs (platform.claude.com/docs) renders pricing as a <table>
    with columns: Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes
    | Cache Hits & Refreshes | Output Tokens.

    The parser targets the table whose header contains both 'Base Input' and
    'Output Tokens'; that signature is stable across pricing revisions.
    """
    tables = extract_html_tables(content)
    table = find_table(tables, "Base Input", "Output Tokens")
    if not table:
        raise ValueError("anthropic_html: no model-pricing table found (header must contain 'Base Input' + 'Output Tokens')")
    headers = [h.lower() for h in table["headers"]]
    col_model = next((i for i, h in enumerate(table["headers"]) if h.strip().lower() == "model"), None)
    col_input = next((i for i, h in enumerate(headers) if "base input" in h), None)
    col_output = next((i for i, h in enumerate(headers) if "output tokens" in h), None)
    if None in (col_model, col_input, col_output):
        raise ValueError(f"anthropic_html: required columns missing (model={col_model}, input={col_input}, output={col_output})")

    models: List[Dict[str, Any]] = []
    for row in table["rows"]:
        if len(row) <= max(col_model, col_input, col_output):
            continue
        name = re.sub(r"\s*\[[^\]]+\]", "", row[col_model]).strip()
        in_price = first_price_in(row[col_input])
        out_price = first_price_in(row[col_output])
        if not (name and in_price is not None and out_price is not None):
            continue
        # Context window inference: Fable 5 / Mythos 5 / Opus 4.6+ / Sonnet 4.6+ /
        # Sonnet 5 advertise 1M; everything else currently 200K.
        ctx = 1000000 if any(t in name.lower() for t in (
            "fable 5", "mythos 5", "opus 4.6", "opus 4.7", "opus 4.8",
            "sonnet 4.6", "sonnet 5",
        )) else 200000
        deprecated = "deprecated" in row[col_model].lower() or "retired" in row[col_model].lower()
        models.append({
            "id": _model_id(name),
            "display_name": name,
            "context_window": ctx,
            "input_price_per_mtok": in_price,
            "output_price_per_mtok": out_price,
            "deprecated": deprecated,
            "notes": _trim(row[col_model]),
        })
    if not models:
        raise ValueError("anthropic_html: parsed zero models; column order may have shifted")
    return _payload(meta, models)


def parse_openai_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI pricing page renders many tables; column shape varies by section.

    Two layouts are common:
      Layout A — gpt-5.x series comparison (Short context | Long context):
        Headers: ['', 'Short context', 'Long context']
        Sub-header row: ['Model', 'Input', 'Cached input', 'Cache writes',
                         'Output', 'Input', 'Cached input', 'Cache writes', 'Output']
        Data row:      ['gpt-5.5', '$5.00', '$0.50', '$6.25', '$30.00', '$10.00', ...]

      Layout B — category model list (ChatGPT / Codex / etc.):
        Headers: ['Category', 'Model', 'Input', 'Cached input', 'Output']
        Data row: ['ChatGPT', 'chat-latest', '$5.00', '$0.50', '$30.00']

    The parser is column-header-aware: it locates the row containing
    'Input' / 'Output' tokens, then reads those columns regardless of
    Layout A vs B. It also skips auxiliary rows (multi-modal sub-rows,
    Categories without model ids) to avoid duplicating or polluting the list.
    """
    tables = extract_html_tables(content)
    if not tables:
        raise ValueError("openai_html: no tables on page")

    models: List[Dict[str, Any]] = []
    seen: set = set()

    def _is_model_id(cell: str) -> bool:
        c = cell.strip()
        if not c:
            return False
        if c.lower() in {"model", "category", "tool", "use case", "modality", "size"}:
            return False
        # OpenAI model ids are alphanumeric+hyphen+. — e.g. gpt-5.5, o4-mini, gpt-realtime-2.1.
        return bool(re.match(r"^[a-z][a-z0-9.\-]+$", c))

    for t in tables:
        rows = t["rows"]
        if not rows:
            continue

        # Find the column-header row: contains the words "Model" (or "Input"/"Output").
        col_header_idx: Optional[int] = None
        for i, r in enumerate(rows):
            joined = " ".join(c.strip().lower() for c in r)
            # Layout A pattern: row contains both "Input" and "Output" verbatim.
            # Layout B pattern: row contains "Model" and at least one of ("Input", "Output").
            if ("input" in joined and "output" in joined) or "model" in joined:
                if any(c.strip().lower() == "model" for c in r):
                    col_header_idx = i
                    break
                if "input" in joined and "output" in joined:
                    col_header_idx = i
                    break
        if col_header_idx is None:
            continue

        col_header = [c.strip().lower() for c in rows[col_header_idx]]
        col_model = next((i for i, h in enumerate(col_header) if h == "model"), None)
        col_input = next((i for i, h in enumerate(col_header) if h == "input"), None)
        col_output = next((i for i, h in enumerate(col_header) if h == "output"), None)
        if None in (col_model, col_input, col_output):
            continue

        for r in rows[col_header_idx + 1:]:
            if len(r) <= max(col_model, col_input, col_output):
                continue
            mid = r[col_model].strip()
            if not _is_model_id(mid) or mid in seen:
                continue
            in_price = first_price_in(r[col_input])
            out_price = first_price_in(r[col_output])
            if in_price is None or out_price is None:
                continue
            seen.add(mid)
            ctx = 400000 if mid.startswith(("gpt-", "chat-", "o4-")) else 200000
            models.append({
                "id": _model_id(mid),
                "display_name": mid,
                "context_window": ctx,
                "input_price_per_mtok": in_price,
                "output_price_per_mtok": out_price,
                "deprecated": False,
                "notes": "",
            })

    if not models:
        raise ValueError("openai_html: parsed zero models — page structure may have changed")
    return _payload(meta, models)


def parse_minimax_md(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """MiniMax docs/guides/pricing pages are markdown-with-JSX tables.

    Quirks handled here (shape-based detection — no language-dependent
    header matching):
      - Strikethrough price pairs like `~~4.20~~ 2.10` — we keep the new
        (current) price by extracting the LAST number in the cell.
      - Multiple tables across `<Tabs>` (standard vs priority tier) and
        `<Accordion>` (historical model section). We merge them all.
      - A model id may appear twice (e.g. MiniMax-M3 with standard AND
        priority tiers). We keep both tiers via a tier suffix on `notes`.

    Currency is CNY — provider metadata `currency` field controls the
    unit. The fixture at
    ``skills/llm-refresh/tests/fixtures/minimax_pricing.md`` is the
    English-translated oracle; when refreshing against the live
    page, only the column-header text differs.
    """
    # Pass 1: walk the raw content and tag each line with the section
    # it belongs to. Sections are detected from JSX component markers
    # captured BEFORE the JSX tags are stripped, so we keep the
    # semantic boundary information that the parser needs.
    raw_lines = content.splitlines()
    section_for_line: List[str] = ["standard"] * len(raw_lines)
    current = "standard"
    for i, line in enumerate(raw_lines):
        # `<Tab title="Standard">` and `<Tab title="Priority*">` are
        # captured here. JSX regex below keeps the *content* of titles.
        m = re.search(r'<Tab\s+title="([^"]+)"', line)
        if m:
            title = m.group(1).strip().rstrip("*").lower()
            if "priority" in title:
                current = "priority"
            elif "standard" in title:
                current = "standard"
        if "Historical Models" in line or "historical" in line.lower():
            # The accordion tag stays in the line, but we want the
            # section label regardless.
            if "<Accordion" in line or re.search(r"<Accordion[^>]*title=.historical.", line, re.IGNORECASE):
                current = "historical"
        section_for_line[i] = current

    # Strip JSX (but keep the title text we already captured — replacing
    # `<Tab title="Standard">` with `## Standard`-style markers is not
    # needed; section info was captured above). Per-line stripping is
    # intentional: a regex with DOTALL on multi-line input would collapse
    # newlines and break the line-index alignment used below.
    text = "\n".join(
        re.sub(r"</?(?:Tabs?|Tab|Accordion|Info|Tip|Warning|CodeGroup|CodeBlock)\b[^>]*>", " ", ln)
        for ln in re.sub(r"<br\s*/?>", " ", content, flags=re.IGNORECASE).splitlines()
    )
    text = re.sub(r"<span[^>]*>.*?</span>", " ", text, flags=re.IGNORECASE | re.DOTALL)

    models: List[Dict[str, Any]] = []
    seen_keys: set = set()
    for idx, raw in enumerate(text.splitlines()):
        if "|" not in raw:
            continue

        cells = [_clean_md_cell(c) for c in raw.split("|")]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue

        mid = cells[0].strip()
        if not mid.lower().startswith("MiniMax".lower()):
            continue

        in_price = _last_price_in(cells[1] if len(cells) > 1 else "")
        out_price = _last_price_in(cells[2] if len(cells) > 2 else "")
        if in_price is None or out_price is None:
            continue

        band = ""
        band_m = re.search(r"(≤|>|>=|<=)\s*(\d+)\s*([km])", cells[0])
        if band_m:
            band = f"-{band_m.group(1)}{band_m.group(2)}{band_m.group(3)}"

        section = section_for_line[idx] if idx < len(section_for_line) else "standard"
        suffix = "highspeed" if "highspeed" in mid.lower() else ""
        if section == "priority" and not suffix:
            tier_label = "priority"
        elif section == "historical" and not suffix:
            tier_label = "historical"
        else:
            tier_label = ""
        key = _model_id(mid) + band + (("-" + tier_label) if tier_label else "")
        if key in seen_keys:
            continue
        seen_keys.add(key)

        ctx = 245000
        if "M3" in mid and band_m:
            ctx = 512000 if band_m.group(1) == "≤" else 1000000
        elif "M3" in mid:
            ctx = 512000
        if not ctx or ctx == 245000:
            ctx_text = cells[4] if len(cells) > 4 else ""
            ctx_parsed = _parse_ctx_to_int(ctx_text)
            if ctx_parsed > 1000:
                ctx = ctx_parsed

        notes = " ".join(cells[3:]) if len(cells) > 3 else ""
        if tier_label and tier_label not in notes.lower():
            notes = f"{tier_label} — {notes}" if notes else tier_label
        if suffix and suffix not in notes.lower():
            notes = f"{notes} ({suffix})" if notes else suffix

        models.append({
            "id": key,
            "display_name": mid,
            "context_window": ctx,
            "input_price_per_mtok": in_price,
            "output_price_per_mtok": out_price,
            "deprecated": tier_label == "historical",
            "notes": _trim(notes)[:200],
        })
    if not models:
        raise ValueError("minimax_md: parsed zero models — pricing-paygo.md page structure may have changed")
    return _payload(meta, models)


def _last_price_in(cell: str) -> Optional[float]:
    """Return the LAST numeric value in a cell, ignoring strikethrough prefixes.

    MiniMax renders price changes as `~~old~~ new`. The current price is the
    number AFTER the strikethrough; we keep the last number to capture that.
    """
    if not cell:
        return None
    nums = re.findall(r"([0-9]+(?:\.[0-9]+)?)", cell)
    if not nums:
        return None
    try:
        return float(nums[-1])
    except ValueError:
        return None


def _clean_md_cell(raw: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", raw)
    cleaned = re.sub(r"[*_`~]+", "", cleaned)  # strip markdown formatting chars
    return _WS_RE.sub(" ", cleaned).strip()


def _parse_ctx_to_int(text: str) -> int:
    if not text:
        return 200000
    m = re.search(r"(\d+)\s*([km]?)(?:b)?", text.lower())
    if not m:
        return 200000
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "k":
        return n * 1000
    if unit == "m":
        return n * 1_000_000
    if unit == "b":
        return n * 1_000_000_000
    return n


def parse_deepseek_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """DeepSeek api-docs (Docusaurus) renders one comparison table whose first
    column is the row label and the next two columns are the two models
    (deepseek-v4-flash / deepseek-v4-pro) being compared. The model names live
    in the TABLE HEADER row (column 1 / column 2).

    Pricing rows we need:
        PRICING section header (cell-0) — followed by three sub-rows.
        1M INPUT TOKENS (CACHE HIT)   -> $[flash_hit], $[pro_hit]
        1M INPUT TOKENS (CACHE MISS)  -> $[flash_in],  $[pro_in]
        1M OUTPUT TOKENS              -> $[flash_out], $[pro_out]

    Docusaurus quirks to handle:
      - The first pricing sub-row (CACHE HIT) sometimes rides on the same
        <tr> as the "PRICING" rowspan parent. So when cell-0 == "PRICING"
        and there are >=3 trailing cells, we accept the cache-hit label and
        dollar values from that row.
    """
    tables = extract_html_tables(content)
    table = tables[0] if tables else None
    if not table or len(table["headers"]) < 3:
        raise ValueError("deepseek_html: comparison table missing or has fewer than 3 columns")

    def _clean_id(raw: str) -> str:
        return re.sub(r"\s*\(\d+\)", "", raw).strip()

    flash_id = _clean_id(table["headers"][1])
    pro_id = _clean_id(table["headers"][2])
    if not flash_id or not pro_id:
        raise ValueError("deepseek_html: model names missing from header row")

    def _row_label_upper(row: List[str]) -> str:
        return row[0].strip().upper() if row else ""

    def _row_cells(row: List[str]) -> Tuple[Optional[str], Optional[str]]:
        if len(row) >= 3:
            return row[1].strip(), row[2].strip()
        return None, None

    # Walk rows. When cell-0 == "PRICING", peek for an embedded cache-hit row.
    pricing: Dict[str, Tuple[str, str]] = {}
    pricing_row_idx: Optional[int] = None
    for i, r in enumerate(table["rows"]):
        if not r:
            continue
        if _row_label_upper(r) == "PRICING":
            pricing_row_idx = i
            if len(r) >= 4 and "CACHE HIT" in r[1].strip().upper():
                # cells: ['PRICING', '1M INPUT TOKENS (CACHE HIT)', '$0.0028', '$0.003625']
                pricing[r[1].strip().upper()] = (r[2].strip(), r[3].strip())
            break

    # Continue collecting rows after the PRICING label for cache-miss + output.
    if pricing_row_idx is not None:
        for r in table["rows"][pricing_row_idx + 1:]:
            if not r:
                continue
            label = _row_label_upper(r)
            if not label.startswith("1M"):
                # Stop scanning once we hit a non-pricing sub-row.
                break
            v1, v2 = _row_cells(r)
            if v1 is not None and v2 is not None:
                pricing[label] = (v1, v2)

    cache_hit = pricing.get("1M INPUT TOKENS (CACHE HIT)")
    cache_miss = pricing.get("1M INPUT TOKENS (CACHE MISS)")
    output = pricing.get("1M OUTPUT TOKENS")
    if not (cache_miss and output):
        raise ValueError(f"deepseek_html: missing one of cache_miss/output (have keys: {list(pricing)})")

    flash_in = float(cache_miss[0].lstrip("$"))
    pro_in = float(cache_miss[1].lstrip("$"))
    flash_out = float(output[0].lstrip("$"))
    pro_out = float(output[1].lstrip("$"))
    notes_hit = ""
    if cache_hit:
        try:
            flash_hit = float(cache_hit[0].lstrip("$"))
            pro_hit = float(cache_hit[1].lstrip("$"))
            notes_hit = f"Cache hit: ${flash_hit}/MTok and ${pro_hit}/MTok."
        except ValueError:
            pass

    return _payload(meta, [
        {
            "id": flash_id,
            "display_name": "DeepSeek-V4-Flash",
            "context_window": 1000000,
            "input_price_per_mtok": flash_in,
            "output_price_per_mtok": flash_out,
            "deprecated": False,
            "notes": f"Max output 384k. {notes_hit}".strip(),
        },
        {
            "id": pro_id,
            "display_name": "DeepSeek-V4-Pro",
            "context_window": 1000000,
            "input_price_per_mtok": pro_in,
            "output_price_per_mtok": pro_out,
            "deprecated": False,
            "notes": f"Max output 384k. {notes_hit}".strip(),
        },
    ])


def _model_id(display: str) -> str:
    """Slugify 'Claude Fable 5' -> 'claude-fable-5'."""
    return re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")


def _trim(text: str) -> str:
    """Compress an annotation paragraph into one short note (<= 200 chars)."""
    s = _WS_RE.sub(" ", text).strip()
    return s[:200]


PARSERS: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {
    "anthropic_html": parse_anthropic_html,
    "openai_html": parse_openai_html,
    "minimax_md": parse_minimax_md,
    "deepseek_html": parse_deepseek_html,
}


# ---------- IO ----------

def load_sources(root: Path, sources_path: Optional[Path] = None) -> Dict[str, Any]:
    path = sources_path or (root / "docs" / "llm-info" / "sources.json")
    if not path.exists():
        print(f"error: sources.json not found: {path}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: sources.json is not valid JSON ({path}): {exc}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def load_existing(root: Path, provider_id: str) -> Optional[Dict[str, Any]]:
    path = root / "docs" / "llm-info" / f"{provider_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_payload(root: Path, payload: Dict[str, Any]) -> Path:
    path = root / "docs" / "llm-info" / f"{payload['provider']}.json"
    atomic_write_json(path, payload)
    return path


def diff_payloads(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> str:
    before_text = (
        json.dumps(before, indent=2, sort_keys=True, ensure_ascii=False) if before else ""
    )
    after_text = json.dumps(after, indent=2, sort_keys=True, ensure_ascii=False)
    diff = difflib.unified_diff(
        before_text.splitlines(),
        after_text.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
    )
    return "\n".join(diff)


# ---------- CLI subcommands ----------

def _provider_filter(providers: List[Dict[str, Any]], requested: Optional[str]) -> List[Dict[str, Any]]:
    if not requested:
        return providers
    filtered = [p for p in providers if p["id"] == requested]
    if not filtered:
        known = ", ".join(p["id"] for p in providers)
        print(f"error: provider '{requested}' not in sources.json (known: {known})", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return filtered


def cmd_fetch_fixture(provider_id: str, fixture_path: Path) -> int:
    """Parse a locally-saved HTML/MD file with the configured parser. No network."""
    root = _project_root()
    sources = load_sources(root)
    match = next((p for p in sources["providers"] if p["id"] == provider_id), None)
    if not match:
        print(f"error: provider '{provider_id}' not in sources.json", file=sys.stderr)
        return EXIT_USAGE
    content = fixture_path.read_text(encoding="utf-8")
    parser_kind = match.get("parser", "")
    if parser_kind not in PARSERS:
        print(f"error: unknown parser '{parser_kind}' for provider '{provider_id}'", file=sys.stderr)
        return EXIT_FETCH
    try:
        payload = PARSERS[parser_kind](content, match)
    except Exception as exc:  # noqa: BLE001
        print(f"[{provider_id}] FAIL: {exc}", file=sys.stderr)
        return EXIT_FETCH
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh docs/llm-info/<provider>.json from each vendor's official pricing page.",
    )
    parser.add_argument("--provider", help="One provider id (e.g. claude); default = all four.")
    parser.add_argument("--check", action="store_true", help="Diff only; never write. Exit 1 on diff.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary.")
    parser.add_argument("--sources", help="Override sources.json path (for testing).")
    parser.add_argument("--fetch-fixture", nargs=2, metavar=("PROVIDER", "FILE"),
                        help="Parse a locally-saved page (debug helper, no network).")
    parsed = parser.parse_args(argv)

    if parsed.fetch_fixture:
        return cmd_fetch_fixture(parsed.fetch_fixture[0], Path(parsed.fetch_fixture[1]))

    root = _project_root()
    sources_path = Path(parsed.sources).resolve() if parsed.sources else None
    sources = load_sources(root, sources_path)
    providers = _provider_filter(sources.get("providers", []), parsed.provider)

    summary: Dict[str, Any] = {}
    overall = EXIT_OK
    for src in providers:
        pid = src["id"]
        parser_kind = src.get("parser", "")
        if parser_kind not in PARSERS:
            overall = max(overall, EXIT_FETCH)
            print(f"[{pid}] FAIL: unknown parser '{parser_kind}'", file=sys.stderr)
            summary[pid] = {"error": f"unknown parser '{parser_kind}'"}
            continue

        try:
            content = fetch_url(src["url"])
            payload = PARSERS[parser_kind](content, src)
        except Exception as exc:  # noqa: BLE001 - any fetch/parse failure under one banner
            overall = max(overall, EXIT_FETCH)
            print(f"[{pid}] FAIL: {exc}", file=sys.stderr)
            summary[pid] = {"error": str(exc)}
            continue

        existing = load_existing(root, pid)
        changed = existing != payload
        target = root / "docs" / "llm-info" / f"{pid}.json"

        if parsed.check:
            if changed:
                overall = max(overall, EXIT_DIFF)
                if not parsed.json:
                    print(f"--- {pid} (changed) ---")
                    print(diff_payloads(existing, payload))
            elif not parsed.json:
                print(f"[{pid}] no change")
        elif changed:
            write_payload(root, payload)
            if not parsed.json:
                print(f"[{pid}] wrote {target} ({len(payload['models'])} models)")
        elif not parsed.json:
            print(f"[{pid}] no change")

        summary[pid] = {
            "changed": changed,
            "model_count": len(payload["models"]),
            "fetched_at": payload["fetched_at"],
        }

    if parsed.json:
        print(json.dumps({"summary": summary, "exit": overall}, indent=2, sort_keys=True, ensure_ascii=False))
    return overall


if __name__ == "__main__":
    sys.exit(main())
