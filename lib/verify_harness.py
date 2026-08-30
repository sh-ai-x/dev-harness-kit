#!/usr/bin/env python3
"""
verify_harness.py — Tier 0 deterministic verification gate.

Parses a step's declared verification commands, runs them without a shell,
and derives a stable failure signature for the no-progress guard. Never
calls an LLM — exit codes and test counts only. See docs/proposals/
verification-harness/harness-design.yaml for the full three-tier design.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analysis_core import (
    mask_secrets as _mask_secrets,  # noqa: E402 — reuse the repo's one secret-redaction pass (promoted from private in inspect 2026-08-27 overarch-1)
)
from repair_coordinator import failure_signature  # noqa: E402

_VERIFICATION_HEADING_RE = re.compile(
    r"##\s*Verification\s*&?\s*Status\s*Update", re.IGNORECASE
)
_NEXT_HEADING_RE = re.compile(r"\n##\s")
_FENCED_BLOCK_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")
_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")

_TAIL_CHARS = 2000
_COMMAND_TIMEOUT_SECONDS = 300


def parse_verification(step_meta: Mapping[str, Any], step_md_text: str) -> List[str]:
    """Resolve a step's declared verification commands.

    Precedence: `step_meta["verification"]` (str or list[str]) wins over a
    fenced code block found under a "Verification & Status Update" section
    in `step_md_text`. Returns `[]` when neither source declares anything.
    """
    declared = step_meta.get("verification")
    if isinstance(declared, str) and declared.strip():
        return [declared.strip()]
    if isinstance(declared, (list, tuple)):
        filtered = [str(item).strip() for item in declared if str(item).strip()]
        if filtered:
            return filtered

    heading_match = _VERIFICATION_HEADING_RE.search(step_md_text)
    if heading_match is None:
        return []
    remainder = step_md_text[heading_match.end():]
    # Bound the search to this section only — stop at the next "## " heading
    # so an empty Verification section doesn't silently adopt a fenced
    # block from a later, unrelated section (e.g. the step template's
    # "Don't" section).
    next_heading = _NEXT_HEADING_RE.search(remainder)
    section = remainder[:next_heading.start()] if next_heading else remainder
    block_match = _FENCED_BLOCK_RE.search(section)
    if block_match is None:
        return []
    commands = []
    for line in block_match.group(1).splitlines():
        stripped = _INLINE_COMMENT_RE.sub("", line).strip()
        if not stripped or stripped.startswith("#"):
            continue
        commands.append(stripped)
    return commands


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    tests_passed: Optional[int]
    tests_failed: Optional[int]
    tail: str


@dataclass(frozen=True)
class VerifyResult:
    commands: Tuple[str, ...]
    results: Tuple[CommandResult, ...]
    ok: bool


def _extract_test_counts(text: str) -> Tuple[Optional[int], Optional[int]]:
    passed_match = _PASSED_RE.search(text)
    failed_match = _FAILED_RE.search(text)
    passed = int(passed_match.group(1)) if passed_match else None
    failed = int(failed_match.group(1)) if failed_match else None
    return passed, failed


def run_verification(
    commands: Sequence[str],
    cwd: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> VerifyResult:
    """Run each declared command in `cwd`, no shell, and aggregate the result.

    `runner` defaults to `subprocess.run` and is injected for testing. Every
    declared command runs (no early exit) so a failing step gets full
    evidence for all its acceptance criteria, not just the first miss.
    """
    results = []
    for command in commands:
        argv = shlex.split(command)
        try:
            proc = runner(
                argv, cwd=str(cwd), capture_output=True, text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
            combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            combined = f"command timed out after {_COMMAND_TIMEOUT_SECONDS}s"
            exit_code = 124
        except OSError as exc:
            # Missing/misspelled executable, permission denied, etc. — a
            # bad command must not abort the rest of the checklist; it
            # becomes ordinary failure evidence like any other.
            combined = str(exc)
            exit_code = 127
        passed, failed = _extract_test_counts(combined)
        results.append(
            CommandResult(
                command=command,
                exit_code=exit_code,
                tests_passed=passed,
                tests_failed=failed,
                tail=_mask_secrets(combined[-_TAIL_CHARS:]),
            )
        )
    ok = all(r.exit_code == 0 for r in results)
    return VerifyResult(commands=tuple(commands), results=tuple(results), ok=ok)


def successful_checks(result: VerifyResult) -> int:
    """Count of declared commands that exited 0.

    A ready-made second signal for `repair_coordinator.has_progress`
    (its `successful_checks` field) so PR 3 doesn't have to reconstruct
    this from `VerifyResult.results` on its own.
    """
    return sum(1 for r in result.results if r.exit_code == 0)


def verification_signature(result: VerifyResult) -> str:
    """Stable identity for the current verification failure, for the
    no-progress guard (`repair_coordinator.has_progress`).

    Delegates to `repair_coordinator.failure_signature` — reuse, don't
    reinvent. Only failing commands feed the signature; an all-pass result
    still produces a deterministic (empty-evidence) signature rather than
    raising, since callers may compute it defensively.
    """
    failing = [r for r in result.results if r.exit_code != 0]
    return failure_signature(
        category="verify",
        checks=[r.command for r in failing],
        # Includes tests_failed, not just exit_code: most test/lint
        # runners return the same nonzero exit code regardless of how
        # many checks failed, so exit_code alone can't distinguish real
        # progress (e.g. 3 failed -> 1 failed) from a stuck retry.
        evidence={
            r.command: {"exit_code": r.exit_code, "tests_failed": r.tests_failed}
            for r in failing
        },
    )
