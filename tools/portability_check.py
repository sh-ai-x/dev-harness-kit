#!/usr/bin/env python3
"""Read-only portability contract check for Claude Code and Codex."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

# A leading `DEV_KIT_AGENT=<value> ` command prefix is an intentional,
# expected divergence between the Claude and Codex hooks.json — each
# runtime stamps its own identity so the harness-effectiveness stability
# submetric (issue #663) can see which agent emitted an event. Strip it
# before comparing signatures, same as the CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT
# normalization below.
_DEV_KIT_AGENT_PREFIX = re.compile(r"^DEV_KIT_AGENT=\S+ ")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hook_signature(config: dict[str, Any]) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    for event, groups in config.get("hooks", {}).items():
        for group in groups or []:
            matcher = group.get("matcher", "")
            for hook in group.get("hooks", []):
                command = hook.get("command", "").replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
                command = _DEV_KIT_AGENT_PREFIX.sub("", command)
                result.add((event, matcher, command))
    return result


def run(project_root: Path) -> dict[str, Any]:
    findings: list[str] = []
    claude_manifest = project_root / ".claude-plugin" / "plugin.json"
    codex_manifest = project_root / ".codex-plugin" / "plugin.json"
    claude_hooks = project_root / "hooks" / "hooks.json"
    codex_hooks = project_root / ".codex-plugin" / "hooks" / "hooks.json"
    manifests = [claude_manifest, codex_manifest, claude_hooks, codex_hooks]
    missing = [str(p.relative_to(project_root)) for p in manifests if not p.is_file()]
    if missing:
        findings.append(f"missing portability contract files: {missing}")
    if not missing:
        cm, xm = _load(claude_manifest), _load(codex_manifest)
        for field in ("name", "version"):
            if cm.get(field) != xm.get(field):
                findings.append(f"manifest drift: {field} differs between Claude and Codex")
        cs, xs = _hook_signature(_load(claude_hooks)), _hook_signature(_load(codex_hooks))
        if cs != xs:
            missing_in_codex = sorted(cs - xs)
            missing_in_claude = sorted(xs - cs)
            if missing_in_codex:
                findings.append(f"hook parity: Codex missing {missing_in_codex}")
            if missing_in_claude:
                findings.append(f"hook parity: Claude missing {missing_in_claude}")

    hooks_dir = project_root / "hooks"
    for script in sorted(hooks_dir.glob("*.sh")) if hooks_dir.is_dir() else []:
        try:
            check = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        except OSError as exc:
            findings.append(f"shell verifier unavailable: {exc}")
            break
        if check.returncode:
            findings.append(f"shell syntax: {script.relative_to(project_root)}: {check.stderr.strip()}")
    return {
        "portable": not findings,
        "providers": ["claude", "codex"],
        "findings": findings,
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    report = run(args.project_root.resolve())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("portable" if report["portable"] else "not portable")
        for finding in report["findings"]:
            print(f"- {finding}")
    return 0 if report["portable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
