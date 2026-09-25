#!/usr/bin/env python3
"""
test_skill_governance.py — L6 alpha declaration gate for new SKILL.md files.

Pinned by `rules/skill-authoring.md` (L6 section). Every NEW SKILL.md must
declare an `alpha:` frontmatter field in {state, enforcement, analysis}. The
gate is enforced only on skills added AFTER origin/main so the existing 39
skills aren't punished — the baseline is computed from origin/main's
`skills/` tree (or, when origin/main is unreachable, the local `main`
branch / git-log fallback; see `baseline_skill_dirs` for the full chain).

Failure modes covered:
- New skill directory has no SKILL.md -> FAIL with the directory name.
- New SKILL.md has no `alpha:` frontmatter field -> FAIL with the skill name.
- New SKILL.md has `alpha:` whose value is not in ALLOWED_ALPHAS -> FAIL.

A clean branch (zero added skills) passes vacuously.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
ALLOWED_ALPHAS = frozenset({"state", "enforcement", "analysis"})
PRIVATE_SKILL_DIRS = frozenset()


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command in PROJECT_ROOT. Non-zero exit codes are *not* raised —
    callers inspect `returncode` / `stderr` so fallbacks can chain cleanly.
    """
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _strip_prefix(raw: str) -> set[str]:
    """Parse `git ls-tree -d --name-only skills/` output -> skill-dir names.

    Each non-empty line is `skills/<name>`; we strip the prefix and drop the
    top-level `skills` entry. Unknown shapes are tolerated and skipped.
    """
    names: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "skills" or line == "skills/":
            continue
        if line.startswith("skills/"):
            names.add(line[len("skills/"):])
    return names


def baseline_skill_dirs() -> tuple[set[str], str]:
    """Return (skill_dir_names, source_label) for the baseline set.

    Preference order:
      1. `origin/main` `skills/` subtree (the worktree's branch point).
      2. Local `main` branch `skills/` subtree (if origin isn't fetched).
      3. Union of `git log --diff-filter=A` paths against main (last resort).

    `source_label` lets the test surface which tier it actually used, so a
    reviewer running in CI can tell whether origin/main was reachable.
    """
    r = _git("ls-tree", "-d", "--name-only", "origin/main", "skills/")
    if r.returncode == 0:
        names = _strip_prefix(r.stdout)
        if names:
            return names, "origin/main"
    r = _git("ls-tree", "-d", "--name-only", "main", "skills/")
    if r.returncode == 0:
        names = _strip_prefix(r.stdout)
        if names:
            return names, "main (local fallback)"
    # Last resort: derive added skill dirs from history.
    r = _git(
        "log", "--diff-filter=A", "--name-only", "--pretty=format:",
        "main", "--", "skills/",
    )
    if r.returncode == 0:
        names: set[str] = set()
        for raw in r.stdout.splitlines():
            path = raw.strip()
            if not path.startswith("skills/"):
                continue
            m = re.match(r"^skills/([^/]+)/", path + "/")
            if m:
                names.add(m.group(1))
        if names:
            return names, "git-log main (last-resort fallback)"
    return set(), "none"


def local_skill_dirs() -> set[str]:
    """Set of skill directory names currently on disk under skills/."""
    skills_root = PROJECT_ROOT / "skills"
    if not skills_root.exists():
        return set()
    return {
        p.name for p in skills_root.iterdir()
        if p.is_dir() and p.name not in PRIVATE_SKILL_DIRS
    }


def extract_alpha(text: str) -> tuple[str | None, bool]:
    """Return (alpha_value_or_None, frontmatter_seen).

    `frontmatter_seen` distinguishes "no frontmatter at all" from "frontmatter
    present but no `alpha:` field" — useful for actionable error messages.
    """
    m = re.match(r"^---\s*\n(.+?)\n---", text, re.DOTALL)
    if not m:
        return None, False
    for line in m.group(1).splitlines():
        if line.startswith("alpha:"):
            value = line.split(":", 1)[1].strip()
            return (value or None), True
    return None, True


class TestSkillGovernance(unittest.TestCase):
    def test_baseline_resolves_to_nonempty_set(self):
        """Sanity: whichever tier the fallback resolves to must yield >=1
        skill name. If all tiers fail, the gate is meaningless — fail loudly.
        """
        names, source = baseline_skill_dirs()
        self.assertGreater(
            len(names), 0,
            f"baseline resolved empty (source={source!r}); cannot enforce L6",
        )

    def test_new_skills_declare_alpha(self):
        """L6 gate: any skill directory present locally but NOT in the
        baseline must declare `alpha: state|enforcement|analysis` in its
        SKILL.md frontmatter. Fails with the offending skill name(s).
        """
        baseline, source = baseline_skill_dirs()
        local = local_skill_dirs()
        new_skills = sorted(local - baseline)

        if not new_skills:
            # Vacuous pass on a clean branch.
            return

        violations: list[str] = []
        for skill in new_skills:
            skill_md = PROJECT_ROOT / "skills" / skill / "SKILL.md"
            if not skill_md.exists():
                violations.append(
                    f"{skill}: SKILL.md missing under skills/{skill}/"
                )
                continue
            text = skill_md.read_text(encoding="utf-8")
            alpha, fm_seen = extract_alpha(text)
            if not fm_seen:
                violations.append(
                    f"{skill}: SKILL.md frontmatter missing or malformed"
                )
                continue
            if alpha is None:
                violations.append(
                    f"{skill}: SKILL.md frontmatter missing `alpha:` field "
                    f"(must be one of state/enforcement/analysis — see "
                    f"rules/skill-authoring.md L6 section)"
                )
                continue
            if alpha not in ALLOWED_ALPHAS:
                violations.append(
                    f"{skill}: SKILL.md frontmatter alpha={alpha!r} not in "
                    f"{sorted(ALLOWED_ALPHAS)}"
                )

        header = f"Skill governance (L6) -- baseline source: {source}\n"
        self.assertEqual(
            violations, [],
            header + "Violations:\n  " + "\n  ".join(violations),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
