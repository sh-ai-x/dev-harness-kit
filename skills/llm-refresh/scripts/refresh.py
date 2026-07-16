#!/usr/bin/env python3
"""refresh.py — refresh docs/llm-info/<provider>.json from each vendor's
official pricing page.

Usage:
    python3 skills/llm-refresh/scripts/refresh.py [--provider ID] [--check] [--json]

Why this exists instead of inline WebFetch:
    The dev-kit plugin policy disallows WebFetch (see lib/llm_judge.py for the
    only positive HTTP caller precedent). This script mirrors that pattern:
    `urllib.request.urlopen` from a single Bash-invoked Python entry, so the
    SKILL.md stays WebFetch-free and the network call lives in code.

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
from typing import Any, Callable, Dict, List, Optional

# Re-use the plugin's POSIX-atomic write helper (lib/atomic.py). The script
# walks four parents up so it can be invoked from any cwd as long as it
# remains inside the plugin checkout or its worktrees.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parents[3] / "lib"))

from atomic import atomic_write_json, now_iso  # type: ignore  # noqa: E402

USER_AGENT = "dev-kit-llm-refresh/1.0 (+https://github.com/sh-ai-x/dev-harness-kit)"
DEFAULT_TIMEOUT_S = 15
DEFAULT_RETRIES = 1
EXIT_OK = 0
EXIT_DIFF = 1
EXIT_FETCH = 2
EXIT_USAGE = 3

# ---------- project root ----------

def _project_root() -> Path:
    """Resolve the project root from the script location, falling back to cwd.

    The script lives at `<root>/skills/llm-refresh/scripts/refresh.py`. The
    root is recognised by the presence of `.claude-plugin/plugin.json`. When
    the script is invoked from inside a worktree (`.worktrees/<name>/`), the
    worktree path IS the root because each worktree has its own plugin.json.
    """
    candidate = _THIS.parents[3]
    if (candidate / ".claude-plugin" / "plugin.json").exists():
        return candidate
    cwd_candidate = Path.cwd()
    if (cwd_candidate / ".claude-plugin" / "plugin.json").exists():
        return cwd_candidate
    return candidate


# ---------- fetch ----------

def fetch_url(url: str, *, timeout: int = DEFAULT_TIMEOUT_S, retries: int = DEFAULT_RETRIES) -> str:
    """Fetch `url` via urllib; raise RuntimeError after `retries` consecutive failures."""
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - official public pricing pages only
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
    raise RuntimeError(f"fetch failed after {retries + 1} attempt(s): {url}: {last}")


# ---------- parsers ----------
#
# Each parser returns a payload matching the schema in docs/llm-info/README.md:
#   {provider, label, source_url, fetched_at, currency, models[], plans[]}
# Pages are owned by their vendors; if a page's HTML structure drifts, the
# parser raises ValueError with an explicit message. Per set-provider.sh
# precedent, silent fallback to "structure preserved" is the bug we avoid.

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    """Replace every HTML tag with a space so regex matchers work on plain text."""
    return _TAG_RE.sub(" ", html)


def _model_id(display_name: str) -> str:
    """Slugify 'Claude Opus 4.8' -> 'claude-opus-4-8'."""
    return re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")


def _payload(meta: Dict[str, Any], models: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "provider": meta["id"],
        "label": meta["label"],
        "source_url": meta["url"],
        "fetched_at": now_iso(),
        "currency": meta.get("currency", "USD"),
        "models": models,
        "plans": [],  # subscription plans are curated manually; pages do not advertise the same schema
    }


def parse_anthropic_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic publishes a per-model block: name then two Mtok prices."""
    text = _strip_tags(content)
    pat = re.compile(
        r"(?:^|\n)\s*(Claude\s+(?:Opus|Sonnet|Haiku)\s+[0-9][\w.\-]*)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE | re.DOTALL,
    )
    models: List[Dict[str, Any]] = []
    for m in pat.finditer(text):
        name = m.group(1).strip()
        models.append({
            "id": _model_id(name),
            "display_name": name,
            "context_window": 200000,
            "input_price_per_mtok": float(m.group(2)),
            "output_price_per_mtok": float(m.group(3)),
            "deprecated": False,
            "notes": "",
        })
    if not models:
        raise ValueError("anthropic_html: no models parsed; page structure drifted (re-tune regex)")
    return _payload(meta, models)


