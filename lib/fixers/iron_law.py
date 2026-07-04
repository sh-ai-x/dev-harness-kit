"""iron_law.py — Specialized fixer for Iron Laws (CLAUDE.md §1 + lib/write_claude_md.py)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

from .base import FixerABC, Issue
from ._registry import register


CANONICAL_LAWS = {
    "L1": "No-Test-No-Code",
    "L2": "Root-Cause-First",
    "L3": "Evidence-Before-Done",
    "L4": "No-Stub",
    "L5": "Lean-Output",
}


class IronLawFixer(FixerABC):
    name = "iron_law"
    target_files = ["CLAUDE.md", "lib/write_claude_md.py"]

    def diagnose(self, project_root: Path, asset: Dict) -> List[Issue]:
        issues = []
        content = asset.get("content", "")
        path = asset.get("path", "")
        for num, canonical_short in CANONICAL_LAWS.items():
            marker = f"**L{num[-1]} {canonical_short}**"
            if marker not in content:
                # find first L occurrence to suggest line
                line_no = 0
                for i, line in enumerate(content.splitlines(), 1):
                    if line.startswith(f"**{num}**"):
                        line_no = i
                        break
                issues.append(Issue(
                    asset=asset.get("path", "?"),
                    file_path=path,
                    line=line_no,
                    axis="consistency",
                    problem=f"missing canonical Iron Law {num} ({canonical_short})",
                    suggested_fix=f"- **{num} {canonical_short}**: <canonical text from lib/write_claude_md.py:IRON_LAWS>",
                ))
        return issues


INSTANCE = IronLawFixer()
register("iron_law", INSTANCE)
