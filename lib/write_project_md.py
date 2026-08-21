#!/usr/bin/env python3
"""
write_project_md.py — CLAUDE.md + AGENTS.md + index.md atomic writer (SSOT, MUST-11).

CLAUDE.md is a minimal pointer document. Detailed content lives in dedicated
index files, generated alongside CLAUDE.md:

  iron-laws/index.md  — Iron Laws (MUST-8 SSOT)
  guidelines/index.md — Behavioral coding guidelines (Karpathy-style, abbreviated)
  hooks/index.md      — Hook matrix + hook shell reference (MUST-13 SSOT)
  rules/index.md      — Shared rules (only if rules/ exists)
  docs/CODEBASE-MAP.md — Full codebase tree (only on --full-claude-md)

AGENTS.md is a 1-line symlink to CLAUDE.md for CLIs that read AGENTS.md.
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
L6_ALPHA_DECLARE = "New skills must declare `alpha: state|enforcement|analysis` in frontmatter. Reasoning-only `analysis` skills are tolerated only for distinct user intents — minimize new instances."
L7_ALPHA_LOCATION = "A skill's alpha lives in the parts the model can't self-impose (deterministic enforcement, stateful processes, audit artifacts). Don't spend alpha on reasoning the next-gen model will absorb."
L8_PROSE_TRIM = "Skill prompt prose that duplicates state-machine / hook / gate behavior must be trimmed. The state machine is the contract; prose is just orientation. Don't restate the contract in prose — reference the SSOT."

IRON_LAWS: List[str] = [
    L1_NO_TEST_NO_CODE,
    L2_ROOT_CAUSE_FIRST,
    L3_EVIDENCE_BEFORE_DONE,
    L4_NO_STUB,
    L5_LEAN_OUTPUT,
    L6_ALPHA_DECLARE,
    L7_ALPHA_LOCATION,
    L8_PROSE_TRIM,
]

# Behavioral coding guidelines (Karpathy-style, abbreviated).
# Stable; not project-specific. Mirrors upstream guidance:
# https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md
G1_THINK_BEFORE_CODING = (
    "State assumptions explicitly. Surface tradeoffs. Ask when uncertain. "
    "If multiple interpretations exist, present them. If a simpler approach "
    "exists, say so."
)
G2_SIMPLICITY_FIRST = (
    "Minimum code that solves the problem. No speculative features. No "
    "abstractions for single-use code. No error handling for impossible "
    "scenarios. If 200 lines could be 50, rewrite."
)
G3_SURGICAL_CHANGES = (
    "Touch only what you must. Match existing style. Don't refactor "
    "unrelated code. Every changed line should trace to the user's request."
)
G4_GOAL_DRIVEN = (
    "Define success criteria before coding. Loop until verified. Strong "
    "criteria enable independent iteration. Weak criteria require constant "
    "clarification."
)

GUIDELINES: List[str] = [
    G1_THINK_BEFORE_CODING,
    G2_SIMPLICITY_FIRST,
    G3_SURGICAL_CHANGES,
    G4_GOAL_DRIVEN,
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


def write_agents_md(project_root: Path) -> Path:
    """Create the AGENTS.md -> CLAUDE.md compatibility symlink."""
    path = project_root / "AGENTS.md"
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to("CLAUDE.md")
    return path


CODEBASE_MAP_DOC_REL = "docs/CODEBASE-MAP.md"


def render_codebase_map_doc(project_root: Path) -> str:
    """Full codebase map (tree + manifest + deps + conventions).

    Written to `docs/CODEBASE-MAP.md` when `--full-claude-md` is opted in.
    CLAUDE.md is a slim pointer that references this file lazily.
    """
    tree = _safe_tree(project_root)
    manifest = _safe_existence_list(project_root, MANIFEST_CANDIDATES, "no manifest detected")
    deps = _safe_deps(project_root)
    conventions = _safe_existence_list(project_root, CONVENTION_CANDIDATES, "no conventions file detected")
    header = (
        f"# Codebase Map — {project_root.name}\n\n"
        f"> Generated by `/dev-kit:bootstrap --full-claude-md`. "
        "Reference only; CLAUDE.md holds a slim pointer that lazy-loads this file on demand.\n"
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


def render_codebase_map_stub() -> str:
    """docs/CODEBASE-MAP.md placeholder (default mode).

    Always written so CLAUDE.md's `docs/CODEBASE-MAP.md` link resolves.
    Full content is opt-in via `/dev-kit:bootstrap --full-claude-md`,
    which calls `render_codebase_map_doc()` instead.
    """
    return (
        "# Codebase Map (lazy placeholder)\n"
        "\n"
        "> Run `/dev-kit:bootstrap --full-claude-md` to (re)generate this\n"
        "> file with the full 4-section map (Tree via `os.walk` depth 4,\n"
        "> Manifest, Deps top-10, Conventions). Default bootstrap keeps\n"
        "> this stub so CLAUDE.md's reference always resolves; the heavy\n"
        "> content is opt-in.\n"
    )


def write_codebase_map_stub(project_root: Path) -> Path:
    """Atomic write the docs/CODEBASE-MAP.md placeholder. Idempotent.

    Always called by `write_project_md()` regardless of `full_map`. When
    `full_map=True`, `write_codebase_map_doc()` overwrites this stub with
    the full content.
    """
    path = project_root / CODEBASE_MAP_DOC_REL
    atomic_write_text(path, render_codebase_map_stub())
    return path


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
    except (OSError, ValueError):
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
            except (OSError, UnicodeDecodeError):
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


def _summarize_rule(path: Path) -> str:
    """Extract a 1-line summary from a rule file's first non-heading paragraph.

    Skips YAML frontmatter (`---\\n...\\n---`), the H1 title, and any blank
    lines / setext-underline after it; returns the first substantive paragraph
    truncated to 120 chars.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "(unreadable)"
    lines = text.splitlines()
    # Strip YAML frontmatter if present (opens with --- on line 1).
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[i + 1:]
                break
    in_body = False
    for raw in lines:
        line = raw.strip()
        if not in_body:
            if line.startswith("#"):
                in_body = True
                continue
            if not line:
                continue
            in_body = True
        if line.startswith("#"):
            continue
        if not line:
            continue
        if line.startswith("---") or set(line) == {"="}:
            continue
        summary = line
        if len(summary) > 120:
            summary = summary[:117] + "..."
        return summary
    return "(no body)"


