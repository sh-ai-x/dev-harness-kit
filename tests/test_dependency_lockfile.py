"""Regression test for the requirements.lock fixture.

The OWASP A03 supply-chain scorer deducts 10 points when the repo has
no recognized dependency lockfile
(`requirements.lock | uv.lock | poetry.lock | package-lock.json | pnpm-lock.yaml | yarn.lock`).
This test pins the presence + format of the lockfile so a refactor
that drops it (or breaks its pinned-version shape) gets caught here
instead of by the security scorecard.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
LOCKFILE = ROOT / "requirements.lock"
RECOGNIZED_LOCKFILES = {
    "requirements.lock",
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}


def test_a_recognized_lockfile_exists() -> None:
    """At least one of the recognized lockfile names must exist at the repo root."""
    found = [p.name for p in ROOT.iterdir() if p.is_file() and p.name in RECOGNIZED_LOCKFILES]
    assert found, (
        f"No recognized dependency lockfile at repo root. "
        f"Expected one of: {sorted(RECOGNIZED_LOCKFILES)}. "
        f"This causes a -10 OWASP A03 supply-chain deduction in "
        f"`/dev-kit:security-metrics`."
    )


def test_requirements_lock_pins_versions() -> None:
    """Every non-comment, non-blank line in requirements.lock must pin a version."""
    if not LOCKFILE.exists():
        return  # covered by the prior test
    text = LOCKFILE.read_text(encoding="utf-8")
    pinned_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert pinned_lines, "requirements.lock has no pinned requirements"
    for line in pinned_lines:
        # PEP 508 pinned form: `name==X.Y.Z` (extras / markers optional)
        assert "==" in line, (
            f"requirements.lock line is not pinned to an exact version: {line!r}"
        )


def test_requirements_lock_lists_pytest() -> None:
    """pytest must appear in the lockfile — CI's test step depends on it."""
    if not LOCKFILE.exists():
        return
    text = LOCKFILE.read_text(encoding="utf-8").lower()
    assert "pytest" in text, "pytest missing from requirements.lock"
