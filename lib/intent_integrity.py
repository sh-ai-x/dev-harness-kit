"""intent_integrity.py — PRD ↔ phases consistency check (pre-build).

PR #1 of the intent integrity plan. Pre-build only — reads
`phases/<phase>/index.json` + `step<N>.md` files + `PRD.md`, emits
`Finding` records, and returns exit 2 on any `severity == "high"`.

Pure function `analyze(plan_dir, prd_path) -> list[Finding]`. No side
effects at import time. CLI: `python -m lib.intent_integrity --pre <phase>`.

Post-build checks (IC-5..IC-7) are out of scope for this module — they
arrive in the next PR.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Dual-import so consumer installs that ship `lib/*.py` flat (no
# `__init__.py` in the consumer `lib/`) keep working.
try:
    from .atomic import atomic_write_json  # type: ignore
except ImportError:
    from atomic import atomic_write_json  # type: ignore

# ---------- public data shape ----------


@dataclass
class Finding:
    """Single integrity violation detected by `analyze()`.

    Fields are deliberately distinct from the proposal YAML's `code/target`
    naming so callers can read the file without flipping between two
    vocabularies. `finding_id` carries the IC-N tag; `category` carries
    the human-readable class.
    """

    finding_id: str
    category: str
    severity: str  # "high" | "medium" | "low"
    evidence: str
    action: str
    confirmed: bool = False


# ---------- PRD requirement parsing ----------


_REQ_LINE_RE = re.compile(
    r"^\s*-\s+(?P<id>REQ-[A-Za-z0-9_-]+|MUST|SHALL)\s*[:：]\s*(?P<text>.+?)\s*$"
)


def _parse_prd_requirements(prd_text: str) -> List[tuple]:
    """Extract requirement IDs from PRD markdown.

    A requirement line is `- ` followed by `REQ-N:`, `MUST:`, or `SHALL:`.
    Returns list of `(req_id, line_number, text)`. `MUST:` and `SHALL:`
    without a numeric suffix are disambiguated by their line number so
    the same requirement is not collapsed across two different lines.

    Lines that don't match are silently ignored — the PRD may carry
    headings, notes, and other non-requirement content.
    """
    out: List[tuple] = []
    for lineno, line in enumerate(prd_text.splitlines(), start=1):
        m = _REQ_LINE_RE.match(line)
        if not m:
            continue
        raw_id = m.group("id")
        text = m.group("text")
        # Disambiguate bare MUST / SHALL by line number — two `- MUST: ...`
        # lines are two distinct requirements, not one.
        if raw_id in ("MUST", "SHALL"):
            req_id = f"{raw_id}:L{lineno}"
        else:
            req_id = raw_id
        out.append((req_id, lineno, text))
    return out


# ---------- step file parsing ----------


def _strip_inline_comment(line: str) -> str:
    """Drop `# ...` tail (very light — no quoting, no escapes)."""
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def _parse_step_file(text: str, step_num: int) -> dict:
    """Extract structured fields from a phases/<name>/step<N>.md body.

    Returns dict with keys: name, acceptance (list[str]),
    dependencies (list[int]), verification (str), raw_body (str).
    The list fields are what most checks need; raw_body is kept so
    the orphan-step check can scan the whole file if needed.
    """
    out = {
        "name": "",
        "acceptance": [],
        "dependencies": [],
        "verification": "",
        "raw_body": text,
    }
    section: Optional[str] = None
    for line in text.splitlines():
        stripped = _strip_inline_comment(line).rstrip()
        if not stripped.strip():
            section = None
            continue
        # Top-level `key: value` lines.
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", stripped)
        if m and not stripped.startswith((" ", "\t")):
            key = m.group(1).lower()
            value = m.group(2).strip()
            if key in ("name", "step", "owner", "estimated_complexity"):
                if key == "name":
                    out["name"] = value
                section = None
                continue
            if key in ("acceptance", "dependencies", "verification", "files_touched"):
                section = key
                # Inline value (e.g. `verification: pytest -q`) — capture it.
                if value and key == "verification":
                    out["verification"] = value
                    section = None  # single-line, nothing to accumulate
                elif value and key in ("acceptance", "dependencies"):
                    # `acceptance: foo` is a single-item list.
                    if key == "acceptance":
                        out["acceptance"].append(value)
                    else:
                        out["dependencies"].append(_to_int(value))
                    section = None  # list exhausted
                else:
                    section = key
                continue
            section = None
            continue
        # Indented list item under an active section.
        if section in ("acceptance", "dependencies"):
            item = stripped.lstrip(" \t-").strip()
            if not item:
                continue
            if section == "acceptance":
                out["acceptance"].append(item)
            else:
                out["dependencies"].append(_to_int(item))
        elif section == "verification":
            # Verification is a single command; first non-empty line wins.
            cmd = stripped.lstrip(" \t-").strip()
            if cmd:
                out["verification"] = cmd
                section = None
    return out


def _to_int(value: str) -> int:
    """Best-effort int parse. Falls back to -1 so dependency-gap still fires."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