def render_iron_laws_index() -> str:
    """iron-laws/index.md body — MUST-8 SSOT, 8 laws with descriptions."""
    items = "\n".join(f"- **L{i+1}**: {law}" for i, law in enumerate(IRON_LAWS))
    return (
        "# Iron Laws (SSOT — MUST-8)\n"
        "\n"
        "> Source of truth for project invariants. Read once before any decision.\n"
        "\n"
        f"{items}\n"
        "\n"
        "(hooks emit \"Iron Law #N violation\" stderr only. Bodies not duplicated.)\n"
    )


def render_guidelines_index() -> str:
    """guidelines/index.md body — Karpathy-style behavioral guidelines, abbreviated.

    Stable; not project-specific. Mirrors upstream guidance:
    https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md
    """
    items = "\n".join(f"- **G{i+1}**: {g}" for i, g in enumerate(GUIDELINES))
    return (
        "# Coding Guidelines (behavioral)\n"
        "\n"
        "> Abbreviated from andrej-karpathy-skills. Merge with project Iron Laws.\n"
        "> Tradeoff: bias toward caution over speed. Use judgment for trivial tasks.\n"
        "\n"
        f"{items}\n"
    )


def render_rules_index(project_root: Path) -> str:
    """rules/index.md body — pointer to each rule file with 1-line summary.

    Returns empty string when no `rules/` directory exists (caller skips the
    write).
    """
    rules_dir = project_root / "rules"
    if not rules_dir.is_dir():
        return ""
    files = sorted(p for p in rules_dir.glob("*.md") if p.name != "index.md")
    if not files:
        return ""
    items = "\n".join(
        f"- [`{f.name}`]({f.name}) — {_summarize_rule(f)}"
        for f in files
    )
    return (
        "# Shared rules (Claude Code + Codex)\n"
        "\n"
        "AGENTS.md → CLAUDE.md → these rule files. Read each before planning or editing.\n"
        "\n"
        f"{items}\n"
    )


