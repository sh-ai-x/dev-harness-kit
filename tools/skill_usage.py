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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Default discovery root: <repo>/logs/{claude-code,codex}/**/*.jsonl.
# Matches the capture layout written by tools/save_log.py so the tool
# works on a fresh checkout without any extra wiring.
_DEFAULT_LOGS_GLOB = "logs/claude-code/**/*.jsonl"


def _parse_iso(ts: str) -> _dt.datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if not s:
        return None
    try:
        # Tolerate a trailing 'Z' that datetime.fromisoformat rejects on
        # older Python builds; replace with explicit UTC offset.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


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


def _iter_logs(logs_glob: str) -> Iterable[Path]:
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
            # Resolve to a concrete directory tree; ``glob.glob`` would not
            # honour ``**`` without ``recursive=True`` so we walk manually.
            anchor = pat.split("*", 1)[0].rstrip("/")
            anchor_path = Path(anchor) if anchor else Path(".")
            if anchor_path.is_dir():
                for path in anchor_path.rglob("*.jsonl"):
                    if path.is_file():
                        yield path
            continue
        # Treat as a directory.
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


def _iter_tool_uses(record: dict):
    """Yield ``tool_use`` blocks from a Claude-Code or Codex record.

    Claude-Code nests blocks under ``record.message.content`` (list of
    dicts, each with ``type=="tool_use"``). Codex nests blocks under
    ``record.payload`` (either a list of blocks or a dict carrying a
    ``tool_uses`` list). Some intermediate builds wrap blocks one
    level deeper (``content`` inside a wrapper dict). This normalizer
    flattens those shapes so the aggregation loop can iterate over a
    uniform sequence of blocks without branching on record origin.

    Non-dict entries, ``text`` blocks, and records with neither
    ``message`` nor ``payload`` are silently skipped -- the
    aggregator treats tool_use counts as a partial signal and any
    malformed block is the same as a missing one.
    """
    # Claude-Code shape: message.content is a list of blocks (or a
    # wrapper dict that itself carries a content list).
    msg = record.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    yield from _flatten_block_list(_unwrap_blocks(content))

    # Codex shape: payload may be a list, a dict with ``tool_uses``,
    # or nested one level deeper. Walk both layouts so future Codex
    # schemas keep working without touching the aggregator.
    payload = record.get("payload")
    if isinstance(payload, list):
        yield from _flatten_block_list(payload)
    elif isinstance(payload, dict):
        yield from _flatten_block_list(payload.get("tool_uses"))
        yield from _flatten_block_list(_unwrap_blocks(payload.get("content")))


