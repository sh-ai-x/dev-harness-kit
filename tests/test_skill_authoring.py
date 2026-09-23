#!/usr/bin/env python3
"""
test_skill_authoring.py — L6 alpha: every existing skill SKILL.md must declare `alpha:`.

This is the bulk-apply companion to `test_skill_governance.py`, which only
gates NEW skills (added after origin/main). PR-3 (closes #283) backfills
`alpha: state|enforcement|analysis` onto all existing skills; this test
locks the result so future drift is caught immediately.

The test is intentionally separate from test_skill_governance.py:

* `test_skill_governance.py` — gates NEW skills (lint-on-add).
* `test_skill_authoring.py` — asserts EVERY skill declares alpha (lint-on-tree).

The two tests overlap on the NEW-skill subset; that's intentional. A skill
that survives governance on add must also survive authoring on the next
sync.

Failure modes covered:

* Skill directory under `skills/` has no SKILL.md -> FAIL with dir name.
* SKILL.md frontmatter is missing or malformed -> FAIL with skill name.
* SKILL.md frontmatter has no `alpha:` field -> FAIL with skill name.
* `alpha:` value is not in {state, enforcement, analysis} -> FAIL with skill name + value.

A clean tree (zero skill directories) passes vacuously.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SKILLS_ROOT = PROJECT_ROOT / "skills"
ALLOWED_ALPHAS = frozenset({"state", "enforcement", "analysis"})
PRIVATE_SKILL_DIRS = frozenset()

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.+?)\n---", re.DOTALL)
_ALPHA_LINE_RE = re.compile(r"^alpha:\s*(\S+)\s*$", re.MULTILINE)


def _skill_dirs() -> list[Path]:
    if not SKILLS_ROOT.exists():
        return []
    return sorted(
        p for p in SKILLS_ROOT.iterdir()
        if p.is_dir() and p.name not in PRIVATE_SKILL_DIRS
    )


def _parse_alpha(text: str) -> tuple[str | None, bool]:
    """Return (alpha_value_or_None, frontmatter_seen).

    `frontmatter_seen=False` means the SKILL.md has no `--- ... ---` block
    at all (vs. frontmatter present but the `alpha:` key missing).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, False
    body = m.group(1)
    hit = _ALPHA_LINE_RE.search(body)
    if not hit:
        return None, True
    return hit.group(1), True


class TestSkillAuthoring(unittest.TestCase):
    def test_every_skill_declares_alpha(self):
        """Every skill directory must have SKILL.md with `alpha:` ∈ ALLOWED.

        Fails with the offending skill name(s); clean tree passes vacuously.
        """
        dirs = _skill_dirs()
        if not dirs:
            return

        violations: list[str] = []
        for d in dirs:
            name = d.name
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                violations.append(f"{name}: SKILL.md missing under skills/{name}/")
                continue
            text = skill_md.read_text(encoding="utf-8")
            alpha, fm_seen = _parse_alpha(text)
            if not fm_seen:
                violations.append(
                    f"{name}: SKILL.md frontmatter missing or malformed "
                    f"(expected `--- ... ---` block at top of file)"
                )
                continue
            if alpha is None:
                violations.append(
                    f"{name}: SKILL.md frontmatter missing `alpha:` field "
                    f"(must be one of state/enforcement/analysis — see "
                    f"rules/skill-authoring.md L6 section)"
                )
                continue
            if alpha not in ALLOWED_ALPHAS:
                violations.append(
                    f"{name}: SKILL.md frontmatter alpha={alpha!r} not in "
                    f"{sorted(ALLOWED_ALPHAS)}"
                )

        self.assertEqual(
            violations, [],
            "Skill authoring (L6 alpha) violations:\n  "
            + "\n  ".join(violations),
        )

    def test_alpha_counts_balanced(self):
        """Smoke: at least one skill per alpha class is present. Catches
        accidental bulk-mislabel (e.g. all-skills-get-state regressions)."""
        seen: set[str] = set()
        for d in _skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            alpha, _ = _parse_alpha(text)
            if alpha in ALLOWED_ALPHAS:
                seen.add(alpha)
        self.assertEqual(
            seen, ALLOWED_ALPHAS,
            f"Expected to see all three alpha classes across skills/, "
            f"missing: {sorted(ALLOWED_ALPHAS - seen)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
