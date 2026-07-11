#!/usr/bin/env python3
"""validate.py — validates the CI installation in the target repo.

Extracted from dev-kit's own `.github/workflows/ci.yml` `validate` job (5 inline
`python3 -c "..."` blocks, originally lines 67-111). Repo-agnostic: gracefully
skips checks that don't apply to the target's structure.

Exit code 0 on success, 1 on any check failure. Output is line-oriented for
GitHub Actions log readability.

Checks performed (each prints `OK (...)` or `FAIL (...):`):
1. validate_installation_complete — all 8 required files present (FAIL if missing)
2. validate_bash_syntax         — `bash -n` on every installed .sh + pre-push
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REQUIRED_FILES = [
    ".github/workflows/ci.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/review.yml",
    ".githooks/pre-push",
    "scripts/validate.py",
    "scripts/test.sh",
    "scripts/branch-policy.sh",
    "scripts/ci-local.sh",
]


def _ok(msg: str) -> None:
    print(f"  - {msg} OK")


def _fail(msg: str) -> None:
    print(f"  - {msg} FAIL")


def validate_installation_complete(repo_root: pathlib.Path) -> bool:
    missing = [f for f in REQUIRED_FILES if not (repo_root / f).exists()]
    if missing:
        _fail(f"installation: missing {len(missing)} file(s): {missing}")
        return False
    _ok(f"installation complete ({len(REQUIRED_FILES)} files)")
    return True


def validate_bash_syntax(repo_root: pathlib.Path) -> bool:
    """Run `bash -n` on every installed .sh and `.githooks/pre-push`.

    Covers `scripts/{test,branch-policy,ci-local}.sh` and the githook in one pass,
    so no separate `validate_test_runner` step is needed.
    """
    sh_files = list((repo_root / "scripts").glob("*.sh")) + [repo_root / ".githooks" / "pre-push"]
    failures = []
    for h in sh_files:
        if not h.exists():
            continue
        r = subprocess.run(["bash", "-n", str(h)], capture_output=True, text=True)
        if r.returncode != 0:
            failures.append((h.name, r.stderr.strip()))
    if failures:
        _fail(f"bash syntax: {len(failures)} file(s): {failures}")
        return False
    _ok(f"bash syntax ({len(sh_files)} scripts clean)")
    return True


def main(repo_root: pathlib.Path | None = None) -> int:
    repo_root = repo_root or pathlib.Path.cwd()
    print(f"validate.py — repo_root={repo_root}")
    checks = [
        validate_installation_complete,
        validate_bash_syntax,
    ]
    results = [c(repo_root) for c in checks]
    if all(results):
        print("OK: CI installation valid")
        return 0
    failed = sum(1 for r in results if not r)
    print(f"FAIL: {failed}/{len(results)} check(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