def render_hooks_index() -> str:
    """hooks/index.md body — hook matrix table + hook shell reference.

    Matrix is sourced from `lib.active_hooks_codec.DEFAULT_MATRIX` (single source
    of truth). Hook shell descriptions are mirrored from
    `skills/bootstrap/SKILL.md` to keep them co-located with the actual shells.
    """
    matrix = render_hook_matrix_table()
    shells = _render_hook_shell_reference()
    return (
        "# Hooks (SSOT)\n"
        "\n"
        "> Matrix state lives in `.dev-kit/.active-hooks.json` (MUST-13).\n"
        "> Shells live in `hooks/*.sh` and are wired via `hooks/hooks.json`.\n"
        "\n"
        "## Hook matrix (per stage)\n"
        "\n"
        f"{matrix}\n"
        "\n"
        "## Hook shells\n"
        "\n"
        f"{shells}\n"
    )


def _render_hook_shell_reference() -> str:
    """Render the hook shell reference table (purpose + stage ON)."""
    rows = [
        "| Hook | Stage ON | Purpose |",
        "|------|----------|---------|",
        "| `tdd-guard` | build | active when `lib/methodology/tdd.py` is loaded (MUST-48). |",
        "| `bash-guard` | build | blocks dangerous shell patterns (`rm -rf`, force-push, etc.). |",
        "| `secret-scan` | build / review / security | PostToolUse credential-pattern grep. |",
        "| `slop-detector` | build / review / security | KO+EN banned-phrase scan. |",
        "| `stop-verify` | plan / design / build / review / security / ship | Stop hook: AC claim verification. |",
        "| `worktree-guard` | n/a | PreToolUse Edit/Write block on main checkout (this repo). |",
        "| `git-guard` | n/a | PreToolUse Bash block on `git commit`/`push` to main. |",
    ]
    return "\n".join(rows)


def write_iron_laws_index(project_root: Path) -> Path:
    """Atomic write iron-laws/index.md. Always writes (universal SSOT)."""
    path = project_root / "iron-laws" / "index.md"
    atomic_write_text(path, render_iron_laws_index())
    return path


def write_guidelines_index(project_root: Path) -> Path:
    """Atomic write guidelines/index.md. Always writes (universal guidance)."""
    path = project_root / "guidelines" / "index.md"
    atomic_write_text(path, render_guidelines_index())
    return path


def write_rules_index(project_root: Path) -> Optional[Path]:
    """Atomic write rules/index.md. No-op when no `rules/` directory exists."""
    body = render_rules_index(project_root)
    if not body:
        return None
    path = project_root / "rules" / "index.md"
    atomic_write_text(path, body)
    return path


def write_hooks_index(project_root: Path) -> Path:
    """Atomic write hooks/index.md. Always writes (universal hook SSOT)."""
    path = project_root / "hooks" / "index.md"
    atomic_write_text(path, render_hooks_index())
    return path


_HEADER = (
    "<!-- AUTO-GENERATED by lib/write_project_md.py — DO NOT EDIT MANUALLY -->\n"
    "<!-- Use `/dev-kit:bootstrap` to regenerate. -->\n"
    "\n"
    "# CLAUDE.md — project SSOT pointer\n"
    "\n"
    "> Minimal pointer document. Detailed content lives in the linked index files.\n"
    "> Read each linked file on demand; do not duplicate content here.\n"
    "\n"
    "---\n"
)
_FOOTER = "\n<!-- END AUTO-GENERATED -->\n"


