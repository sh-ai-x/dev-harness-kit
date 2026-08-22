"""Deterministic staleness gate for rules/*.md.

Parses YAML frontmatter from each rules/*.md (skips rules/index.md) and fails
if `stale_after < today`. `status: deprecated` is exempt. Missing
`stale_after` is a pass (fail-open — see docs/proposals/okf-adoption/03).

Exit codes:
  0 — all files within date or exempt
  1 — at least one expired file OR frontmatter parse failure

Iron Law L7: this is the deterministic enforcement the LLM staleness heuristic
in skills/docs-maintenance/SKILL.md cannot self-impose. See proposal §delta 회계
for cost rationale and §roll-back for exit conditions.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

try:
    import yaml
except ImportError:  # PyYAML is in requirements.lock but keep a graceful exit
    print("error: PyYAML is required for check_doc_lifecycle", file=sys.stderr)
    sys.exit(1)


_RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
_RESERVED = {"index.md"}  # rules/index.md is a navigation page, not a doc
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.+?)\n---\s*\n", re.DOTALL)


def _read_frontmatter(path: Path) -> Tuple[Optional[dict], bool, Optional[str]]:
    """Return (parsed-mapping, had-frontmatter, error).

    `had_frontmatter` is True when a leading ``---`` block exists, even if
    it failed to parse — this lets the gate distinguish "no block at all"
    (fail-open per proposal §limitations 4) from "block present but
    malformed" (fail-closed per §게이트 동작).
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, False, None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return None, True, f"yaml parse error: {e}"
    if not isinstance(fm, dict):
        return None, True, "frontmatter is not a mapping"
    return fm, True, None


def _check_one(path: Path, today: date) -> Optional[str]:
    """Return a one-line violation message, or None on pass."""
    fm, had_fm, err = _read_frontmatter(path)
    if had_fm and err is not None:
        # Frontmatter exists but won't parse → fail-closed (§게이트 동작 row 5)
        return f"{path}: frontmatter error ({err})"
    if not had_fm or fm is None:
        return None  # fail-open: no frontmatter at all (§limitations 4)
    status = fm.get("status")
    if status == "deprecated":
        return None  # §5.4: deprecated docs are exempt from the expiry gate
    stale_after = fm.get("stale_after")
    if stale_after is None:
        return None  # Fail-open: missing field ≠ expired (proposal §limitations 4)
    # PyYAML safe_load auto-parses YYYY-MM-DD into datetime.date and
    # RFC 3339 timestamps like 2026-11-30T00:00:00 into datetime.datetime.
    # Both shapes are legal — only the calendar date matters for the
    # expiry check — so normalize datetime down to date before comparing.
    # Other invalid inputs land here as str or as unknown scalars.
    if isinstance(stale_after, datetime):
        expiry = stale_after.date()
    elif isinstance(stale_after, date):
        expiry = stale_after
    elif isinstance(stale_after, str):
        try:
            expiry = date.fromisoformat(stale_after)
        except ValueError:
            return f"{path}: stale_after='{stale_after}' is not ISO 8601 date"
    else:
        return f"{path}: stale_after must be a date (YYYY-MM-DD), got {type(stale_after).__name__}"
    if expiry < today:
        # Render with the normalized date so the message matches the cmp
        # (a datetime would otherwise print `2025-01-01 00:00:00`).
        return f"{path}: stale_after={expiry.isoformat()} (< today={today.isoformat()})"
    return None


def run(rules_dir: Path = _RULES_DIR, today: Optional[date] = None) -> int:
    today = today or date.today()
    violations: list[str] = []
    for md in sorted(rules_dir.glob("*.md")):
        if md.name in _RESERVED:
            continue
        v = _check_one(md, today)
        if v is not None:
            violations.append(v)
    if violations:
        print("stale_after violations:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"\n{len(violations)} violation(s). Run /dev-kit:docs-maintenance to refresh.",
            file=sys.stderr,
        )
        return 1
    print(f"doc lifecycle OK (today={today.isoformat()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rules-dir",
        type=Path,
        default=_RULES_DIR,
        help=f"path to rules/ (default: {_RULES_DIR})",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="override today (YYYY-MM-DD) for tests",
    )
    args = parser.parse_args(argv)
    return run(args.rules_dir, args.today)


if __name__ == "__main__":
    sys.exit(main())
