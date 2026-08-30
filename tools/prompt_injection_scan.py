#!/usr/bin/env python3
"""Prompt-injection static scanner.

Scans arbitrary text (PR body, PR diff, WebFetch output, sub-agent output,
``gh api`` JSON) for known adversarial-instruction patterns before the text
is injected into an LLM context. Companion to:

- ``hooks/injection-content-guard.sh`` (channel-level sibling hook)
- ``.github/workflows/review.yml`` ``injection_scan`` job (PR-time gate)
- ``bin/review-local.sh`` ``RUN_INJECTION_SCAN=1`` flag (local mirror)

Usage::

    python tools/prompt_injection_scan.py --text "$PR_BODY"            # stdin
    python tools/prompt_injection_scan.py --file path/to/diff.patch   # file
    python tools/prompt_injection_scan.py --json                      # JSON envelope

Exit codes:
    0 = Approve (clean OR only Low-severity hits)
    1 = Changes Requested (Medium hits)
    2 = Blocked (Critical hits — fail the gate)

Stdlib only. Deterministic. No network, no model call, sub-second on
multi-MB PRs.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

# Severity tiers. Critical = hard fail (exit 2). Medium = soft fail (exit 1).
# Low = log only. The gate's worst-of-wins ranker collapses the lower two
# into Approve; only Critical flips the verdict to Blocked.
CRITICAL = "critical"
MEDIUM = "medium"
LOW = "low"

# Pre-compiled patterns. Each entry: (compiled regex, severity, name, why).
# Adding a new pattern here is the only edit needed to extend detection.
PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    # --- CRITICAL: explicit instruction-override attempts ---
    (
        re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|directives?|guidance)\b"),
        CRITICAL,
        "ignore-previous-instructions",
        "Attempts to override prior instructions in the prompt.",
    ),
    (
        re.compile(r"(?i)\bdisregard\s+(?:all\s+)?(?:previous|prior|above|the)\s+(?:instructions?|prompts?|rules?|directives?)\b"),
        CRITICAL,
        "disregard-previous",
        "Disregard-framing instruction override.",
    ),
    (
        re.compile(r"(?i)\bforget\s+(?:everything|all)\b.*\b(?:instructions?|prompts?|context)\b"),
        CRITICAL,
        "forget-everything",
        "Forget-context instruction override.",
    ),
    (
        re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the)\s+\w+"),
        CRITICAL,
        "you-are-now",
        "Role-reassignment attempt.",
    ),
    (
        re.compile(r"(?i)\bact\s+as\s+(?:a|an|the)\s+(?:system|admin|administrator|root|jailbreak)\b"),
        CRITICAL,
        "act-as-admin",
        "Privilege-escalation role-play attempt.",
    ),
    (
        re.compile(r"(?i)\bpretend\s+(?:to\s+be|you\s+are)\b"),
        CRITICAL,
        "pretend-to-be",
        "Identity-override attempt.",
    ),
    # --- CRITICAL: explicit model-role markers (inter-format injection) ---
    (
        re.compile(r"<\|(?:im_start|im_end|system|endoftext|pad|sep)\|>"),
        CRITICAL,
        "chatml-marker",
        "ChatML/Special-token marker (cross-format role injection).",
    ),
    (
        re.compile(r"\[INST\]|\[/INST\]"),
        CRITICAL,
        "llama-inst-marker",
        "Llama-2 [INST] role marker (cross-format role injection).",
    ),
    (
        re.compile(r"(?im)^\s*(?:system|assistant|human)\s*:\s*[A-Za-z]"),
        CRITICAL,
        "role-label-prefix",
        "Single-line role-label prefix (e.g. 'System: ').",
    ),
    (
        re.compile(r"###\s*(?:Instruction|Response|System|Assistant|Human)\s*:"),
        CRITICAL,
        "markdown-role-header",
        "Markdown role-header (Instruction:/Response: pattern).",
    ),
    (
        re.compile(r"<\|?system\|?>|<\|?assistant\|?>"),
        CRITICAL,
        "system-tag-marker",
        "HTML-like system/assistant role tag.",
    ),
    # --- MEDIUM: indirect / shell-metachar smuggling + suspicious encodings ---
    (
        re.compile(r"(?i)\b(?:execute|run|eval)\s*\(\s*(?:curl|wget|fetch|http)\b"),
        MEDIUM,
        "shell-fetch-exec",
        "Direct curl/wget → execute pattern.",
    ),
    (
        re.compile(r"(?i)curl\s+[^\n]*\|\s*(?:sh|bash|zsh|python)\b"),
        MEDIUM,
        "curl-pipe-shell",
        "curl|sh classic RCE-via-fetch pattern.",
    ),
    (
        re.compile(r"\bbase64\s+(-d|--decode)"),
        MEDIUM,
        "base64-decode",
        "Inline base64 decode invocation.",
    ),
    # Long base64 blob (>= 200 chars, alphanumeric+=/+). Sentinel for
    # smuggled payloads. Skips short base64 (legitimate e.g. short hashes).
    (
        re.compile(r"\b[A-Za-z0-9+/]{200,}={0,2}\b"),
        MEDIUM,
        "long-base64-blob",
        "Long base64 string (possible smuggled payload).",
    ),
    (
        re.compile(r"(?i)\bdo\s+not\s+(?:tell|inform|mention|reveal|disclose)\s+(?:the\s+)?user\b"),
        MEDIUM,
        "hide-from-user",
        "Instruction to conceal actions from the user.",
    ),
    (
        re.compile(r"(?i)\bbypass\s+(?:the\s+)?(?:filter|safety|guard|policy|restriction)\b"),
        MEDIUM,
        "bypass-guard",
        "Guard/safety-bypass attempt.",
    ),
    # --- LOW: suspicious-but-not-blockable signals (logged for review) ---
    (
        re.compile(r"(?i)\bprompt\s+injection\b"),
        LOW,
        "self-reference",
        "Self-referential 'prompt injection' mention (often a tell).",
    ),
    (
        re.compile(r"(?i)\bjailbreak\b"),
        LOW,
        "jailbreak-mention",
        "Jailbreak vocabulary present.",
    ),
)


@dataclass(frozen=True)
class Hit:
    name: str
    severity: str
    span: tuple[int, int]
    excerpt: str
    why: str

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["span"] = list(self.span)
        return d


@dataclass
class ScanResult:
    verdict: str
    critical: int = 0
    medium: int = 0
    low: int = 0
    hits: list[Hit] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "critical": self.critical,
            "medium": self.medium,
            "low": self.low,
            "hits": [h.to_dict() for h in self.hits],
        }


def _excerpt(text: str, start: int, end: int, pad: int = 40) -> str:
    """Trim ``text[start:end]`` with ``pad`` chars of context either side.

    Collapses interior whitespace and caps total length at ~160 chars so
    the JSON envelope stays readable.
    """
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    chunk = text[lo:hi]
    chunk = re.sub(r"\s+", " ", chunk)
    if len(chunk) > 200:
        chunk = chunk[:197] + "..."
    return chunk


def _scan(text: str) -> ScanResult:
    hits: list[Hit] = []
    for pattern, severity, name, why in PATTERNS:
        for m in pattern.finditer(text):
            hits.append(
                Hit(
                    name=name,
                    severity=severity,
                    span=(m.start(), m.end()),
                    excerpt=_excerpt(text, m.start(), m.end()),
                    why=why,
                )
            )

    # Dedup identical (name, start) tuples — different patterns occasionally
    # match overlapping spans (e.g. "ignore previous instructions" vs.
    # "disregard prior directives"). Keep the worst-severity copy.
    severity_rank = {CRITICAL: 2, MEDIUM: 1, LOW: 0}
    seen: dict[tuple[str, int], Hit] = {}
    for h in hits:
        key = (h.name, h.span[0])
        prev = seen.get(key)
        if prev is None or severity_rank[h.severity] > severity_rank[prev.severity]:
            seen[key] = h
    hits = sorted(
        seen.values(),
        # Critical (rank 2) first, then medium, then low; ties broken by span.
        key=lambda h: (-severity_rank[h.severity], h.span[0]),
    )

    critical = sum(1 for h in hits if h.severity == CRITICAL)
    medium = sum(1 for h in hits if h.severity == MEDIUM)
    low = sum(1 for h in hits if h.severity == LOW)

    if critical:
        verdict = "Blocked"
    elif medium:
        verdict = "Changes Requested"
    else:
        verdict = "Approve"

    return ScanResult(verdict=verdict, critical=critical, medium=medium, low=low, hits=hits)


def _decode_base64_blobs(text: str) -> str:
    """Return ``text`` with every base64-like blob decoded and re-injected.

    Catches the common smuggling trick where a long ``data:base64,...`` blob
    contains instructions that the regex alone misses. We are NOT
    re-scanning the decoded text — that risks false positives on legitimate
    binary content. The function exists so callers can pass ``--decode``
    when they want strict checking.

    Note on the regex: ``[A-Za-z0-9+/]`` matches the base64 alphabet but
    ``=`` (the padding char) is *not* in that class. We therefore capture
    body + padding in a single group, anchored on the right by either a
    non-base64-non-equals character or end-of-string, so the trailing
    ``=`` chars are included in the match. We then right-pad to a
    multiple of 4 before ``b64decode`` (which requires 4-aligned input).
    """
    out: list[str] = []
    cursor = 0
    blob = re.compile(r"\b([A-Za-z0-9+/]{200,}={0,2})(?:[^A-Za-z0-9+/=]|$)", re.MULTILINE)
    for m in blob.finditer(text):
        body = m.group(1)
        out.append(text[cursor : m.start()])
        # Right-pad to a multiple of 4 so b64decode is happy.
        padded = body + "=" * ((4 - len(body) % 4) % 4)
        try:
            decoded = base64.b64decode(padded, validate=False).decode("utf-8", "replace")
            out.append(f"<decoded base64 len={len(decoded)}> {decoded[:120]}")
        except Exception:
            out.append(body)
        cursor = m.end()
    out.append(text[cursor:])
    return "".join(out)


def _read_input(args: argparse.Namespace) -> str:
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8", errors="replace")
    if args.text is not None:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("ERROR: no input (--text / --file / stdin)", file=sys.stderr)
    sys.exit(2)


def _format_text(result: ScanResult) -> str:
    lines = [f"**Verdict:** {result.verdict}"]
    lines.append(
        f"counts: critical={result.critical} medium={result.medium} low={result.low}"
    )
    if result.hits:
        lines.append("hits:")
        for h in result.hits:
            lines.append(f"  [{h.severity:8s}] {h.name} @ {h.span[0]}:{h.span[1]}")
            lines.append(f"    why: {h.why}")
            lines.append(f"    excerpt: {h.excerpt!r}")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prompt-injection static scanner.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--text", help="scan a literal string")
    src.add_argument("--file", help="scan a file path")
    parser.add_argument("--json", action="store_true", help="emit JSON envelope")
    parser.add_argument(
        "--decode", action="store_true", help="also decode+rescan base64 blobs (strict)"
    )
    args = parser.parse_args(argv)

    text = _read_input(args)
    if args.decode:
        text = _decode_base64_blobs(text)
    result = _scan(text)

    if args.json:
        sys.stdout.write(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(_format_text(result) + "\n")

    # Exit code is the gate signal:
    #   0 = Approve       (GH-Actions step passes)
    #   1 = Changes*      (GH-Actions step fails non-fatally → ranks worst-of)
    #   2 = Blocked       (GH-Actions step fails → gate fails fast)
    if result.verdict == "Blocked":
        return 2
    if result.verdict == "Changes Requested":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
