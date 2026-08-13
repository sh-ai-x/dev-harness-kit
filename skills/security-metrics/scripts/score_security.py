#!/usr/bin/env python3
"""Deterministic OWASP-oriented repository security scorecard."""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Category:
    code: str
    name: str
    score: int = 100
    findings: list[str] = field(default_factory=list)

    def deduct(self, points: int, evidence: str) -> None:
        """Apply a bounded deduction and retain its deterministic evidence."""
        self.score = max(0, self.score - points)
        self.findings.append(f"-{points}: {evidence}")


NAMES = {
    "A01": "Broken Access Control", "A02": "Security Misconfiguration",
    "A03": "Software Supply Chain Failures", "A04": "Cryptographic Failures",
    "A05": "Injection", "A06": "Insecure Design",
    "A07": "Authentication Failures", "A08": "Software/Data Integrity Failures",
    "A09": "Security Logging and Alerting Failures", "A10": "Mishandling Exceptional Conditions",
}


def files(root: Path) -> list[Path]:
    """Return regular repository files while excluding ignored trees and links.

    Excludes:
    - VCS / cache trees (`.git`, `.mypy_cache`, …)
    - Stale agent worktrees (`.worktrees`, `.claude/worktrees`) — those are
      scratch directories left by prior agent runs; scoring them inflates
      the scorecard with false positives (e.g. every stale worktree copies
      the same fixture / regex literal as the canonical source).
    - The scorer output artifact itself (`security-metrics.md`) — it
      contains the report table, which echoes pattern names like
      `shell=True` and triggers self-matching on re-run.
    """
    ignored_dirs = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".worktrees", ".claude",
    }
    ignored_files = {"security-metrics.md"}
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if p.name in ignored_files:
            continue
        parts = set(p.relative_to(root).parts)
        if ignored_dirs.intersection(parts):
            continue
        out.append(p)
    return out


