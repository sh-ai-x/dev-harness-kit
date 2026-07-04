"""a2a.py — Specialized fixer for a2a category (MUST-53)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

from .base import FixerABC, Issue
from ._registry import register


class A2aFixer(FixerABC):
    name = "a2a"
    target_files = ["skills/a2a/**"]

    def diagnose(self, project_root, asset):
        content = asset.get("content", "")
        issues = []
        if "name:" not in content:
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=asset.get("path", ""),
                line=1,
                axis="completeness",
                problem="a2a: missing frontmatter 'name:'",
                suggested_fix="add 'name: <skill-name>' to YAML frontmatter",
            ))
        return issues


INSTANCE = A2aFixer()
register("a2a", INSTANCE)