def parse_openai_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI lists each model with input/output token prices per Mtok."""
    text = _strip_tags(content)
    pat = re.compile(
        r"(?:^|\n)\s*((?:GPT-?[0-9]+(?:\s*mini)?|o[0-9]+(?:-mini)?))"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE | re.DOTALL,
    )
    models: List[Dict[str, Any]] = []
    for m in pat.finditer(text):
        name = m.group(1).strip()
        models.append({
            "id": re.sub(r"\s+", "-", name).lower(),
            "display_name": name,
            "context_window": 400000,
            "input_price_per_mtok": float(m.group(2)),
            "output_price_per_mtok": float(m.group(3)),
            "deprecated": False,
            "notes": "",
        })
    if not models:
        raise ValueError("openai_html: no models parsed; page structure drifted (re-tune regex)")
    return _payload(meta, models)


def parse_minimax_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """MiniMax lists each model with input/output token prices per Mtok."""
    text = _strip_tags(content)
    pat = re.compile(
        r"(?:^|\n)\s*(MiniMax-[A-Za-z0-9\[\]0-9]+)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)",
    )
    models: List[Dict[str, Any]] = []
    for m in pat.finditer(text):
        name = m.group(1).strip()
        models.append({
            "id": name,
            "display_name": name,
            "context_window": 1000000,
            "input_price_per_mtok": float(m.group(2)),
            "output_price_per_mtok": float(m.group(3)),
            "deprecated": False,
            "notes": "",
        })
    if not models:
        raise ValueError("minimax_html: no models parsed; page structure drifted (re-tune regex)")
    return _payload(meta, models)


def parse_deepseek_html(content: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """DeepSeek lists deepseek-chat and deepseek-reasoner with Mtok prices."""
    text = _strip_tags(content)
    pat = re.compile(
        r"(?:^|\n)\s*(deepseek-[a-z]+)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)"
        r".{0,400}?\$([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE,
    )
    models: List[Dict[str, Any]] = []
    for m in pat.finditer(text):
        name = m.group(1).strip()
        models.append({
            "id": name,
            "display_name": name,
            "context_window": 64000,
            "input_price_per_mtok": float(m.group(2)),
            "output_price_per_mtok": float(m.group(3)),
            "deprecated": False,
            "notes": "",
        })
    if not models:
        raise ValueError("deepseek_html: no models parsed; page structure drifted (re-tune regex)")
    return _payload(meta, models)


PARSERS: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {
    "anthropic_html": parse_anthropic_html,
    "openai_html": parse_openai_html,
    "minimax_html": parse_minimax_html,
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
    """Unified diff for two payloads; empty string when equal."""
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


# ---------- main ----------

def _provider_filter(providers: List[Dict[str, Any]], requested: Optional[str]) -> List[Dict[str, Any]]:
    if not requested:
        return providers
    filtered = [p for p in providers if p["id"] == requested]
    if not filtered:
        known = ", ".join(p["id"] for p in providers)
        print(f"error: provider '{requested}' not in sources.json (known: {known})", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return filtered


def main(argv: Optional[List[str]] = None) -> int:
    args = argparse.ArgumentParser(
        description="Refresh docs/llm-info/<provider>.json from each vendor's official pricing page.",
    )
    args.add_argument("--provider", help="One provider id (e.g. claude); default = all four.")
    args.add_argument("--check", action="store_true", help="Diff only; never write. Exit 1 on diff.")
    args.add_argument("--json", action="store_true", help="Emit machine-readable summary.")
    args.add_argument("--sources", help="Override sources.json path (for testing).")
    parsed = args.parse_args(argv)

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
        except Exception as exc:  # noqa: BLE001 - report any fetch/parse failure under one banner
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
