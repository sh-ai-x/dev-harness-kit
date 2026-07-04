"""hooks.py — Specialized fixer for hook shell scripts (MUST-53)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

from .base import FixerABC, Issue
from ._registry import register


class HooksFixer(FixerABC):
    name = "hooks"
    target_files = [
        ".claude-plugin/plugin/hooks/tdd-guard.sh",
        ".claude-plugin/plugin/hooks/bash-guard.sh",
        ".claude-plugin/plugin/hooks/secret-scan.sh",
        ".claude-plugin/plugin/hooks/slop-detector.sh",
        ".claude-plugin/plugin/hooks/stop-verify.sh",
        ".githooks/pre-push",
    ]

    def diagnose(self, project_root: Path, asset: Dict) -> List[Issue]:
        issues = []
        content = asset.get("content", "")
        lines = content.splitlines()
        path = asset.get("path", "")

        # Rule 1: must have shebang
        if not lines or not lines[0].startswith("#!"):
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=path,
                line=1,
                axis="completeness",
                problem="missing shebang line",
                suggested_fix="#!/usr/bin/env bash",
            ))

        # Rule 2: must have `set -eo pipefail` for safety
        if "set -eo pipefail" not in content and "set -e" not in content:
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=path,
                line=2,
                axis="correctness",
                problem="missing 'set -eo pipefail' for fail-fast",
                suggested_fix="set -eo pipefail",
            ))

        # Rule 3: must default to exit 0 (advisory)
        if "exit 0" not in content:
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=path,
                line=0,
                axis="consistency",
                problem="must default to 'exit 0' (advisory; --strict only for exit 2)",
                suggested_fix="exit 0",
            ))

        # Rule 4: must use ${CLAUDE_PLUGIN_ROOT} portable path
        if any(s in content for s in ("/Users/", "dev/dev-harness")):
            issues.append(Issue(
                asset=asset.get("path", "?"),
                file_path=path,
                line=0,
                axis="correctness",
                problem="hardcoded absolute path; use ${CLAUDE_PLUGIN_ROOT}",
                suggested_fix="${CLAUDE_PLUGIN_ROOT}",
            ))

        return issues


INSTANCE = HooksFixer()
register("hooks", INSTANCE)
