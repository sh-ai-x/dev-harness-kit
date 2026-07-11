#!/usr/bin/env python3
"""
write_project_md.py — CLAUDE.md + AGENTS.md atomic writer (SSOT, MUST-11).

CLAUDE.md sections:
  §1 Iron Laws (5 laws, MUST-8 SSOT)
  §2 Active Stage (state-codec integration)
  §3 Codebase Map (lazy-loading index; opt-in :full writes docs/CODEBASE-MAP.md)
  §4 Hook Matrix (active-hooks.json SSOT, MUST-13)
  §5 Hand-off Pointer

AGENTS.md: full duplicate of CLAUDE.md content, byte-for-byte, for CLIs
(Codex and other AGENTS.md-reading harnesses) that read AGENTS.md directly
instead of CLAUDE.md. Not a pointer: Codex-family tools read AGENTS.md as
their instruction set and won't reliably follow a reference to another
file, so the complete SSOT content has to live in AGENTS.md itself (the
same pattern obra/superpowers uses for its own AGENTS.md/CLAUDE.md pair).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from active_hooks_codec import DEFAULT_MATRIX  # noqa: E402
from atomic import atomic_write_text  # noqa: E402

# §3 tree-walk limits
TREE_DEPTH_MAX = 4
TREE_LINES_MAX = 80
FILES_PER_DIR_MAX = 20
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", ".pytest_cache"}

# Candidate files for existence-based sections
MANIFEST_CANDIDATES = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml"]
CONVENTION_CANDIDATES = [".editorconfig", ".eslintrc.json", ".prettierrc", "pyproject.toml"]
LOCKFILES = [
    ("pnpm-lock.yaml", 30),
    ("package-lock.json", 30),
    ("requirements.txt", 20),
    ("Pipfile.lock", 20),
]

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
    """§3 lazy-loading index.

    Pure reference; no filesystem reads. The agent reads the canonical
    files on demand. `/dev-kit:bootstrap --full-claude-md` dumps the full
    map to `docs/CODEBASE-MAP.md` (separate file) — not inline.
    """
    return (
        "### Manifest\n"
        "- `package.json` / `pyproject.toml` / `go.mod` / `Cargo.toml`\n"
        "\n"
        "### Deps (lockfiles)\n"
        "- `pnpm-lock.yaml` / `package-lock.json` / `requirements.txt` / `Pipfile.lock`\n"
        "\n"
        "### Conventions\n"
        "- `.editorconfig` / `.eslintrc.json` / `.prettierrc` / `pyproject.toml [tool.*]`\n"
        "\n"
        "### Full map (opt-in)\n"
        "- Run `/dev-kit:bootstrap --full-claude-md` → `docs/CODEBASE-MAP.md`\n"
        "- Tree depth 4 + manifest + top-10 deps + conventions are written there.\n"
    )


def render_agents_md(
    project_root: Path,
    stage: str = "bootstrap",
    full_map: bool = False,
    iron_laws: Optional[List[str]] = None,
    hook_matrix: Optional[str] = None,
    hand_off_chain: Optional[str] = None,
) -> str:
    """AGENTS.md payload — byte-identical to CLAUDE.md.

    AGENTS.md is the universal entry point that Codex / other CLIs read
    directly, in place of CLAUDE.md, not in addition to it. A pointer file
    doesn't work here: those tools don't reliably follow a reference to
    another file, so the full SSOT content must live in AGENTS.md itself.
    """
    return render_claude_md(
        project_root,
        stage=stage,
        full_map=full_map,
        iron_laws=iron_laws,
        hook_matrix=hook_matrix,
        hand_off_chain=hand_off_chain,
    )


def write_agents_md(project_root: Path, content: Optional[str] = None) -> Path:
    """Atomic write AGENTS.md. Idempotent.

    `content`, when given, is written as-is (the caller already rendered
    it — see `write_project_md`, which shares one rendered string between
    CLAUDE.md and AGENTS.md so the two never drift on stage/flags). Falls
    back to a fresh `render_agents_md(project_root)` for standalone callers.
    """
    path = project_root / "AGENTS.md"
    atomic_write_text(path, content if content is not None else render_agents_md(project_root))
    return path


CODEBASE_MAP_DOC_REL = "docs/CODEBASE-MAP.md"


def render_codebase_map_doc(project_root: Path) -> str:
    """Full codebase map (tree + manifest + deps + conventions).

    Written to `docs/CODEBASE-MAP.md` when `--full-claude-md` is opted in.
    Not inlined into CLAUDE.md §3 — that's a lazy-loading index.
    """
    tree = _safe_tree(project_root)
    manifest = _safe_existence_list(project_root, MANIFEST_CANDIDATES, "no manifest detected")
    deps = _safe_deps(project_root)
    conventions = _safe_existence_list(project_root, CONVENTION_CANDIDATES, "no conventions file detected")
    header = (
        f"# Codebase Map — {project_root.name}\n\n"
        f"> Generated by `/dev-kit:bootstrap --full-claude-md`. "
        "Reference only; CLAUDE.md §3 holds the lazy-loading index.\n"
    )
    return (
        header
        + f"## Tree (depth 4)\n```\n{tree}\n```\n\n"
        + f"## Manifest\n{manifest}\n\n"
        + f"## External deps (top 10)\n{deps}\n\n"
        + f"## Conventions\n{conventions}\n"
    )


def write_codebase_map_doc(project_root: Path) -> Path:
    """Atomic write docs/CODEBASE-MAP.md. Idempotent."""
    path = project_root / CODEBASE_MAP_DOC_REL
    atomic_write_text(path, render_codebase_map_doc(project_root))
    return path


def render_full_section_3(project_root: Path) -> str:
    """Deprecated alias for `render_codebase_map_doc`. Kept for back-compat.

    The full codebase map is no longer inlined into CLAUDE.md §3 (lazy-loading
    index). Use `render_codebase_map_doc` instead and write to
    `docs/CODEBASE-MAP.md`.
    """
    return render_codebase_map_doc(project_root)


# Credential redaction patterns for tree output (mask secrets in file/dir names)
CREDENTIAL_PATTERNS = (
    re.compile(r"x-access-token:[^@/]+@"),
    re.compile(r"(?i)(token|pat|password|secret|api[_-]?key)[:=][^/\s]+"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),  # GitHub PAT
)


def _redact(s: str) -> str:
    """Mask credential-like substrings with `***`."""
    for pat in CREDENTIAL_PATTERNS:
        s = pat.sub("***", s)
    return s


def _safe_tree(root: Path) -> str:
    try:
        out: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(str(root)):].count(os.sep)
            if depth > TREE_DEPTH_MAX:
                dirnames.clear()
                continue
            # Filter SKIP_DIRS at every depth + any credential-like dir/file
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not any(p.search(d) for p in CREDENTIAL_PATTERNS)
            ]
            # Also filter filenames (.git can be a worktree pointer FILE, not a dir)
            safe_filenames = [
                f for f in filenames
                if f not in SKIP_DIRS and not any(p.search(f) for p in CREDENTIAL_PATTERNS)
            ]
            indent = "  " * depth
            out.append(f"{indent}{os.path.basename(dirpath) or '.'}/")
            for f in sorted(safe_filenames)[:FILES_PER_DIR_MAX]:
                out.append(f"{indent}  {_redact(f)}")
        return "\n".join(out[:TREE_LINES_MAX]) or "(empty)"
    except Exception:
        return "(tree extraction failed — STALE)"


def _safe_existence_list(root: Path, candidates: List[str], fallback: str) -> str:
    found = [f"- `{c}` ✓" for c in candidates if (root / c).exists()]
    return "\n".join(found) if found else f"- {fallback}"


def _safe_deps(root: Path) -> str:
    for filename, n in LOCKFILES:
        path = root / filename
        if path.exists():
            try:
                # Redact each line so credentialed registry URLs (private npm/PyPI
                # indexes with x-access-token:...@) don't leak into CODEBASE-MAP.md.
                lines = [
                    _redact(line) for line in path.read_text(encoding="utf-8").splitlines()[:n]
                ]
                return "\n".join(lines) or f"({filename} empty)"
            except Exception:
                return f"(read failed for {filename})"
    return "- (no lockfile detected)"


def render_hook_matrix_table() -> str:
    """Render the hook matrix markdown table from DEFAULT_MATRIX (single source of truth)."""
    stages = list(DEFAULT_MATRIX.keys())
    hooks = list(DEFAULT_MATRIX[stages[0]].keys())
    header = "| Hook           | " + " | ".join(s.capitalize() for s in stages) + " |"
    sep =    "|----------------|" + "|".join(":----:" for _ in stages) + "|"
    def cell(v):
        return "R" if v == "read-only" else "✅" if v else "-"
    rows = []
    for h in hooks:
        row = f"| {h:<14}  | " + " | ".join(f" {cell(DEFAULT_MATRIX[s][h])}   " for s in stages) + " |"
        rows.append(row)
    return "```\n" + "\n".join([header, sep] + rows) + "\n```\n(R = read-only)"


def render_claude_md(
    project_root: Path,
    stage: str = "bootstrap",
    full_map: bool = False,
    iron_laws: Optional[List[str]] = None,
    hook_matrix: Optional[str] = None,
    hand_off_chain: Optional[str] = None,
) -> str:
    """Compose full CLAUDE.md content.

    §3 is always the lazy-loading index. `full_map` is accepted for back-compat
    but no longer affects §3 content — the full map is written to
    `docs/CODEBASE-MAP.md` separately via `write_project_md(full_map=True)`.
    """
    laws = iron_laws if iron_laws is not None else IRON_LAWS
    section_3 = render_stub_section_3(project_root)
    section_4 = hook_matrix if hook_matrix is not None else render_hook_matrix_table()
    section_5 = hand_off_chain if hand_off_chain is not None else (
        "next_stage_trigger: /dev-kit:ci-setup --force\n"
        "shortcut_trigger: /dev-kit:tdd-fast"
    )

    laws_text = "\n".join(f"- **L{i+1}**: {law}" for i, law in enumerate(laws))

    mode_label = "--slim-claude-md (default, lazy §3)"

    return (
        "<!-- AUTO-GENERATED by lib/write_project_md.py — DO NOT EDIT MANUALLY -->\n"
        "<!-- Use `/dev-kit:bootstrap` to regenerate. `--full-claude-md` writes docs/CODEBASE-MAP.md. -->\n"
        "\n"
        "# CLAUDE.md — project SSOT\n"
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
        "## §3 Codebase Map (lazy-loading index)\n"
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


def write_project_md(project_root: Path, *, full_map: bool = False, stage: str = "bootstrap") -> Path:
    """Atomic write CLAUDE.md + AGENTS.md. Returns CLAUDE.md path.

    If `full_map=True`, also writes `docs/CODEBASE-MAP.md` (the full map;
    CLAUDE.md §3 stays the lazy-loading index regardless).
    """
    path = project_root / "CLAUDE.md"
    content = render_claude_md(project_root, stage=stage, full_map=full_map)
    atomic_write_text(path, content)
    write_agents_md(project_root, content=content)
    if full_map:
        write_codebase_map_doc(project_root)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write CLAUDE.md + AGENTS.md SSOT")
    parser.add_argument("--project-root", default=".", help="project root directory")
    parser.add_argument("--full-claude-md", action="store_true",
                        help="also write docs/CODEBASE-MAP.md (CLAUDE.md §3 stays lazy)")
    parser.add_argument("--stage", default="bootstrap", help="active stage to record in §2")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    p = write_project_md(root, full_map=args.full_claude_md, stage=args.stage)
    print(f"wrote {p}")
    if args.full_claude_md:
        print(f"wrote {root / CODEBASE_MAP_DOC_REL}")
    print(f"wrote {root / 'AGENTS.md'}")
