#!/usr/bin/env python3
"""skill_usage.py -- per-skill usage telemetry over logs/**/*.jsonl.

Two distinct signals:

* ``turns`` -- count of assistant messages that carry a top-level
  ``attributionSkill`` field. This is *depth / work done* by the skill:
  one slash-command kick can produce many turns if the skill orchestrates
  sub-agents or iterates.

* ``invocations`` -- count of explicit ``Skill`` ``tool_use`` blocks
  whose ``input.skill`` names the skill. This is *distinct human kicks*:
  the user (or another skill) explicitly asked for the skill to run.

The same skill name in both signals is what the harness-thesis audit
used to drive cut/merge calls. Keeping it standing lets future audits
skip the manual aggregation step. High turns + low invocations means a
babysitter / maintenance loop (probably keep); both low means a prune
candidate; high turns + high invocations means a heavy hitter.

Workspace attribution is captured per ``cwd`` so target-project skill
usage is separable from self-dev usage -- critical in this repo where
self-dev log volume dominates.

Stdlib only.

Usage::

    python3 tools/skill_usage.py --days 30            # table to stdout
    python3 tools/skill_usage.py --days 30 --json     # machine-readable
    python3 tools/skill_usage.py --cwd /repo/x        # one workspace only
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

# Normalize + flatten live in their own module; this file keeps the
# aggregator + CLI. Re-export the public names so callers (and tests)
# can keep importing ``skill_usage._iter_tool_uses`` etc.
from skill_usage_normalize import (  # noqa: E402  (sys.path set in main)
    NormalizedUsage,
    _flatten_block_list,
    _iter_tool_uses,
    _normalize_usage_record,
    _parse_iso,
    _unwrap_blocks,
)
from skill_usage_render import (  # noqa: E402
    _discover_catalog_skills,
    _run_propose_delete,
    _seed_from_catalog,
    format_json,
    format_table,
)
from skill_usage_render import (
    filter_by_cwd_prefix as _filter_by_cwd_prefix_impl,
)

# Public re-exports. Tests import these via ``skill_usage.X``; ruff
# treats re-exports listed in __all__ as intentional and skips the
# F401 "unused import" check on them.
__all__ = [
    # normalize helpers (kept for test surface)
    "_iter_tool_uses", "_unwrap_blocks", "_flatten_block_list",
    "NormalizedUsage", "_normalize_usage_record", "_parse_iso",
    # render + propose-delete
    "format_table", "format_json", "filter_by_cwd_prefix",
    "_run_propose_delete", "_seed_from_catalog", "_discover_catalog_skills",
    # aggregator
    "aggregate_skill_usage",
]

# Default discovery root: <repo>/logs/{claude-code,codex}/**/*.jsonl.
# Matches the capture layout written by tools/save_log.py so the tool
# works on a fresh checkout without any extra wiring.
_DEFAULT_LOGS_GLOB = "logs/claude-code/**/*.jsonl"


def _within_window(ts: _dt.datetime | None, cutoff: _dt.datetime | None) -> bool:
    if cutoff is None:
        return True
    if ts is None:
        return False
    return ts >= cutoff


def _expand_braces(pattern: str) -> list[str]:
    """Expand a single ``{a,b,c}`` alternative group into multiple patterns.

    Only one group is expanded per call; nested braces are not handled (the
    current call sites use exactly one group). Returns ``[pattern]``
    unchanged when no group is present.
    """
    open_idx = pattern.find("{")
    if open_idx < 0:
        return [pattern]
    close_idx = pattern.find("}", open_idx)
    if close_idx < 0:
        return [pattern]
    prefix = pattern[:open_idx]
    suffix = pattern[close_idx + 1:]
    alts = pattern[open_idx + 1:close_idx].split(",")
    return [prefix + alt + suffix for alt in alts]


def _cwd_matches(cwd: str, prefix: str) -> bool:
    """True iff ``cwd`` equals ``prefix`` or starts with ``prefix + '/'``.

    Prevents over-matching: ``/repo/a`` must not match ``/repo/a-old``
    when ``prefix`` is ``/repo/a``.
    """
    norm = prefix.rstrip("/")
    if not norm:
        return True
    return cwd == norm or cwd.startswith(norm + "/")


def _iter_logs(logs_glob: str):
    """Yield every .jsonl matching the glob. Handles both:

    * ``logs/claude-code/**/*.jsonl`` -- recursive bash glob pattern.
    * ``logs/{claude-code,codex}/**/*.jsonl`` -- brace alternative.
    * A literal file path (one log).

    Bash globs are expanded by the shell before the Python process sees
    them, so the literal-file fallback only matters when the caller
    passed a single path without shell expansion. Braces are not
    expanded by ``Path.rglob``, so they are handled explicitly.
    """
    for pat in _expand_braces(logs_glob):
        p = Path(pat)
        if p.is_file():
            yield p
            continue
        if any(ch in pat for ch in "*?["):
            anchor = pat.split("*", 1)[0].rstrip("/")
            anchor_path = Path(anchor) if anchor else Path(".")
            if anchor_path.is_dir():
                for path in anchor_path.rglob("*.jsonl"):
                    if path.is_file():
                        yield path
            continue
        base = Path(pat)
        if base.is_dir():
            for path in base.rglob("*.jsonl"):
                if path.is_file():
                    yield path


def _ensure_skill(skills: dict, name: str, *, include_per_cwd: bool) -> dict:
    rec = skills.get(name)
    if rec is None:
        rec = {"turns": 0, "invocations": 0, "last_seen": None}
        if include_per_cwd:
            rec["cwds"] = {}
        skills[name] = rec
    return rec


def _bump_last_seen(rec: dict, ts_str: str) -> None:
    cur = rec.get("last_seen")
    if cur is None or (ts_str and ts_str > cur):
        rec["last_seen"] = ts_str


def _bump_cwd(rec: dict, cwd: str, *, turns: int, invocations: int,
              ts_str: str) -> None:
    cwds = rec.setdefault("cwds", {})
    bucket = cwds.get(cwd)
    if bucket is None:
        bucket = {"turns": 0, "invocations": 0, "last_seen": None}
        cwds[cwd] = bucket
    bucket["turns"] += turns
    bucket["invocations"] += invocations
    if ts_str and (bucket["last_seen"] is None or ts_str > bucket["last_seen"]):
        bucket["last_seen"] = ts_str


# Public re-export of filter_by_cwd_prefix. The render module injects
# our ``_cwd_matches`` so it stays free of cross-module dependencies.
def filter_by_cwd_prefix(skills: dict[str, dict], cwd_prefix: str) -> dict[str, dict]:
    """See :func:`skill_usage_render.filter_by_cwd_prefix` for the
    contract. This wrapper binds the cwd-matcher from this module."""
    return _filter_by_cwd_prefix_impl(skills, cwd_prefix,
                                      _cwd_matches=_cwd_matches)


def aggregate_skill_usage(logs_glob: str,
                          window_days: int | None = 30,
                          *,
                          cwd_prefix: str | None = None,
                          include_per_cwd: bool = False,
                          now: _dt.datetime | None = None,
                          ) -> dict[str, dict]:
    """Aggregate ``attributionSkill`` (turns) and explicit ``Skill``
    ``tool_use`` (invocations) across every jsonl matching ``logs_glob``.

    Returns ``{skill_name: {"turns": int, "invocations": int,
    "last_seen": iso_ts | None, [optional] "cwds": {cwd: {...}}}}``.

    Malformed lines and lines missing a timestamp are silently dropped
    -- the analyzer is read-only over captured logs and must never raise
    on a single bad record. Decoding + shape normalization is delegated
    to :func:`_normalize_usage_record` so the inner loop only sees
    uniform :class:`NormalizedUsage` values regardless of whether the
    record came from Claude-Code (top-level ``timestamp`` /
    ``message.content``) or Codex (``payload.timestamp`` /
    ``payload.tool_uses``).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff: _dt.datetime | None = None
    if window_days is not None:
        cutoff = now - _dt.timedelta(days=window_days)

    skills: dict[str, dict] = {}

    for path in _iter_logs(logs_glob):
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                norm = _normalize_usage_record(obj)

                if cwd_prefix and not _cwd_matches(norm.cwd, cwd_prefix):
                    continue
                if not _within_window(norm.ts, cutoff):
                    continue

                if norm.skill:
                    rec = _ensure_skill(skills, norm.skill,
                                        include_per_cwd=include_per_cwd)
                    rec["turns"] += 1
                    _bump_last_seen(rec, norm.ts_str)
                    if include_per_cwd:
                        _bump_cwd(rec, norm.cwd, turns=1, invocations=0,
                                  ts_str=norm.ts_str)

                for name in norm.skill_invocations:
                    rec = _ensure_skill(skills, name,
                                        include_per_cwd=include_per_cwd)
                    rec["invocations"] += 1
                    _bump_last_seen(rec, norm.ts_str)
                    if include_per_cwd:
                        _bump_cwd(rec, norm.cwd, turns=0, invocations=1,
                                  ts_str=norm.ts_str)
        finally:
            fh.close()

    return skills


