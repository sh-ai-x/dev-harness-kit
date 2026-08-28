#!/usr/bin/env python3
"""scripts/audit_skill_frontmatter.py

Skill frontmatter determinism audit (G5 in
docs/proposals/cache-hit-rate/structural-fix.yaml §Validation gates).

Walks every ``skills/*/SKILL.md`` and flags frontmatter fields that
change between invocations — timestamps, long hex hashes (build
artifacts), build numbers. Same discipline as the existing
``feedback-version-from-plugin-json.md`` (no hardcoded version
strings): if the SKILL.md frontmatter can change while the skill
content is identical, the prompt cache namespace for that skill is
unique per build and never reuses across sessions.

Exit codes:
  0  — every skill's frontmatter is clean
  1  — one or more skills have forbidden fields
  2  — usage error

Stdlib only. No third-party deps.

Usage:
  python3 scripts/audit_skill_frontmatter.py
  python3 scripts/audit_skill_frontmatter.py --json
"""

from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# Forbidden patterns — anything matching these in frontmatter is a
# non-deterministic field that will bust the prompt cache when the
# skill is reloaded.
#
# The regex deliberately allows ``last-reviewed:`` style metadata
# (a stable string the maintainer writes once) but flags ISO-style
# dates that auto-regenerate, long hex hashes (≥7 chars, often git
# SHAs that change with each commit), and ``build-NNN`` style numbers.
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ISO-style dates: YYYY-MM-DD (with optional T... suffix).
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b"),
    # Long hex hashes (≥7 hex chars) — git SHAs and similar.
    re.compile(r"\b[0-9a-f]{7,40}\b"),
    # Build/version numbers like "build-1234" or "build_42".
    re.compile(r"\bbuild[-_]\d+\b"),
    # SHA-style refs like "@abc1234" or "(#abc1234)" — common in
    # auto-generated footers.
    re.compile(r"[@\(]\s*[0-9a-f]{7,}\s*[\)]?"),
)


def parse_frontmatter(text: str) -> str:
    """Return the YAML frontmatter block (between leading ``---`` fences)
    or an empty string when the file has no frontmatter.
    """
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    return text[3:end].lstrip("\n")


def audit_skill(path: Path) -> list[str]:
    """Return a list of forbidden-pattern hits in the frontmatter, or
    empty list when the frontmatter is clean.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["<unreadable>"]
    fm = parse_frontmatter(text)
    if not fm:
        return []
    hits: list[str] = []
    for line in fm.splitlines():
        # Skip comments — they're not part of the rendered metadata.
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pat in FORBIDDEN_PATTERNS:
            for m in pat.finditer(line):
                hits.append(f"{stripped[:60]!r} → {m.group(0)}")
    return hits


def main(argv: list[str]) -> int:
    emit_json = "--json" in argv
    if not SKILLS_DIR.is_dir():
        print(f"error: {SKILLS_DIR} not found", file=sys.stderr)
        return 2

    skill_paths = sorted(
        Path(p) for p in glob.glob(str(SKILLS_DIR / "*" / "SKILL.md"))
    )
    if not skill_paths:
        print(f"error: no SKILL.md files under {SKILLS_DIR}", file=sys.stderr)
        return 2

    report: dict[str, list[str]] = {}
    for p in skill_paths:
        hits = audit_skill(p)
        if hits:
            # Use the skill name (parent dir) as the key — paths can
            # be long and noisy in a JSON report.
            report[p.parent.name] = hits

    if emit_json:
        print(json.dumps({"checked": len(skill_paths), "bad": report},
                          indent=2, sort_keys=True))
    else:
        if not report:
            print(f"[ok] {len(skill_paths)} skill(s) have deterministic frontmatter")
            return 0
        print(f"[bad] {len(report)}/{len(skill_paths)} skill(s) have forbidden fields:")
        for skill, hits in report.items():
            print(f"  - {skill}:")
            for h in hits:
                print(f"      · {h}")
        return 1
    return 0 if not report else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