def _unwrap_blocks(value):
    """Return ``value`` if it is a list of blocks; otherwise, if it
    is a dict that itself carries a ``content`` or ``tool_uses``
    list, return that inner list. Anything else returns ``None``."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        inner = value.get("content")
        if isinstance(inner, list):
            return inner
        inner = value.get("tool_uses")
        if isinstance(inner, list):
            return inner
    return None


def _flatten_block_list(items) -> Iterable[dict]:
    """Yield each dict in ``items`` whose ``type`` is ``"tool_use"``.

    Non-list inputs, non-dict members, and non-tool_use blocks are
    silently skipped. ``items`` may itself contain nested lists (rare
    but seen in Codex payloads); the loop recurses one level.
    """
    if not isinstance(items, list):
        return
    for blk in items:
        if isinstance(blk, list):
            yield from _flatten_block_list(blk)
            continue
        if not isinstance(blk, dict):
            continue
        if blk.get("type") != "tool_use":
            continue
        yield blk


@dataclass
class NormalizedUsage:
    """One record's worth of skill-usage signals, resolved across the
    Claude-Code and Codex wire shapes so the aggregator can iterate over
    a uniform sequence.

    Codex wraps several fields under ``payload.*`` while leaving
    ``attributionSkill`` at the top level. Reading only the top level
    (pre-refactor behavior) dropped records whose ``timestamp`` lived
    under ``payload`` -- silently undercounting used skills as deletion
    candidates. The normalizer walks both layers so a record contributes
    regardless of where its timestamp sits.
    """

    ts_str: str = ""
    ts: _dt.datetime | None = None
    cwd: str = ""
    skill: str = ""
    skill_invocations: list[str] = field(default_factory=list)


def _normalize_usage_record(record: dict) -> NormalizedUsage:
    """Resolve top-level vs ``payload.*`` fields into a uniform
    :class:`NormalizedUsage`. The aggregator consumes only this shape;
    nothing else in the file needs to know about Codex payload nesting."""
    payload = record.get("payload") if isinstance(record, dict) else None
    payload_dict = payload if isinstance(payload, dict) else None

    ts_str = record.get("timestamp") or ""
    if not ts_str and payload_dict is not None:
        ts_str = payload_dict.get("timestamp") or ""

    cwd = record.get("cwd") or ""
    if not cwd and payload_dict is not None:
        cwd = payload_dict.get("cwd") or ""

    skill = record.get("attributionSkill")
    skill = skill if isinstance(skill, str) and skill else ""

    invocations: list[str] = []
    for blk in _iter_tool_uses(record):
        if blk.get("name") != "Skill":
            continue
        inp = blk.get("input") or {}
        name = inp.get("skill")
        if isinstance(name, str) and name:
            invocations.append(name)

    return NormalizedUsage(
        ts_str=ts_str,
        ts=_parse_iso(ts_str) if ts_str else None,
        cwd=cwd,
        skill=skill,
        skill_invocations=invocations,
    )


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

                # ---- attributionSkill -> turns (depth / work done) ----
                if norm.skill:
                    rec = _ensure_skill(skills, norm.skill,
                                        include_per_cwd=include_per_cwd)
                    rec["turns"] += 1
                    _bump_last_seen(rec, norm.ts_str)
                    if include_per_cwd:
                        _bump_cwd(rec, norm.cwd, turns=1, invocations=0,
                                  ts_str=norm.ts_str)

                # ---- Skill tool_use -> invocations (explicit kicks) ----
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


def format_table(skills: dict[str, dict],
                 *, top: int | None = None) -> str:
    """Render the aggregate as a fixed-width text table.

    Sorted by ``turns`` descending, ties broken by ``invocations`` desc,
    then by skill name (stable order). Skill name is truncated at 40
    chars -- actual names are ``<plugin>:<skill>`` (typically <30 chars).
    ``last_seen`` is truncated to the minute precision to keep rows
    scannable.
    """
    rows = sorted(skills.items(),
                  key=lambda kv: (-kv[1]["turns"], -kv[1]["invocations"],
                                  kv[0]))
    if top is not None:
        rows = rows[:top]

    name_w = max([8] + [min(40, len(k)) for k, _ in rows])
    headers = (f"{'SKILL':<{name_w}}  {'TURNS':>6}  {'INVOCATIONS':>11}  "
               f"{'LAST_SEEN':<22}")
    sep = "-" * len(headers)
    lines = [headers, sep]
    for name, rec in rows:
        shown = name if len(name) <= name_w else name[: name_w - 1] + "~"
        last = rec.get("last_seen") or "?"
        last_short = last[:19].replace("T", " ") if last != "?" else "?"
        lines.append(f"{shown:<{name_w}}  {rec['turns']:>6}  "
                     f"{rec['invocations']:>11}  {last_short:<22}")
    return "\n".join(lines)


def format_json(skills: dict[str, dict]) -> str:
    """Emit the aggregate as JSON (sorted by turns desc for stable diffs)."""
    ordered = dict(sorted(skills.items(),
                          key=lambda kv: (-kv[1]["turns"],
                                          -kv[1]["invocations"],
                                          kv[0])))
    return json.dumps(ordered, indent=2, sort_keys=False)




def filter_by_cwd_prefix(skills: dict[str, dict], cwd_prefix: str) -> dict[str, dict]:
    """Return a fresh aggregate restricted to skills whose ``cwds`` map
    has at least one entry starting with ``cwd_prefix``.

    The returned dict rolls each surviving cwd's per-skill counts back
    into the top-level counters so callers can render top-N without
    touching the per-cwd breakdown. ``last_seen`` is also rolled up
    as the max across the matching cwds.

    Skills without a ``cwds`` map (i.e. ``include_per_cwd=False``) are
    dropped -- the caller should rerun aggregation with
    ``include_per_cwd=True`` when per-cwd filtering is needed.
    """
    out: dict[str, dict] = {}
    if not cwd_prefix:
        return out
    for name, rec in skills.items():
        cwds = rec.get("cwds")
        if not cwds:
            continue
        merged = {"turns": 0, "invocations": 0, "last_seen": None}
        for cwd, bucket in cwds.items():
            if not _cwd_matches(cwd, cwd_prefix):
                continue
            merged["turns"] += bucket.get("turns", 0)
            merged["invocations"] += bucket.get("invocations", 0)
            ls = bucket.get("last_seen")
            if ls and (merged["last_seen"] is None or ls > merged["last_seen"]):
                merged["last_seen"] = ls
        if merged["turns"] or merged["invocations"]:
            out[name] = merged
    return out


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
    if not skills:
        print(f"[skill-usage] no skills found under {logs_glob}",
              file=sys.stderr)
        return 0

    if args.propose_delete:
        return _run_propose_delete(skills, window, dry_run=args.dry_run)

    if args.json:
        print(format_json(skills))
    else:
        top = None if args.top == 0 else args.top
        print(format_table(skills, top=top))
    return 0


def _run_propose_delete(skills: dict[str, dict],
                        window: int | None,
                        *,
                        dry_run: bool) -> int:
    """Pipe the 0/0-in-window subset to ``dump_usage.py``.

    The subset is the deterministic gate: skills whose aggregated
    ``turns`` AND ``invocations`` are both 0 within the window. Skills
    that never appeared in any log are excluded here too -- the dump
    tool runs against telemetry, not against the on-disk inventory, so
    a skill that has never been invoked in any captured session will
    not show up.

    ``dry_run=True`` echoes ``--dry-run`` to dump_usage.py so the
    chat-rendered table is printed without the AskUserQuestion loop.
    Returns dump_usage.py's exit code (0 on a clean loop).
    """
    candidates = sorted(
        name for name, rec in skills.items()
        if rec.get("turns", 0) == 0 and rec.get("invocations", 0) == 0
    )
    # dump_usage.py lives next to the skill that owns it. Resolve the
    # path from the tools/ dir to keep the call site free of absolute
    # repo paths.
    here = Path(__file__).resolve().parent
    dump_script = here.parent / "skills" / "prune-propose" / "scripts" / "dump_usage.py"
    if not dump_script.is_file():
        print(f"[skill-usage] dump script missing: {dump_script}",
              file=sys.stderr)
        return 2

    import subprocess
    cmd = [sys.executable, str(dump_script),
           "--window-days", str(window if window is not None else 0)]
    if dry_run:
        cmd.append("--dry-run")
    payload = "\n".join(candidates) + ("\n" if candidates else "")
    r = subprocess.run(cmd, input=payload, text=True,
                       capture_output=True, timeout=300)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
