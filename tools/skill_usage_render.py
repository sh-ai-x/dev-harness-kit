"""skill_usage_render.py -- presentation + downstream side-effects.

Three concerns that used to live in ``tools/skill_usage.py``:

1. Table + JSON formatting (``format_table`` / ``format_json``).
2. Per-cwd roll-up filtering (``filter_by_cwd_prefix``).
3. Pipe-into-``dump_usage.py`` for the prune-propose AskUserQuestion
   loop (``_run_propose_delete``).

Re-imported by ``tools/skill_usage.py`` so the public test surface
(``skill_usage.format_table`` / ``skill_usage.format_json`` /
``skill_usage.filter_by_cwd_prefix``) stays importable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


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


def filter_by_cwd_prefix(skills: dict[str, dict], cwd_prefix: str,
                         *, _cwd_matches=None) -> dict[str, dict]:
    """Return a fresh aggregate restricted to skills whose ``cwds`` map
    has at least one entry starting with ``cwd_prefix``.

    The returned dict rolls each surviving cwd's per-skill counts back
    into the top-level counters so callers can render top-N without
    touching the per-cwd breakdown. ``last_seen`` is also rolled up
    as the max across the matching cwds.

    Skills without a ``cwds`` map (i.e. ``include_per_cwd=False``) are
    dropped -- the caller should rerun aggregation with
    ``include_per_cwd=True`` when per-cwd filtering is needed.

    ``_cwd_matches`` is injected so this module stays free of
    ``tools/skill_usage``-internal helpers. ``tools/skill_usage.py``
    binds it at re-export time.
    """
    if _cwd_matches is None:
        raise TypeError(
            "filter_by_cwd_prefix requires _cwd_matches to be injected; "
            "import via tools/skill_usage.py, not directly"
        )
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


def _run_propose_delete(skills: dict[str, dict],
                        window: int | None,
                        *,
                        dry_run: bool,
                        here: Path | None = None) -> int:
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

    ``here`` is the tools/ dir; injected so this module stays
    free of __file__-relative paths and is test-friendly.
    """
    candidates = sorted(
        name for name, rec in skills.items()
        if rec.get("turns", 0) == 0 and rec.get("invocations", 0) == 0
    )
    here = here or Path(__file__).resolve().parent
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
