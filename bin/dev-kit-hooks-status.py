#!/usr/bin/env python3
"""Report the local Claude Code, Codex, and Git hook status."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def git_config(root: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "config", "--get", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def hook_events(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(data.get("hooks", {}).keys())


def codex_hooks_path(root: Path, manifest: Path) -> Path:
    """Resolve the Codex hook file relative to the plugin package root."""
    try:
        hooks_ref = json.loads(manifest.read_text(encoding="utf-8")).get("hooks", "")
    except (OSError, json.JSONDecodeError):
        return root / ".codex-plugin" / "hooks" / "hooks.json"
    return root / hooks_ref if isinstance(hooks_ref, str) and hooks_ref else root / ".codex-plugin" / "hooks" / "hooks.json"


def _pr_gate_line(root: Path, timeout_s: float = 2.0) -> str:
    """Run the read-only `bin/babysit-pr-local-status.py` SSOT and
    return its single-line stdout. Empty string on any failure.

    The script is fail-soft by contract (exits 0 unconditionally, emits
    `?` glyphs when `gh` is unavailable), so we only need to bound its
    runtime so the statusLine doesn't hang on a wedged `gh`.
    """
    script = root / "bin" / "babysit-pr-local-status.py"
    if not script.is_file():
        return ""
    try:
        proc = subprocess.run(
            ["python3", str(script), str(root)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env={**__import__("os").environ, "BABYSIT_STATUS_NO_COLOR": "1"},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    # The script is single-line by contract; take the first non-empty
    # line and strip ANSI in case the env var did not take.
    out = proc.stdout or ""
    for line in out.splitlines():
        line = line.strip()
        if line:
            # Strip ANSI SGR escapes for environments that render the
            # statusLine without color (e.g. some terminals, log files).
            import re

            return re.sub(r"\x1b\[[0-9;]*m", "", line)
    return ""


def status(root: Path) -> dict[str, object]:
    hooks_json = root / "hooks" / "hooks.json"
    claude_manifest = root / ".claude-plugin" / "plugin.json"
    codex_manifest = root / ".codex-plugin" / "plugin.json"
    codex_hooks_json = codex_hooks_path(root, codex_manifest)
    pre_push_hook = root / ".githooks" / "pre-push"
    pre_commit_hook = root / ".githooks" / "pre-commit"
    configured_path = git_config(root, "core.hooksPath")
    configured_dir = Path(configured_path)
    if configured_path and not configured_dir.is_absolute():
        configured_dir = root / configured_dir
    configured_pre_push = configured_dir / "pre-push" if configured_path else None
    configured_pre_commit = configured_dir / "pre-commit" if configured_path else None
    pre_push_active = bool(configured_pre_push and configured_pre_push.is_file())
    pre_commit_active = bool(configured_pre_commit and configured_pre_commit.is_file())

    codex_registered = False
    try:
        codex_registered = codex_hooks_json.is_file()
    except OSError:
        pass

    return {
        "root": str(root),
        "source_hooks": {
            "path": str(hooks_json),
            "exists": hooks_json.is_file(),
            "events": hook_events(hooks_json),
        },
        "claude": {
            "manifest": claude_manifest.is_file(),
            "hooks_registered": hooks_json.is_file(),
        },
        "codex": {
            "manifest": codex_manifest.is_file(),
            "hooks_registered": codex_registered,
            "hooks_path": str(codex_hooks_json),
            "trust": "review with /hooks if new or changed",
        },
        "git": {
            "pre_push_file": pre_push_hook.is_file(),
            "configured_hooks_path": configured_path or None,
            "configured_pre_push": str(configured_pre_push) if configured_pre_push else None,
            "pre_push_active": pre_push_active,
            "pre_commit_file": pre_commit_hook.is_file(),
            "configured_pre_commit": str(configured_pre_commit) if configured_pre_commit else None,
            "pre_commit_active": pre_commit_active,
        },
        "active_hooks_matrix": (root / ".dev-kit" / ".active-hooks.json").is_file(),
        "pr_gate": _pr_gate_line(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root (default: current directory)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    args = parser.parse_args()
    result = status(args.root.resolve())
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"root: {result['root']}")
        print(f"Claude Code: {'registered' if result['claude']['hooks_registered'] else 'not registered'}")
        print(f"Codex:       {'registered' if result['codex']['hooks_registered'] else 'not registered'} (trust: {result['codex']['trust']})")
        print(f"Git pre-commit: {'active' if result['git']['pre_commit_active'] else 'inactive'}")
        print(f"Git pre-push: {'active' if result['git']['pre_push_active'] else 'inactive'}")
        print(f"Matrix:      {'present' if result['active_hooks_matrix'] else 'missing'} (.dev-kit/.active-hooks.json)")
        print(f"Events:      {', '.join(result['source_hooks']['events']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