def _default_logs_glob() -> str:
    """Pick the right default glob: include ``codex/`` when present.

    Both log sources use the same on-disk schema so the analyzer handles
    either; defaulting to claude-code alone (the heavier source in this
    repo) keeps the table quick on a single-CLI machine.
    """
    here = Path.cwd()
    if (here / "logs" / "codex").is_dir():
        return "logs/{claude-code,codex}/**/*.jsonl"
    return _DEFAULT_LOGS_GLOB


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Per-skill usage telemetry (turns + invocations) "
                    "over logs/**/*.jsonl.")
    p.add_argument("--logs-glob", default=None,
                   help="Glob for log files (default: ./logs/**/...)")
    p.add_argument("--days", type=int, default=30,
                   help="only count turns/invocations within the last N days "
                        "(default 30; pass 0 to disable the window)")
    p.add_argument("--cwd", default=None, metavar="PREFIX",
                   help="only count lines whose cwd starts with PREFIX")
    p.add_argument("--top", type=int, default=20,
                   help="show only the top N skills (default 20; 0 = all)")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of a table")
    p.add_argument("--per-cwd", action="store_true",
                   help="include a per-cwd breakdown in the JSON output")
    p.add_argument("--propose-delete", action="store_true",
                   help="filter to skills with 0 turns AND 0 invocations in "
                        "the window and pipe the list to "
                        "skills/prune-propose/scripts/dump_usage.py for a "
                        "per-skill delete proposal loop")
    p.add_argument("--dry-run", action="store_true",
                   help="with --propose-delete, print the candidate table "
                        "only and skip the AskUserQuestion loop")
    args = p.parse_args(argv)

    logs_glob = args.logs_glob or _default_logs_glob()
    window = None if args.days == 0 else args.days
    skills = aggregate_skill_usage(logs_glob, window,
                                   cwd_prefix=args.cwd,
                                   include_per_cwd=args.per_cwd)

    if args.propose_delete:
        # Skip the "no skills found" bail below: propose-delete seeds
        # from the on-disk catalog too, so an empty telemetry dict
        # (fresh checkout, no logs yet) is still a valid input -- every
        # catalog skill is a candidate in that case.
        return _run_propose_delete(skills, window, dry_run=args.dry_run,
                                   here=Path(__file__).resolve().parent)

    if not skills:
        print(f"[skill-usage] no skills found under {logs_glob}",
              file=sys.stderr)
        return 0

    if args.json:
        print(format_json(skills))
    else:
        top = None if args.top == 0 else args.top
        print(format_table(skills, top=top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
