#!/usr/bin/env python3
"""dump_usage.py -- per-skill delete-proposal loop for prune-propose.

Reads a candidate-skill list (one name per line on stdin) emitted by
``tools/skill_usage.py --propose-delete``, prints a chat-rendered
table, then asks the user one ``AskUserQuestion`` per skill. Each
question is binary: Delete or Keep. No batch approve, no ``--yes``.

The script is invoked in two modes:

* **interactive** (default): prints the table, then issues the
  ``AskUserQuestion`` calls. Exit code 0 when the loop finishes; the
  approved-for-deletion set is written to stdout as ``DELETED: <names>``
  and ``KEPT: <names>`` lines so downstream automation can parse it.
* **dry-run** (``--dry-run``): prints the candidate table only and
  exits 0. Used by ``tools/skill_usage.py --propose-delete --dry-run``
  to sanity-check the 0/0 + 30-day filter before the user commits to
  the click-through loop.

The ``AskUserQuestion`` integration is intentionally a thin wrapper:
the skill body decides the framing. When this script is run as a CLI
(``python3 dump_usage.py < candidates.txt``), the loop degrades to
a plain-text prompt so it remains testable in CI without a TTY.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _read_candidates(stream) -> list[str]:
    """Pull one skill name per non-empty line from ``stream``.

    Whitespace, blanks, and ``#``-prefixed comments are dropped. Order
    is preserved (the caller's table is already sorted by usage signal).
    """
    out: list[str] = []
    for raw in stream:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def render_table(skills: list[str], window_days: int) -> str:
    """Render the candidate list as a fixed-width table.

    The header echoes the window so the user can verify the filter
    before clicking through each Delete prompt.
    """
    if not skills:
        return f"[prune-propose] no candidates (0 turns AND 0 invocations in last {window_days} days)"
    name_w = max([8] + [min(40, len(s)) for s in skills])
    header = (f"{'SKILL':<{name_w}}  {'WINDOW_DAYS':>11}")
    sep = "-" * len(header)
    lines = [header, sep]
    for s in skills:
        shown = s if len(s) <= name_w else s[: name_w - 1] + "~"
        lines.append(f"{shown:<{name_w}}  {window_days:>11}")
    return "\n".join(lines)


def _ask_user(skill: str) -> bool:
    """Prompt the user for a Delete/Keep decision on ``skill``.

    Two execution paths:

    * If the ``PRUNE_PROPOSE_ASKUSER`` env var points to an executable
      that accepts a JSON payload on stdin and returns 0 (delete) or
      1 (keep), it is invoked. The wrapper around ``AskUserQuestion``
      in the skill body is expected to set this.
    * Otherwise, fall back to a plain stdin prompt so the script
      remains usable in a non-interactive CI job.

    Returns True iff the user approved the deletion.
    """
    wrapper = os.environ.get("PRUNE_PROPOSE_ASKUSER")
    payload = json.dumps({"skill": skill, "options": ["Delete", "Keep"]})
    if wrapper:
        import subprocess
        r = subprocess.run(
            [wrapper], input=payload, text=True,
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    # Plain-text fallback.
    print(f"  Delete {skill!r}? [y/N]: ", end="", flush=True)
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Per-skill delete-proposal loop (reuses skill_usage data).")
    p.add_argument("--window-days", type=int, default=30,
                   help="window used by the upstream filter (default 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the candidate table only; skip the ask loop")
    p.add_argument("--candidates", default=None, metavar="CSV",
                   help="comma-separated candidate skill names (used by "
                        "tests so the ask loop can read fresh stdin; the "
                        "CLI tool reads candidates from stdin by default)")
    args = p.parse_args(argv)

    if args.candidates is not None:
        candidates = [s.strip() for s in args.candidates.split(",") if s.strip()]
    else:
        candidates = _read_candidates(sys.stdin)
    print(render_table(candidates, args.window_days))
    print(f"[prune-propose] candidates={len(candidates)} window={args.window_days}d")

    if args.dry_run:
        print("[prune-propose] dry-run: skipping AskUserQuestion loop")
        return 0

    if not candidates:
        print("[prune-propose] nothing to propose; exit 0")
        return 0

    deleted: list[str] = []
    kept: list[str] = []
    for skill in candidates:
        if _ask_user(skill):
            deleted.append(skill)
        else:
            kept.append(skill)

    print(f"DELETED: {' '.join(deleted) if deleted else '(none)'}")
    print(f"KEPT:    {' '.join(kept) if kept else '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