def text_files(root: Path) -> list[tuple[Path, str]]:
    result = []
    for path in files(root):
        if path.suffix.lower() in {".py", ".js", ".ts", ".tsx", ".sh", ".yml", ".yaml", ".json", ".toml", ".md"}:
            try:
                result.append((path, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                pass
    return result


def assessed_files(root: Path) -> list[tuple[Path, str]]:
    """Return shipped implementation/config files, not examples, tests, or fixtures."""
    excluded_components = {"docs", "tests", "eval"}
    excluded_filenames = {"README.md", "README.ko.md"}
    # Path-prefix exclusions — matched against the repo-relative POSIX
    # path, so multi-segment dirs like `skills/review/fixtures` work
    # without flattening every `skills/*` file into an exclusion.
    excluded_prefixes = (
        "skills/review/fixtures/",
        "skills/security-metrics/",
    )
    out: list[tuple[Path, str]] = []
    for path, text in text_files(root):
        rel = path.relative_to(root)
        parts = rel.parts
        if any(p in excluded_components for p in parts):
            continue
        if rel.name in excluded_filenames:
            continue
        rel_posix = rel.as_posix()
        if any(rel_posix.startswith(prefix) for prefix in excluded_prefixes):
            continue
        out.append((path, text))
    return out


def scan(root: Path) -> list[Category]:
    """Score repository contents using the fixed OWASP-oriented rules."""
    data = assessed_files(root)
    all_files = files(root)
    all_text = "\n".join(text for _, text in data)
    result = [Category(code, name) for code, name in NAMES.items()]
    by = {item.name: item for item in result}

    if not (root / "SECURITY.md").exists():
        by["Security Misconfiguration"].deduct(10, "SECURITY.md is missing")
    # Look at all repository files (not just extensioned text files),
    # so an extensionless `LICENSE` file is detected.
    if not any(path.name.lower().startswith("license") for path in all_files):
        by["Security Misconfiguration"].deduct(5, "LICENSE is missing")
    # Match a hardcoded credential-shaped string. Skip values that start with
    # `$` — those are shell variable references (`$VAR`, `${VAR-…}`), not
    # literal secrets. Pattern: `<name>[:=] ['"] <8+ chars not starting with $> ['"]`.
    if re.search(r"(?i)(password|secret|api[_-]?key|token)\s*[:=]\s*[\"'](?!\$)[^\"']{8,}", all_text):
        by["Security Misconfiguration"].deduct(20, "possible hardcoded credential pattern")
    if re.search(r"(?i)\b(md5|sha1)\s*\(", all_text):
        by["Cryptographic Failures"].deduct(20, "weak hash call detected")
    if re.search(r"(?i)\beval\s*\(|new\s+Function\s*\(", all_text):
        by["Injection"].deduct(25, "dynamic code evaluation pattern detected")
    if re.search(r"(?i)\bcurl\b[^\n|]*\|\s*(ba)?sh\b", all_text):
        by["Software/Data Integrity Failures"].deduct(25, "network content piped to a shell")
    if re.search(r"(?i)\bshell\s*=\s*True\b", all_text):
        by["Injection"].deduct(20, "shell=True detected")
    # Match only SQL-shaped interpolation: SELECT ... FROM ... WHERE/INTO ... {var}.
    # `jq`'s `select(... | contains(...)) | {body: .body}` queries look
    # superficially similar but lack the FROM keyword, so they're filtered
    # out by the FROM requirement below.
    if re.search(r"(?i)\bSELECT\b[^\n]*\bFROM\b[^\n]*\{[^}]+\}", all_text):
        by["Injection"].deduct(20, "SQL-like interpolated query detected")
    if re.search(r"(?m)^\s*except\s*:\s*(?:pass)?\s*$", all_text):
        by["Mishandling Exceptional Conditions"].deduct(15, "bare exception handler detected")
    if re.search(r"(?m)^\s*uses:\s*[^#\n]+@(main|master|v?\d+)(?:\s|$)", all_text):
        by["Software Supply Chain Failures"].deduct(15, "GitHub Action is not pinned to a commit SHA")
    if not re.search(r"(?i)\b(allowlist|denylist|authorization|permission|rbac|acl)\b", all_text):
        by["Broken Access Control"].deduct(10, "no authorization or permission-control marker detected")
    if not re.search(r"(?i)\b(validate|invariant|threat model|security requirement)\b", all_text):
        by["Insecure Design"].deduct(10, "no validation, invariant, or threat-model marker detected")
    if not re.search(r"(?i)\b(auth|authentication|session|oauth|oidc|mfa|password)\b", all_text):
        by["Authentication Failures"].deduct(10, "no authentication or session-control marker detected")
    if not any(path.name in {"requirements.lock", "uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"} for path in all_files):
        by["Software Supply Chain Failures"].deduct(10, "no recognized dependency lockfile")
    if not re.search(r"(?i)\b(timeout|deadline)\b", all_text):
        by["Mishandling Exceptional Conditions"].deduct(10, "no timeout/deadline marker detected")
    if not re.search(r"(?i)\b(log|logger|logging|audit)\b", all_text):
        by["Security Logging and Alerting Failures"].deduct(15, "no logging/audit marker detected")
    if not any(path.name == "dependabot.yml" or ".github/dependabot" in path.as_posix() for path in all_files):
        by["Software Supply Chain Failures"].deduct(5, "Dependabot configuration is missing")
    return result


def render(root: Path, categories: list[Category]) -> str:
    """Render a byte-stable Markdown scorecard for already-scanned categories."""
    overall = round(sum(item.score for item in categories) / len(categories))
    lines = ["# Security Metrics", "", f"- Repository: `{root}`", f"- Overall score: **{overall}/100**", "", "| OWASP area | Score | Status | Evidence / deductions |", "|---|---:|---|---|"]
    for item in categories:
        evidence = "<br>".join(item.findings).replace("|", "\\|") if item.findings else "No deterministic findings"
        status = "PASS" if item.score == 100 else "REVIEW"
        lines.append(f"| {item.code} {item.name} | {item.score}/100 | {status} | {evidence} |" )
    lines += ["", "> This is a deterministic triage metric, not a security certification. Run `/dev-kit:security` for the full OWASP evidence review.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    report = render(root, scan(root))
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
