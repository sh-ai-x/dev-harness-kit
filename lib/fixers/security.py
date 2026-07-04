"""security.py — Specialized fixer for security category (MUST-53)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

from .base import FixerABC, Issue
from ._registry import register


class SecurityFixer(FixerABC):
    name = "security"
    target_files = ["skills/security/**"]

    def diagnose(self, project_root, asset):
        content = asset.get("content", "")
        issues = []
        if "name:" not in content:
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=asset.get("path", ""),
                line=1,
                axis="completeness",
                problem="security: missing frontmatter 'name:'",
                suggested_fix="add 'name: <skill-name>' to YAML frontmatter",
            ))
        return issues


INSTANCE = SecurityFixer()
register("security", INSTANCE)
