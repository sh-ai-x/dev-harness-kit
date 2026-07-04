"""review.py — Specialized fixer for review category (MUST-53)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

from .base import FixerABC, Issue
from ._registry import register


class ReviewFixer(FixerABC):
    name = "review"
    target_files = ["skills/review/**"]

    def diagnose(self, project_root, asset):
        content = asset.get("content", "")
        issues = []
        if "name:" not in content:
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=asset.get("path", ""),
                line=1,
                axis="completeness",
                problem="review: missing frontmatter 'name:'",
                suggested_fix="add 'name: <skill-name>' to YAML frontmatter",
            ))
        return issues


INSTANCE = ReviewFixer()
register("review", INSTANCE)