# ---------- IC-4 command heuristic ----------


# Recognized command verbs / prefixes. Conservative on purpose — an
# unrecognized command is treated as a dead verification (warn) rather
# than trusted. Keep this list small and obvious.
_RECOGNIZED_COMMAND_PATTERNS = (
    re.compile(r"^(?:./\S+|\S*/\S+)"),                # ./script or path/script
    re.compile(r"^\s*(?:pytest|py\.test|tox|nox)\b"),
    re.compile(r"^\s*(?:python|python3|pip|pipx|uv|poetry)\b"),
    re.compile(r"^\s*(?:node|npm|pnpm|yarn|bun|npx|tsx|ts-node)\b"),
    re.compile(r"^\s*(?:go|rustc|cargo|make|cmake|just)\b"),
    re.compile(r"^\s*(?:bash|sh|zsh|fish|pwsh|powershell)\b"),
    re.compile(r"^\s*(?:curl|wget|http)\b"),
    re.compile(r"^\s*(?:git|gh)\b"),
    re.compile(r"^\s*(?:docker|docker compose|podman|nerdctl)\b"),
    re.compile(r"^\s*(?:kubectl|helm|terraform|ansible)\b"),
    re.compile(r"^\s*(?:rsync|cp|mv|rm|mkdir|ls|cat|grep|sed|awk)\b"),
    re.compile(r"^\s*(?:echo|printf|true|false|test)\b"),
    re.compile(r"^\s*(?:ssh|scp|rsync)\b"),
    re.compile(r"^\s*[\$@]\s*\S"),                    # shell var or here-doc start
)


def _looks_like_command(value: str) -> bool:
    """Heuristic: is `value` plausibly a runnable shell command?

    Returns False for empty / whitespace-only / prose. The pattern set
    is intentionally narrow — false negatives (real commands marked
    dead) are easier to fix than false positives (prose treated as
    commands and run).
    """
    v = value.strip()
    if not v:
        return False
    return any(p.match(v) for p in _RECOGNIZED_COMMAND_PATTERNS)


# ---------- core analyze() ----------


def analyze(plan_dir: Path, prd_path: Path) -> List[Finding]:
    """Run all pre-build integrity checks against `plan_dir` + `prd_path`.

    Returns an empty list on a clean plan. Findings are NOT marked
    `confirmed` here — call `mark_confirmed(findings)` to flag
    duplicates, or rely on the CLI driver which calls it for you.

    Order of checks (matches the proposal table): IC-1 → IC-2 →
    IC-3 → IC-4. The order is deterministic so test assertions can
    rely on it.
    """
    findings: List[Finding] = []

    prd_text = prd_path.read_text(encoding="utf-8")
    prd_reqs = _parse_prd_requirements(prd_text)
    prd_id_set = {rid for rid, _, _ in prd_reqs}

    index_path = plan_dir / "index.json"
    if not index_path.exists():
        # Without index.json we can't validate dependencies at all.
        # IC-3 would be meaningless; report nothing rather than guess.
        return findings
    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    step_numbers = [int(s["step"]) for s in index_data.get("steps", [])]

    step_data = {}
    for sn in step_numbers:
        sf = plan_dir / f"step{sn}.md"
        if not sf.exists():
            continue
        step_data[sn] = _parse_step_file(sf.read_text(encoding="utf-8"), sn)

    # IC-1 — missing-requirement (PRD ID not referenced by any step).
    # Word-boundary match: a plain `req_id in text` substring check would
    # treat `REQ-1` as referenced by a step mentioning `REQ-10`. Compile
    # one regex per ID; `\b` rejects the prefix collision.
    acceptance_texts = [
        "\n".join(sd["acceptance"]) for sd in step_data.values()
    ]
    for req_id, _lineno, _text in prd_reqs:
        pattern = re.compile(rf"\b{re.escape(req_id)}\b")
        if not any(pattern.search(text) for text in acceptance_texts):
            findings.append(
                Finding(
                    finding_id="IC-1",
                    category="missing-requirement",
                    severity="high",
                    evidence=f"{req_id} is not referenced by any step acceptance",
                    action=(
                        f"Add an acceptance criterion that references {req_id} "
                        f"in the step that owns this requirement"
                    ),
                )
            )

    # IC-2 — orphan-step (a step whose acceptance does not reference any PRD ID).
    # Skip if PRD has no requirements — every step would be a false positive.
    # Same word-boundary fix as IC-1.
    if prd_reqs:
        rid_patterns = [
            (rid, re.compile(rf"\b{re.escape(rid)}\b")) for rid in prd_id_set
        ]
        for sn, sd in step_data.items():
            joined = "\n".join(sd["acceptance"])
            referenced = any(p.search(joined) for _rid, p in rid_patterns)
            if referenced:
                continue
            name = sd["name"] or f"step{sn}"
            findings.append(
                Finding(
                    finding_id="IC-2",
                    category="orphan-step",
                    severity="high",
                    evidence=(
                        f"step{sn} ({name}) acceptance does not reference any PRD requirement"
                    ),
                    action=(
                        f"Add a PRD reference (e.g. REQ-N) to step{sn} "
                        f"acceptance, or remove the step"
                    ),
                )
            )

    # IC-3 — dependency-gap (step number referenced in `dependencies` not in index).
    for sn, sd in step_data.items():
        for dep in sd["dependencies"]:
            if dep not in step_numbers:
                findings.append(
                    Finding(
                        finding_id="IC-3",
                        category="dependency-gap",
                        severity="medium",
                        evidence=(
                            f"step{sn} depends on step {dep} which is not "
                            f"declared in index.json"
                        ),
                        action=(
                            f"Add step {dep} to index.json, or remove the "
                            f"dependency from step{sn}"
                        ),
                    )
                )

    # IC-4 — dead-verification (verification: line is empty / not a command).
    for sn, sd in step_data.items():
        v = sd["verification"]
        if _looks_like_command(v):
            continue
        shown = v if v else "<empty>"
        findings.append(
            Finding(
                finding_id="IC-4",
                category="dead-verification",
                severity="medium",
                evidence=f"step{sn} verification is {shown!r} — not a runnable command",
                action=(
                    f"Replace step{sn} verification with a runnable shell "
                    f"command (pytest, python, ./script.sh, etc.)"
                ),
            )
        )

    return findings


