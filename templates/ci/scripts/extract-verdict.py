#!/usr/bin/env python3
"""
extract-verdict.py — extract the LLM review/security verdict from
anthropics/claude-code-action@v1's output file, with a PR-comments
fallback for providers that drop the assistant stream.

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

ISSUE #625 — MINIMAX PROVIDER FALLBACK
The MINIMAX provider (CI_REVIEW_PROVIDER=minimax, via
https://api.minimax.io/anthropic) drops the assistant-message stream
from `claude-execution-output.json` — the file is parseable JSONL but
contains only `type: "preset"`, `type: "system"` init, and
`type: "result"` summary messages. The agent DOES post the verdict as
a PR comment body, but `extract()` returns PARSE_FAILED because there
is no assistant text block. This script now also scans `type=result`
messages (see CANDIDATE_MSG_TYPES) so MINIMAX wrappers can recover the
verdict directly from the summary envelope when the comments-fallback
path is unavailable.

This script accepts an optional SECOND argument — a path to a JSON file
containing the PR comments for the current run (already filtered by
the caller to ONLY include comments whose body contains the current
`run=<GITHUB_RUN_ID>`). If the execution-file extraction returns
empty OR PARSE_FAILED, the script falls back to scanning those
comments for `Verdict: <value>`. The run-id filter is what defeats
the #244 stale-comment flap: by construction only comments posted in
THIS run are candidates.

CONTRACT (issue #612, consumer PR silent-Approve bug):
  - file missing / HTML / unreadable / suspiciously small → stdout=""
    (caller treats as the genuine "no-file" path; tolerance is
    appropriate because it usually means a transient filesystem /
    network problem)
  - file exists, parseable JSON, but no assistant message contains a
    recognizable `Verdict:` line → stdout="PARSE_FAILED"
    (the agent ran and produced JSON output, but did not emit the
    verdict contract — caller hard-fails the gate so the user MUST
    fix the prompt contract instead of silently letting Approve pass;
    see review.yml gate's `PARSE_FAILED` branch for the remediation
    message)
  - file exists, parseable JSON, with `Verdict:` in an assistant
    message → stdout=verdict (last one wins)
  - NEW (issue #625): if the file verdict is empty OR PARSE_FAILED
    AND a PR-comments file is provided as the second argument, fall
    back to scanning those comments for the verdict. The PR-comments
    file must be filtered by run_id by the caller (otherwise the
    #244 stale-comment flap returns).

The "PARSE_FAILED" sentinel is what enables the gate to distinguish
"agent ran but didn't follow the verdict contract" from "agent's output
file is genuinely missing" — the two failure modes deserve different
treatment (hard-fail vs. tolerance).

Robustness:
- If the file is missing, exits 0 with no output (caller falls back).
- If the file is HTML (e.g. 404 from a redirect), exits 0 with no
  output (caller falls back). Detected by checking the first non-blank
  character.
- If the file is parseable JSON but no Verdict, exits 0 with the
  PARSE_FAILED sentinel (caller hard-fails the gate — see CONTRACT
  above; this is the fix for the consumer silent-Approve bug).
- If the file is unreadable, exits 0 with no output (caller falls back).
- If a PR-comments file is provided and the file verdict is empty /
  PARSE_FAILED, scan those comments for the LAST `Verdict:` line.
  Caller is responsible for filtering by run_id (#244 defeat).
- Returns exit 0 (not 1) on "not found" or "parse failed" so the
  bash || true at the call site can stay simple.

Usage:
  python3 extract-verdict.py <path-to-claude-execution-output.json>
                             [<path-to-pr-comments-this-run.json>]

Prints the verdict (Approve|Blocked|Changes Requested), the sentinel
`PARSE_FAILED`, or nothing (empty stdout = caller falls back to no-file
path). Exits 0 always.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VERDICT_RE = re.compile(r'Verdict:\s*(Approve|Blocked|Changes Requested)\b')

# Issue #625 (MINIMAX provider): the wrapper drops the assistant-message
# stream and emits only `type=result` summary messages. The verdict IS
# in one of those result messages (top-level `result` string, top-level
# `content` string, top-level `content` list of text blocks, or the
# nested `message.content` shape the claude-code SDK uses). Scan both
# `assistant` and `result` so MINIMAX wrappers don't fall through to
# PARSE_FAILED on a clean review.
CANDIDATE_MSG_TYPES = ("assistant", "result")

# Sentinel emitted when the agent's output file exists and is parseable
# JSONL but no candidate message contains a `Verdict:` line. The
# review.yml severity gate has a dedicated branch that hard-fails with
# a remediation message when this sentinel shows up in the verdict
# output (see the `PARSE_FAILED` arm of the combined verdict gate).
PARSE_FAILED = "PARSE_FAILED"


def _extract_texts(msg: dict) -> list[str]:
    """Collect every candidate text string from a message envelope.

    Tries the three shapes the wrappers actually emit, in order:

      1. ``msg["message"]["content"]`` — claude-code SDK and the
         original /dev-kit:* agent stream. May be a list of
         ``{"type": "text", "text": ...}`` blocks or a bare string.
      2. ``msg["content"]`` — some wrappers flatten content to the top
         level. Same list-or-string contract as (1).
      3. ``msg["result"]`` — MINIMAX wrapper summary envelope (issue
         #625). Bare string only.

    Returns a list of strings (possibly empty). Never raises; any
    unparseable shape is silently skipped so a malformed envelope
    degrades to ``PARSE_FAILED`` rather than crashing the post-step.

    The verdict regex scans each string in order, so the LAST
    `Verdict:` line across all three sources within a single message
    wins. That matches the agent-stream contract ("last assistant
    message wins") extended one level down to "last text source wins
    within the last candidate message".
    """
    texts: list[str] = []

    # 1. claude-code SDK / original agent stream: message.content.
    # Guard with isinstance() because `msg.get("message", {})` only
    # substitutes the default when the KEY is absent — `{"message": null}`
    # returns None and `{"message": "..."}` returns a str; both then
    # fail the chained `.get("content")` with AttributeError, contradicting
    # the "Never raises" docstring contract above. Issue #625 review (P1).
    message = msg.get("message")
    message_content = message.get("content") if isinstance(message, dict) else None
    if isinstance(message_content, list):
        for block in message_content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                texts.append(block)
    elif isinstance(message_content, str):
        texts.append(message_content)

    # 2. Top-level content (wrappers that flatten the envelope).
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                texts.append(block)
    elif isinstance(content, str) and content:
        texts.append(content)

    # 3. MINIMAX wrapper summary (issue #625) — top-level `result`
    # string. Collected in addition to (1) and (2) so a message that
    # has BOTH a chat history and a summary envelope is fully scanned.
    result_text = msg.get("result")
    if isinstance(result_text, str) and result_text:
        texts.append(result_text)

    return texts


def extract(path: Path) -> str:
    """Read the agent's execution file and extract the LAST `Verdict: <value>`.

    Returns "" if the file is missing / HTML / unreadable / suspiciously small.
    Returns PARSE_FAILED if the file is parseable JSONL but contains no
    recognizable `Verdict:` line in any assistant message.
    Returns the verdict string otherwise.
    """
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
        # Issue #625: trust both `assistant` (claude-code SDK / original
        # agent stream) and `result` (MINIMAX wrapper summary envelope).
        # Other message types — user, tool_use, system, preset, init —
        # are intentionally ignored; user/tool_use in particular MUST
        # stay ignored (issue #612 test_non_assistant_messages_ignored
        # contract) so a user-quoted or tool-echoed verdict line cannot
        # satisfy the gate. Only the model's actual emitted text counts.
        if msg.get("type") not in CANDIDATE_MSG_TYPES:
            continue
        # Issue #625 review (P1): a `type=result` envelope may carry
        # `is_error: true` or `subtype: error_max_turns` /
        # `error_during_execution` on aborted runs. Trusting such a
        # message would let a partial summary that happens to contain
        # `Verdict: Approve` slip through after the agent was cut off.
        # Skip error-flagged envelopes so an aborted run still resolves
        # to PARSE_FAILED (the pre-#625 behaviour).
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
    # Issue #612 fix: file passed the basic shape checks (exists, not
    # HTML, has content) but no candidate message (assistant or result,
    # see CANDIDATE_MSG_TYPES) contained a recognizable `Verdict:` line.
    # Either the JSONL was garbled, the agent didn't emit a verdict, or
    # the wrapper changed format — in all cases we cannot trust a
    # missing-verdict default. Emit the PARSE_FAILED sentinel so the
    # gate hard-fails with the dedicated remediation message instead of
    # silently defaulting to Approve (the old consumer-facing bug). The
    # no-file / HTML / unreadable cases above still return "" so the
    # caller can keep its genuine no-file tolerance path.
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
            f"usage: {sys.argv[0]} <claude-execution-output.json> "
            f"[<pr-comments-this-run.json>]",
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
