#!/usr/bin/env python3
"""
extract-verdict.py — extract the LLM review/security verdict from
anthropics/claude-code-action@v1's output file, with a PR-comments
fallback for providers that drop the assistant stream.

Reads the action's JSON-lines output
($RUNNER_TEMP/claude-execution-output.json) and extracts the LAST
``Verdict: <value>`` line across ``assistant`` and ``result``
messages. The MINIMAX provider (issue #625, via
https://api.minimax.io/anthropic) drops the assistant stream, so the
parser also scans ``type=result`` summary envelopes — otherwise MINIMAX
wrappers fall through to PARSE_FAILED on a clean review.

CONTRACT (issues #244, #612, #625):
  - file missing / HTML / unreadable / suspiciously small → stdout=""
    (caller's no-file tolerance path)
  - file exists, parseable, no ``Verdict:`` in any candidate message →
    stdout="PARSE_FAILED" (hard-fail so the user MUST fix the prompt
    contract, not silently let Approve pass; the #612 silent-Approve
    consumer bug fix)
  - file exists, parseable, ``Verdict:`` present → stdout=verdict
    (last one wins)
  - if file verdict is empty OR PARSE_FAILED AND a PR-comments file is
    provided as the second argument, fall back to scanning those
    comments. Caller MUST have filtered by run_id — otherwise the #244
    stale-comment flap returns.

Usage:
  python3 extract-verdict.py <claude-execution-output.json>
                             [<pr-comments-this-run.json>]

Exits 0 always (bash ``|| true`` at the call site).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VERDICT_RE = re.compile(r"Verdict:\s*(Approve|Blocked|Changes Requested)\b")

# Issue #625: MINIMAX wrapper emits only `type=result` summary messages;
# the verdict is in one of those. Other message types (user, tool_use,
# system, preset, init) MUST stay ignored (issue #612 — a user-quoted or
# tool-echoed verdict line cannot satisfy the gate).
CANDIDATE_MSG_TYPES = ("assistant", "result")

# Sentinel for "file existed, parseable, but no `Verdict:` line". The
# severity gate's PARSE_FAILED branch hard-fails the gate on this; see
# review.yml's combined-verdict-gate PARSE_FAILED arm.
PARSE_FAILED = "PARSE_FAILED"


def _collect_texts_from(value: object) -> list[str]:
    """Flatten a list-of-blocks OR bare-string value into a list of texts.

    Each block in a list may be either ``{"type": "text", "text": ...}``
    or a bare string. Returns ``[]`` for ``None``, numbers, dicts (other
    than the per-block contract), and the empty string — so an unknown
    shape silently degrades to no contribution rather than crashing.
    """
    if isinstance(value, list):
        out: list[str] = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(str(block.get("text", "")))
            elif isinstance(block, str):
                out.append(block)
        return out
    if isinstance(value, str) and value:
        return [value]
    return []


def _extract_texts(msg: dict) -> list[str]:
    """Collect every candidate text string from a message envelope.

    Tries (in order): ``msg["message"]["content"]`` (SDK),
    ``msg["content"]`` (flattened), ``msg["result"]`` (MINIMAX summary,
    issue #625). The LAST ``Verdict:`` line across all three within one
    message wins — "last text source wins within the last candidate
    message". Never raises.
    """
    # `msg.get("message", {})` only substitutes the default when the
    # KEY is absent — `{"message": null}` / `{"message": "..."}` would
    # crash the chained `.get("content")`. isinstance guard (P1, #625).
    message = msg.get("message")
    message_content = message.get("content") if isinstance(message, dict) else None

    texts: list[str] = []
    texts.extend(_collect_texts_from(message_content))  # SDK / agent stream
    texts.extend(_collect_texts_from(msg.get("content")))  # flattened envelope
    texts.extend(_collect_texts_from(msg.get("result")))  # MINIMAX summary (#625)
    return texts


def extract(path: Path) -> str:
    """Read the agent's execution file and extract the LAST `Verdict: <value>`.

    Returns "" if the file is missing / HTML / unreadable / suspiciously small.
    Returns PARSE_FAILED if the file is parseable JSONL but no candidate
    message contains a `Verdict:` line. Returns the verdict string otherwise.
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Bail on HTML/XML (network error pages, 404 redirects). JSON-lines
    # NEVER starts with '<'.
    peek = text.lstrip()[:1024]
    if peek.startswith("<") or peek.lower().startswith("<?xml"):
        return ""
    # Suspiciously small / empty → treat as missing.
    if len(text) < 10:
        return ""
    last_verdict = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("type") not in CANDIDATE_MSG_TYPES:
            continue
        # P1 (#625 review): skip error-flagged envelopes — an aborted
        # run's partial summary may carry a `Verdict:` line that was
        # emitted before the agent was cut off, which we MUST NOT trust.
        if msg.get("is_error") is True:
            continue
        subtype = msg.get("subtype")
        if isinstance(subtype, str) and subtype.startswith("error_"):
            continue
        texts = _extract_texts(msg)
        for t in texts:
            m = VERDICT_RE.search(t)
            if m:
                last_verdict = m.group(1)
    # No candidate message contained a `Verdict:` line — emit the
    # sentinel so the gate hard-fails (issue #612 fix; the no-file /
    # HTML / unreadable cases above still return "" for the caller's
    # tolerance path).
    if not last_verdict:
        return PARSE_FAILED
    return last_verdict


def extract_from_comments(path: Path) -> str:
    """Issue #625: scan PR-comments JSON for the LAST `Verdict:` line.

    The CALLER is responsible for filtering by run_id — otherwise the
    #244 stale-comment flap returns (this is exactly what the old
    `gh pr comment --jq` grep did, and it broke boilerplate-web PR #18
    by picking up a stale `Verdict: Changes Requested` from a previous
    push). The review.yml wrapper builds the comments file with:

        gh api .../issues/$PR_NUMBER/comments \\
            --jq '.[] | select(.body | contains("run=$RUN_ID")) | {body: .body}'

    so only comments from THIS run are candidates.

    Expected JSON shape: array of objects with a `body` string field.
    Tolerant of unknown shapes — returns "" on any parse error so the
    caller's no-file fallback still works.

    Returns the LAST `Verdict: <value>` line found, or "" if none.
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    if not text:
        return ""
    try:
        comments = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(comments, list):
        return ""
    last_verdict = ""
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body", "")
        if not isinstance(body, str):
            continue
        # Scan each comment body for a verdict line. The agent's
        # summary comment body starts with a single-line "Verdict:"
        # preamble followed by the review content; the audit comment
        # has no verdict line at all. The regex is the same as the
        # execution-file path so the verdict semantics match.
        m = VERDICT_RE.search(body)
        if m:
            last_verdict = m.group(1)
    return last_verdict


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(
            f"usage: {sys.argv[0]} <claude-execution-output.json> [<pr-comments-this-run.json>]",
            file=sys.stderr,
        )
        return 2
    file_path = Path(sys.argv[1])
    verdict = extract(file_path)

    # Issue #625 fallback: if the execution-file verdict is empty or
    # PARSE_FAILED, AND a PR-comments file is provided, scan those
    # comments for the verdict. Caller MUST have filtered by run_id
    # (see extract_from_comments docstring for the rationale).
    if (not verdict or verdict == PARSE_FAILED) and len(sys.argv) >= 3:
        comments_path = Path(sys.argv[2])
        comments_verdict = extract_from_comments(comments_path)
        if comments_verdict:
            verdict = comments_verdict

    # ALWAYS print to stdout (empty if not found). Caller uses stdout
    # to decide whether to use the file verdict or fall back.
    if verdict:
        print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
