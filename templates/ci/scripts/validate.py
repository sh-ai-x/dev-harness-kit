#!/usr/bin/env python3
"""validate.py — validates the CI installation in the target repo.

Extracted from dev-kit's own `.github/workflows/ci.yml` `validate` job (5 inline
`python3 -c "..."` blocks, originally lines 67-111). Repo-agnostic: gracefully
skips checks that don't apply to the target's structure.

Exit code 0 on success, 1 on any check failure. Output is line-oriented for
GitHub Actions log readability.

Checks performed (each prints `OK (...)` or `FAIL (...):`):
1. validate_installation_complete — all required CI and hook files present
2. validate_marker              — `.dev-kit/ci-config.json` shape
3. validate_bash_syntax         — `bash -n` on every installed .sh + pre-push
"""
from __future__ import annotations

import json
import pathlib
import re
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

_HOOK_SCRIPT_REFERENCE = re.compile(
    r"hooks/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.sh)"
)


def referenced_hook_scripts(manifest_text: str) -> set[str]:
    """Return hook source paths referenced by a hook manifest.

    The returned paths are relative to the installed ``.dev-kit/hooks/``
    directory (mirrors ``fix(install): namespace kit hook scripts under
    .dev-kit/hooks/`` in dev-kit-lite commit 45da476e). The regex strips the
    ``hooks/`` prefix from each match so consumers and the source-repo
    validator agree on the post-install location. Keep this helper in the
    shipped validator so consumer-side validation and source-repo tests
    use the same parser without duplicating its regex.
    """
    return set(_HOOK_SCRIPT_REFERENCE.findall(manifest_text))


def _ok(msg: str) -> None:
    print(f"  - {msg} OK")


def _fail(msg: str) -> None:
    print(f"  - {msg} FAIL")


def _skip(msg: str) -> None:
    print(f"  - {msg} SKIP")


def validate_installation_complete(repo_root: pathlib.Path) -> bool:
    missing = [f for f in REQUIRED_FILES if not (repo_root / f).exists()]
    if missing:
        _fail(f"installation: missing {len(missing)} file(s): {missing}")
        return False
    hooks_dir = repo_root / ".dev-kit" / "hooks"
    manifest = hooks_dir / "hooks.json"
    hook_files = list(hooks_dir.rglob("*.sh")) if hooks_dir.is_dir() else []
    if not manifest.is_file():
        _fail("installation: hook manifest missing")
        return False
    try:
        manifest_text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        # Guard the read so a permissions or decode error surfaces as a
        # normal validator FAIL line instead of a Python traceback that
        # would otherwise halt CI before the manifest-references check
        # can flag the real culprit.
        _fail(f"installation: hook manifest unreadable: {e}")
        return False
    referenced = referenced_hook_scripts(manifest_text)
    missing_hooks = [hooks_dir / rel for rel in referenced if not (hooks_dir / rel).is_file()]
    if missing_hooks:
        _fail(f"installation: missing hook source(s): {missing_hooks}")
        return False
    _ok(f"installation complete ({len(REQUIRED_FILES)} CI files + {len(hook_files)} hooks)")
    return True


def validate_marker(repo_root: pathlib.Path) -> bool:
    marker = repo_root / ".dev-kit" / "ci-config.json"
    if not marker.exists():
        _fail("ci-config marker: .dev-kit/ci-config.json missing")
        return False
    try:
        data = json.loads(marker.read_text())
        assert data.get("schema_version"), "missing schema_version"
        assert data.get("installed_by") == "dev-kit:ci-setup", \
            f"installed_by={data.get('installed_by')!r} (expected 'dev-kit:ci-setup')"
    except (AssertionError, json.JSONDecodeError) as e:
        _fail(f"ci-config marker: {e}")
        return False
    _ok(f"ci-config marker (schema={data['schema_version']})")
    return True


def validate_bash_syntax(repo_root: pathlib.Path) -> bool:
    """Run `bash -n` on every installed .sh and `.githooks/pre-push`.

    Covers `scripts/{test,branch-policy,ci-local}.sh` and the githook in one pass,
    so no separate `validate_test_runner` step is needed.
    """
    sh_files = (
        list((repo_root / "scripts").glob("*.sh"))
        + list((repo_root / ".dev-kit" / "hooks").rglob("*.sh"))
        + [repo_root / ".githooks" / "pre-push"]
    )
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
    _ok(f"bash syntax ({len(sh_files)} shell files clean)")
    return True


def main(repo_root: pathlib.Path | None = None) -> int:
    repo_root = repo_root or pathlib.Path.cwd()
    print(f"validate.py — repo_root={repo_root}")
    checks = [
        validate_installation_complete,
        validate_marker,
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
