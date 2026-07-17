#!/usr/bin/env python3
"""
extract-verdict.py — extract the LLM review/security verdict from
anthropics/claude-code-action@v1's output file.

ROOT-CAUSE FIX (issue #244, boilerplate-web PR #17/#19): the previous
post-script extracted the verdict by grepping PR comments for
"Verdict: <value>". That works ONLY when the agent actually posts a
comment with a "Verdict:" line. When the agent posts an inline comment
(mcp__github_inline_comment) or no comment at all, the post-script
falls back to a stale comment from a previous run, causing the
severity gate to flip-flop between Approve / Changes Requested /
Blocked on every push.

This script reads the agent's full output (saved by the action to
$RUNNER_TEMP/claude-execution-output.json or
/home/runner/work/_temp/claude-execution-output.json) and extracts the
LAST assistant text that contains "Verdict: <value>". The action's
output is a JSON-lines stream of messages (init, user, assistant,
result, etc.). The assistant messages contain the model's text output;
the verdict appears in the FINAL assistant message per the prompt
contract.

Robustness:
- If the file is missing, exits 0 with no output (caller falls back).
- If the file is HTML (e.g. 404 from a redirect), exits 0 with no
  output (caller falls back). Detected by checking the first non-blank
  character.
- If the file is JSON but has no Verdict, exits 0 with no output.
- If the file is unreadable, exits 0 with no output (caller falls back).
- Returns exit 0 (not 1) on "not found" so the bash || true at the
  call site can be simplified.

Usage:
  python3 extract-verdict.py <path-to-claude-execution-output.json>

Prints the verdict (Approve|Blocked|Changes Requested) to stdout if
found. Exits 0 always (no verdict on stdout = caller falls back).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VERDICT_RE = re.compile(r'Verdict:\s*(Approve|Blocked|Changes Requested)\b')


def extract(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Bail early if the file looks like an HTML error page (network
    # failure, 404, etc.). JSON-lines from claude-code-action NEVER
    # starts with '<'. The 1KB peek is enough to detect any HTML/XML
    # payload.
    peek = text.lstrip()[:1024]
    if peek.startswith("<") or peek.lower().startswith("<?xml"):
        return ""
    # Also bail if the file is suspiciously small or empty.
    if len(text) < 10:
        return ""
    last_verdict = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Bail on any non-{ line — JSON-lines is strict.
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "assistant":
            continue
        # Content can be in `message.content` (list of content blocks,
        # claude-code SDK) or directly in `content` (string, some
        # wrappers).
        content = msg.get("message", {}).get("content")
        if content is None:
            content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    texts.append(block)
        elif isinstance(content, str):
            texts.append(content)
        for t in texts:
            m = VERDICT_RE.search(t)
            if m:
                last_verdict = m.group(1)
    return last_verdict


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <claude-execution-output.json>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    verdict = extract(path)
    # ALWAYS print to stdout (empty if not found). Caller uses stdout
    # to decide whether to use the file verdict or fall back.
    if verdict:
        print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())