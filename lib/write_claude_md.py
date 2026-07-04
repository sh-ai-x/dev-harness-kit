#!/usr/bin/env python3
"""
write_claude_md.py — CLAUDE.md §1~§5 atomic writer (SSOT, MUST-11).

5 sections:
  §1 Iron Laws (5 laws, MUST-8 SSOT)
  §2 Active Stage (state-codec integration)
  §3 Codebase Map (5-line STUB default, opt-in :full)
  §4 Hook Matrix (active-hooks.json SSOT, MUST-13)
  §5 Hand-off Pointer
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

# Iron Law definitions (MUST-8 SSOT)
L1_NO_TEST_NO_CODE = "No prod code without verification artifact (test/contract/domain/scenario/feature per methodology)"
L2_ROOT_CAUSE_FIRST = "No fix without reproducing the bug (Phase 1 = reproduce)"
L3_EVIDENCE_BEFORE_DONE = "No completion claim without quoted exit code / test count / build log"
L4_NO_STUB = "No TODO/FIXME/'we'll extend later'/'this is a starting point'"
L5_LEAN_OUTPUT = "No option/alternative list when not asked. One answer."

IRON_LAWS: List[str] = [
    L1_NO_TEST_NO_CODE,
    L2_ROOT_CAUSE_FIRST,
    L3_EVIDENCE_BEFORE_DONE,
    L4_NO_STUB,
    L5_LEAN_OUTPUT,
]


def render_stub_section_3(project_root: Path) -> str:
    """5-line STUB (default `--slim-claude-md`). Compact 1-line tree + opt-in marker."""
    return (
        "```\n"
        f"{project_root}\n"
        "  ├─ .claude-plugin/{marketplace,plugin/{plugin,hooks}}.json\n"
        "  ├─ skills/<skill-name>/SKILL.md  (flat, 1 level; category in frontmatter)\n"
        "  ├─ commands/<cmd>.md  (15 commands, 0-arg)\n"
        "  ├─ lib/{state_codec,active_hooks_codec,write_claude_md,...}.py\n"
        "  └─ eval/{golden,prompts,fixtures}/\n"
        "```\n"
        "<!-- Run `/dev-kit:bootstrap --full-claude-md` to embed complete tree (depth 4), manifest, deps -->\n"
    )


def render_full_section_3(project_root: Path) -> str:
    """Full 4-section codebase map."""
    tree = _safe_tree(project_root)
    manifest = _safe_manifest(project_root)
    deps = _safe_deps(project_root)
    conventions = _safe_conventions(project_root)
    return (
        f"### Tree (depth 4)\n```\n{tree}\n```\n\n"
        f"### Manifest\n{manifest}\n\n"
        f"### External deps (top 10)\n{deps}\n\n"
        f"### Conventions\n{conventions}\n"
    )


def _safe_tree(root: Path) -> str:
    try:
        out: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(str(root)):].count(os.sep)
            if depth > 4:
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", ".pytest_cache"}]
            indent = "  " * depth
            out.append(f"{indent}{os.path.basename(dirpath) or '.'}/")
            for f in sorted(filenames)[:20]:
                out.append(f"{indent}  {f}")
        return "\n".join(out[:80]) or "(empty)"
    except Exception:
        return "(tree extraction failed — STALE)"


def _safe_manifest(root: Path) -> str:
    candidates = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml"]
    found = []
    for c in candidates:
        if (root / c).exists():
            found.append(f"- `{c}` ✓")
    return "\n".join(found) if found else "- (no manifest detected)"


def _safe_deps(root: Path) -> str:
    candidates = [
        ("pnpm-lock.yaml", 30),
        ("package-lock.json", 30),
        ("requirements.txt", 20),
        ("Pipfile.lock", 20),
    ]
    for filename, n in candidates:
        f = root / filename
        if f.exists():
            try:
                lines = f.read_text(encoding="utf-8").splitlines()[:n]
                return "\n".join(lines) or f"({filename} empty)"
            except Exception:
                return f"(read failed for {filename})"
    return "- (no lockfile detected)"


def _safe_conventions(root: Path) -> str:
    items = []
    for c in [".editorconfig", ".eslintrc.json", ".prettierrc", "pyproject.toml"]:
        if (root / c).exists():
            items.append(f"- `{c}` ✓")
    if not items:
        items.append("- (no conventions file detected)")
    return "\n".join(items)


def render_claude_md(
    project_root: Path,
    stage: str = "bootstrap",
    full_map: bool = False,
    iron_laws: Optional[List[str]] = None,
    hook_matrix: Optional[str] = None,
    hand_off_chain: Optional[str] = None,
) -> str:
    """Compose full CLAUDE.md content."""
    laws = iron_laws if iron_laws is not None else IRON_LAWS
    section_3 = render_full_section_3(project_root) if full_map else render_stub_section_3(project_root)
    section_4 = hook_matrix if hook_matrix is not None else (
        "```\n"
        "| Hook           | Boot | Plan | Design | Build | Review | Security | Ship |\n"
        "|----------------|:----:|:----:|:------:|:-----:|:------:|:--------:|:----:|\n"
        "| tdd-guard      |  -   |  -   |   -    |  ✅   |   -    |    -     |  -   |\n"
        "| bash-guard     |  -   |  -   |   -    |  ✅   |   -    |    -     |  -   |\n"
        "| secret-scan    |  R   |  -   |   -    |  ✅   |   ✅   |    ✅    |  -   |\n"
        "| slop-detector  |  -   |  -   |   -    |  ✅   |   ✅   |    ✅    |  -   |\n"
        "| stop-verify    |  -   |  ✅  |   ✅   |  ✅   |   ✅   |    ✅    |  ✅  |\n"
        "```\n"
        "(R = read-only)"
    )
    section_5 = hand_off_chain if hand_off_chain is not None else (
        "next_stage_trigger: /dev-kit:plan\n"
        "shortcut_trigger: /dev-kit:tdd-fast"
    )

    laws_text = "\n".join(f"- **L{i+1}**: {law}" for i, law in enumerate(laws))

    mode_label = "--full-claude-md" if full_map else "--slim-claude-md (default)"

    return (
        "<!-- AUTO-GENERATED by lib/write_claude_md.py — DO NOT EDIT MANUALLY -->\n"
        "<!-- Use `/dev-kit:bootstrap --refresh-claude-md` or `--slim|--full` to regenerate. -->\n"
        "\n"
        "# CLAUDE.md — dev-harness-kit codebase map + iron laws\n"
        "\n"
        f"> Last generated by `/dev-kit:bootstrap`. Mode: {mode_label}.\n"
        "> Manual edit blocked by hook (use `--strict` or explicit refresh).\n"
        "\n"
        "---\n"
        "\n"
        "## §1 Iron Laws (SSOT — single source, MUST-8)\n"
        "\n"
        f"{laws_text}\n"
        "\n"
        "(hooks emit \"Iron Law #N violation\" stderr only. Bodies not duplicated.)\n"
        "\n"
        "## §2 Active Stage\n"
        "\n"
        f"- current_stage: {stage}\n"
        "- current_step: 1/1\n"
        "- methodology: tdd (MUST-48 default)\n"
        "- shortcut_used: none\n"
        "\n"
        "## §3 Codebase Map\n"
        "\n"
        f"{section_3}"
        "\n"
        "## §4 Hook Matrix (active-hooks.json SSOT, MUST-13)\n"
        "\n"
        f"{section_4}\n"
        "\n"
        "## §5 Hand-off Pointer\n"
        "\n"
        f"{section_5}\n"
        "\n"
        "<!-- END AUTO-GENERATED -->\n"
    )


def write_claude_md(project_root: Path, *, full_map: bool = False, stage: str = "bootstrap") -> Path:
    """Atomic write CLAUDE.md. Returns path."""
    path = project_root / "CLAUDE.md"
    content = render_claude_md(project_root, stage=stage, full_map=full_map)
    fd, tmp = tempfile.mkstemp(dir=project_root, prefix=".CLAUDE.md.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write CLAUDE.md SSOT")
    parser.add_argument("--project-root", default=".", help="project root directory")
    parser.add_argument("--full-map", action="store_true", help="embed full codebase map (default: STUB)")
    parser.add_argument("--stage", default="bootstrap", help="active stage to record in §2")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    p = write_claude_md(root, full_map=args.full_map, stage=args.stage)
    print(f"wrote {p}")
