"""abc.py — Fixer ABC + Issue dataclass (MUST-53)."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass
class Issue:
    asset: str           # canonical asset path (relative to project root)
    file_path: str       # actual file to fix
    line: int            # 1-based line number (0 if N/A)
    axis: str            # which judge axis: semantic_drift / completeness / correctness / consistency
    problem: str         # human-readable description
    suggested_fix: str   # proposed fix (diff or replacement text)


class FixerABC(ABC):
    """Specialized Fixer for one category (MUST-53)."""
    name: str
    target_files: List[str]  # which files this fixer is responsible for

    @abstractmethod
    def diagnose(self, project_root: Path, asset: Dict) -> List[Issue]:
        """Inspect the asset (already loaded) and return concrete Issues.

        asset: {path, kind, content}
        Returns: list of Issue (possibly empty if no problems detected).
        """

    def fix(self, project_root: Path, issue: Issue) -> str:
        """Apply the suggested fix. Returns unified diff.

        Default implementation: returns the issue's suggested_fix as a
        patch hint. Subclasses override for actual file rewrites.
        """
        return self._diff_hint(issue)

    def _diff_hint(self, issue: Issue) -> str:
        return (
            f"--- a/{issue.file_path}\n"
            f"+++ b/{issue.file_path}\n"
            f"@@ -L{issue.line},1 +L{issue.line},1 @@\n"
            f"-{issue.problem}\n"
            f"+{issue.suggested_fix}\n"
        )