# ---------- confirmed-flag pass ----------


def mark_confirmed(findings: List[Finding]) -> None:
    """In-place: set `confirmed=True` on every finding whose
    (finding_id, evidence) pair has appeared earlier in the list.

    Run after concatenating findings from multiple invocations
    (e.g. plan-time pre + build-time pre) so repeat findings get
    marked as durable rather than one-shot.
    """
    seen: set = set()
    for f in findings:
        key = (f.finding_id, f.evidence)
        if key in seen:
            f.confirmed = True
        else:
            seen.add(key)


# ---------- CLI ----------


def _write_pre_report(root: Path, phase: str, findings: List[Finding]) -> Path:
    """Persist findings to .dev-kit/integrity/<phase>.pre.json."""
    out = root / ".dev-kit" / "integrity" / f"{phase}.pre.json"
    payload = {
        "phase": phase,
        "mode": "pre",
        "findings": [
            {
                "finding_id": f.finding_id,
                "category": f.category,
                "severity": f.severity,
                "evidence": f.evidence,
                "action": f.action,
                "confirmed": f.confirmed,
            }
            for f in findings
        ],
    }
    atomic_write_json(out, payload)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intent_integrity",
        description="PRD ↔ phases consistency check (pre-build, PR #1).",
    )
    parser.add_argument(
        "--pre",
        metavar="PHASE",
        help="Run pre-build checks against phases/<PHASE>/ + PRD.md",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root (default: cwd)",
    )
    args = parser.parse_args(argv)

    if not args.pre:
        parser.error("--pre <phase> is required (post-build arrives in PR #2)")

    # Path-traversal guard: a malicious phase name (e.g. "../etc") would
    # otherwise escape `.dev-kit/integrity/` and let the gate see a
    # "missing report" → continue-without-check. Restrict to a safe
    # character set; the same regex is enforced in lib/execute.py at the
    # second entry point.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.pre):
        parser.error(f"invalid --pre phase: {args.pre!r} (use letters/digits/._-)")

    root = Path(args.root).resolve()
    plan_dir = root / "phases" / args.pre
    prd_path = root / "PRD.md"

    if not plan_dir.exists():
        print(f"intent_integrity: phase dir not found: {plan_dir}", file=sys.stderr)
        return 2
    if not prd_path.exists():
        print(f"intent_integrity: PRD.md not found: {prd_path}", file=sys.stderr)
        return 2

    findings = analyze(plan_dir, prd_path)
    mark_confirmed(findings)
    _write_pre_report(root, args.pre, findings)

    high = [f for f in findings if f.severity == "high"]
    medium = [f for f in findings if f.severity == "medium"]

    if high:
        print(
            f"intent_integrity: {len(high)} high-severity finding(s) — blocking build:",
            file=sys.stderr,
        )
        for f in high:
            print(
                f"  [{f.finding_id}/{f.category}] {f.evidence}",
                file=sys.stderr,
            )
        return 2

    if medium:
        print(
            f"intent_integrity: {len(medium)} medium-severity warning(s) (not blocking):",
            file=sys.stderr,
        )
        for f in medium:
            print(
                f"  [{f.finding_id}/{f.category}] {f.evidence}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
