#!/usr/bin/env python3
"""Deterministic OWASP-oriented repository security scorecard."""
from __future__ import annotations

import argparse
import datetime as dt
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
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache"}
    return [p for p in root.rglob("*") if p.is_file() and not ignored.intersection(p.parts)]


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
    """Return shipped implementation/config files, not examples or tests."""
    excluded = {"docs", "tests", "eval", "README.md", "README.ko.md"}
    return [(path, text) for path, text in text_files(root)
            if not any(part in excluded for part in path.relative_to(root).parts)]


def scan(root: Path) -> list[Category]:
    data = assessed_files(root)
    all_text = "\n".join(text for _, text in data)
    result = [Category(code, name) for code, name in NAMES.items()]
    by = {item.name: item for item in result}

    if not (root / "SECURITY.md").exists():
        by["Security Misconfiguration"].deduct(10, "SECURITY.md is missing")
    if not (root / "LICENSE").exists():
        by["Security Misconfiguration"].deduct(5, "LICENSE is missing")
    if re.search(r"(?i)(password|secret|api[_-]?key|token)\s*[:=]\s*[\"'][^\"']{8,}", all_text):
        by["Security Misconfiguration"].deduct(20, "possible hardcoded credential pattern")
    if re.search(r"(?i)\b(md5|sha1)\s*\(", all_text):
        by["Cryptographic Failures"].deduct(20, "weak hash call detected")
    if re.search(r"(?i)\beval\s*\(|new\s+Function\s*\(", all_text):
        by["Injection"].deduct(25, "dynamic code evaluation pattern detected")
    if re.search(r"(?i)\bcurl\b[^\n|]*\|\s*(ba)?sh\b", all_text):
        by["Software/Data Integrity Failures"].deduct(25, "network content piped to a shell")
    if re.search(r"(?i)\bshell\s*=\s*True\b", all_text):
        by["Injection"].deduct(20, "shell=True detected")
    if re.search(r"(?i)\bSELECT\b.*\{[^}]+\}", all_text):
        by["Injection"].deduct(20, "SQL-like interpolated query detected")
    if re.search(r"(?m)^\s*except\s*:\s*(?:pass)?\s*$", all_text):
        by["Mishandling Exceptional Conditions"].deduct(15, "bare exception handler detected")
    if re.search(r"(?m)^\s*uses:\s*[^#\n]+@(main|master|v?\d+)(?:\s|$)", all_text):
        by["Software Supply Chain Failures"].deduct(15, "GitHub Action is not pinned to a commit SHA")
    if not any(path.name in {"requirements.lock", "uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"} for path, _ in data):
        by["Software Supply Chain Failures"].deduct(10, "no recognized dependency lockfile")
    if not re.search(r"(?i)\b(timeout|deadline)\b", all_text):
        by["Mishandling Exceptional Conditions"].deduct(10, "no timeout/deadline marker detected")
    if not re.search(r"(?i)\b(log|logger|logging|audit)\b", all_text):
        by["Security Logging and Alerting Failures"].deduct(15, "no logging/audit marker detected")
    if not any(path.name == "dependabot.yml" or ".github/dependabot" in path.as_posix() for path, _ in data):
        by["Software Supply Chain Failures"].deduct(5, "Dependabot configuration is missing")
    return result


def render(root: Path, categories: list[Category]) -> str:
    overall = round(sum(item.score for item in categories) / len(categories))
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    lines = ["# Security Metrics", "", f"- Repository: `{root}`", f"- Generated: `{now}`", f"- Overall score: **{overall}/100**", "", "| OWASP area | Score | Status | Evidence / deductions |", "|---|---:|---|---|"]
    for item in categories:
        evidence = "<br>".join(item.findings) if item.findings else "No deterministic findings"
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