def render_claude_md(
    project_root: Path,
    stage: str = "bootstrap",
    full_map: bool = False,
    iron_laws: Optional[List[str]] = None,
    hook_matrix: Optional[str] = None,
    hand_off_chain: Optional[str] = None,
) -> str:
    """Compose CLAUDE.md content. Thin dispatcher — emits a references block only.

    `stage`, `iron_laws`, `hook_matrix`, `hand_off_chain` are accepted for
    back-compat but no longer affect output (the stage / laws / matrix all live
    in dedicated index files now). `full_map` is accepted for back-compat but
    the full map is written to `docs/CODEBASE-MAP.md` separately via
    `write_project_md(full_map=True)`.
    """
    refs = [
        "- **Iron Laws** → [`iron-laws/index.md`](iron-laws/index.md) (MUST-8 SSOT)",
        "- **Coding guidelines** → [`guidelines/index.md`](guidelines/index.md) (Karpathy-style, abbreviated)",
        "- **Codebase map** → [`docs/CODEBASE-MAP.md`](docs/CODEBASE-MAP.md) "
        "(regenerate via `/dev-kit:bootstrap --full-claude-md`)",
        "- **Hook matrix** → [`hooks/index.md`](hooks/index.md) (MUST-13 SSOT; state in `.dev-kit/.active-hooks.json`)",
        "- **Hand-off** → `.dev-kit/hand-off/`",
        "- **Shared rules** → [`rules/index.md`](rules/index.md) (only if `rules/` exists)",
    ]
    body = "## References\n\n" + "\n".join(refs) + "\n"
    return f"{_HEADER}\n{body}{_FOOTER}"


def write_project_md(project_root: Path, *, full_map: bool = False, stage: str = "bootstrap") -> Path:
    """Atomic write CLAUDE.md + AGENTS.md + the four index.md files.

    Always writes: CLAUDE.md, AGENTS.md (symlink), iron-laws/index.md,
    guidelines/index.md, hooks/index.md. Conditionally writes:
    rules/index.md (only when `rules/` exists), docs/CODEBASE-MAP.md
    (only when `full_map=True`).
    """
    path = project_root / "CLAUDE.md"
    content = render_claude_md(project_root, stage=stage, full_map=full_map)
    atomic_write_text(path, content)
    write_agents_md(project_root)
    write_iron_laws_index(project_root)
    write_guidelines_index(project_root)
    write_hooks_index(project_root)
    write_rules_index(project_root)
    # Always emit a stub so CLAUDE.md's `docs/CODEBASE-MAP.md` link
    # resolves on a fresh bootstrap. `--full-claude-md` overwrites the
    # stub with the full 4-section map.
    write_codebase_map_stub(project_root)
    if full_map:
        write_codebase_map_doc(project_root)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write CLAUDE.md + AGENTS.md + index.md files")
    parser.add_argument("--project-root", default=".", help="project root directory")
    parser.add_argument("--full-claude-md", action="store_true",
                        help="also write docs/CODEBASE-MAP.md")
    parser.add_argument("--stage", default="bootstrap",
                        help="active stage (back-compat; no longer inlined in CLAUDE.md)")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    p = write_project_md(root, full_map=args.full_claude_md, stage=args.stage)
    print(f"wrote {p}")
    for sub in ("iron-laws/index.md", "guidelines/index.md", "hooks/index.md", "AGENTS.md"):
        print(f"wrote {root / sub}")
    if (root / "rules").is_dir():
        print(f"wrote {root / 'rules' / 'index.md'}")
    if args.full_claude_md:
        print(f"wrote {root / CODEBASE_MAP_DOC_REL} (full map; overwrote stub)")
    else:
        print(f"wrote {root / CODEBASE_MAP_DOC_REL} (stub; pass --full-claude-md for the full map)")
