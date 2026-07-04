"""build.py — Specialized fixer for build category (MUST-53)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

from .base import FixerABC, Issue
from ._registry import register


class BuildFixer(FixerABC):
    name = "build"
    target_files = ["skills/build/**"]

    def diagnose(self, project_root, asset):
        content = asset.get("content", "")
        issues = []
        if "name:" not in content:
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=asset.get("path", ""),
                line=1,
                axis="completeness",
                problem="build: missing frontmatter 'name:'",
                suggested_fix="add 'name: <skill-name>' to YAML frontmatter",
            ))
        return issues


INSTANCE = BuildFixer()
register("build", INSTANCE)
