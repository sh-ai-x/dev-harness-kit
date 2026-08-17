"""Tests for templates/ci/scripts/extract-verdict.py.

Verifies the verdict extraction contract that review.yml + security
post-steps rely on (issue #244, boilerplate-web PR #19 verification;
issue #612, consumer silent-Approve bug; issue #625, MINIMAX provider
drops assistant stream):

  1. Missing file     → exit 0, empty stdout
  2. HTML file        → exit 0, empty stdout (network error page)
  3. JSONL no verdict → exit 0, prints "PARSE_FAILED" (issue #612)
  4. JSONL one Approve verdict → exit 0, prints "Approve"
  5. JSONL two verdicts (last wins) → exit 0, prints last verdict
  6. Bad usage        → exit 2 (missing arg)

Issue #612 contract: distinguish "I couldn't read the file"
(missing / HTML / unreadable / suspiciously small → stdout="") from
"the file existed but had no recognizable `Verdict:` line"
(stout="PARSE_FAILED"). The latter is hard-failed by the severity
gate so a real review failure can't be papered over as Approve.

Issue #625 contract: when the execution-file verdict is empty OR
PARSE_FAILED AND a PR-comments file is provided as the second arg,
fall back to scanning that file for `Verdict: <value>` lines. The
caller is responsible for filtering by run_id (defeats #244 stale-
comment flap). The file verdict still wins when present.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract-verdict.py"

# Sentinel emitted by extract-verdict.py when the file existed with
# parseable content but no assistant message contained a `Verdict:`
# line. The severity gate's PARSE_FAILED branch hard-fails the gate
# on this string; see review.yml lines ~766-794 and ~600-630 for the
# review + security post-step wiring.
PARSE_FAILED = "PARSE_FAILED"


def _write_jsonl(path: Path, messages: list[dict]) -> None:
    """Write a JSON-lines stream (one JSON object per line)."""
    with path.open("w", encoding="utf-8") as fh:
        for msg in messages:
            fh.write(json.dumps(msg) + "\n")


def _assistant_msg(text: str) -> dict:
    """Mimic a claude-code SDK assistant message with a single text block."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_file(tmp_path: Path) -> None:
    target = tmp_path / "nope.json"
    assert not target.exists()
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout == ""


def test_html_file(tmp_path: Path) -> None:
    target = tmp_path / "err.html"
    target.write_text("<html><body>404 Not Found</body></html>", encoding="utf-8")
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout == ""


def test_suspiciously_small_file(tmp_path: Path) -> None:
    """A file with content but < 10 chars is treated as missing.

    This is the size threshold the script uses to bail early (guards
    against partial-write races where the action started writing but
    didn't finish). Treated as the no-file path so the caller's
    tolerance kicks in.
    """
    target = tmp_path / "tiny.json"
    target.write_text("{}", encoding="utf-8")
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout == ""


def test_jsonl_no_verdict_emits_parse_failed(tmp_path: Path) -> None:
    """Issue #612: assistant message but no `Verdict:` line → PARSE_FAILED.

    Pre-#612 this returned empty stdout, which made the workflow
    silently default to Approve (the consumer bug). Now the sentinel
    hard-fails the gate so the user MUST fix the prompt contract.
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "init"},
            _assistant_msg("Looking at the diff now..."),
            {"type": "result"},
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == PARSE_FAILED


def test_jsonl_only_non_assistant_messages(tmp_path: Path) -> None:
    """JSONL with only init/result (no verdict anywhere) → PARSE_FAILED.

    The agent ran (file existed, was parseable JSON) but produced no
    recognisable `Verdict:` line anywhere. Issue #625 widened the trusted
    message types to include ``result`` (the MINIMAX wrapper summary
    envelope) — but ``result`` here carries no content, so no verdict
    match is found and the #612 contract (parseable JSONL with no verdict
    MUST emit PARSE_FAILED) still holds. User / tool_use messages with
    verdicts are still ignored (see test_non_assistant_messages_ignored).
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "init"},
            {"type": "result", "subtype": "success"},  # no content -> no verdict
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == PARSE_FAILED


def test_jsonl_only_garbled_lines(tmp_path: Path) -> None:
    """File has content but no parseable JSON lines → PARSE_FAILED.

    Distinct from the no-file path (which returns "" so the caller's
    tolerance for genuinely-missing files still applies).
    """
    target = tmp_path / "agent.json"
    target.write_text("not json\nalso not json\n{broken\n", encoding="utf-8")
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == PARSE_FAILED


def test_jsonl_assistant_with_bold_wrapped_verdict(tmp_path: Path) -> None:
    """Bold-wrapped `**Verdict:**` (PR-comment format) is NOT recognized.

    extract-verdict.py only matches the non-bold `Verdict:` form (the
    contract the agent's prompt requires). Bold-wrapped is what the
    PR-comment renderer emits, which the gate's separate comment-body
    parser (`maintenance_gate.py:extract_verdict`) handles. Keeping
    the two parsers distinct avoids the silent-Approve bug from
    issue #612 — if we silently accepted bold-wrapped here, a
    wrapper change that flips one form to the other would still
    silently pass.
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            _assistant_msg("**Verdict:** Approve"),
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == PARSE_FAILED


def test_jsonl_single_approve(tmp_path: Path) -> None:
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "init"},
            _assistant_msg("Review complete.\nVerdict: Approve"),
            {"type": "result"},
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Approve"


def test_jsonl_last_verdict_wins(tmp_path: Path) -> None:
    """Two assistant messages with verdicts — the LAST one wins."""
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            _assistant_msg("First draft:\nVerdict: Approve"),
            _assistant_msg("Revised:\nVerdict: Changes Requested"),
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Changes Requested"


def test_jsonl_all_three_verdicts(tmp_path: Path) -> None:
    """Exercise the full enum — last one wins regardless of order."""
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            _assistant_msg("Verdict: Approve"),
            _assistant_msg("Verdict: Blocked"),
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Blocked"


def test_missing_arg() -> None:
    result = _run([])
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_garbled_jsonl_with_valid_message(tmp_path: Path) -> None:
    """Garbled lines are skipped; valid assistant messages still parsed."""
    target = tmp_path / "agent.json"
    content = (
        "this is not json\n"
        + json.dumps(_assistant_msg("Verdict: Approve"))
        + "\n"
        + "{broken\n"
    )
    target.write_text(content, encoding="utf-8")
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Approve"


def test_non_assistant_messages_ignored(tmp_path: Path) -> None:
    """User / result / tool messages mentioning Verdict are NOT parsed."""
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "user", "content": "Verdict: Blocked (joke)"},
            {"type": "tool_use", "content": "Verdict: Changes Requested"},
            _assistant_msg("Verdict: Approve"),
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Approve"


# ---------------------------------------------------------------------------
# Issue #625: PR-comments fallback tests (MINIMAX provider drops the
# assistant stream from claude-execution-output.json but the agent still
# posts the verdict as a `gh pr comment` body). The caller filters by
# run_id; this contract just verifies the LAST-WINS extraction logic.
# ---------------------------------------------------------------------------


def _write_comments(path: Path, bodies: list[str]) -> None:
    """Write a PR-comments JSON file (array of {body} objects)."""
    path.write_text(
        json.dumps([{"body": b} for b in bodies]),
        encoding="utf-8",
    )


def test_comments_fallback_when_execution_file_missing(tmp_path: Path) -> None:
    """Issue #625: MINIMAX provider path — no execution file, but the
    agent posted the verdict as a PR comment body. The caller filtered
    by run_id; we just scan the file for the LAST Verdict: line."""
    target = tmp_path / "nope.json"  # does not exist
    comments = tmp_path / "comments.json"
    _write_comments(
        comments,
        [
            "<!-- dev-kit-verdict-audit --> run=12345 job=review ...\n",
            "Verdict: Approve\n\n## review summary...\n",
            "<!-- dev-kit-verdict-audit --> run=12345 job=review status=success verdict=Approve source=agent-pr-comment\n",
        ],
    )
    result = _run([str(target), str(comments)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Approve"


def test_comments_fallback_when_execution_file_parse_failed(tmp_path: Path) -> None:
    """Issue #625: MINIMAX execution file has no assistant blocks
    (PARSE_FAILED), but the agent's PR-comment body has the verdict.
    Fall back to comments; should NOT propagate PARSE_FAILED."""
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "preset", "content": "system preset"},
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success"},
        ],
    )
    comments = tmp_path / "comments.json"
    _write_comments(
        comments,
        [
            "<!-- dev-kit-verdict-audit --> run=99 job=review ...\n",
            "Verdict: Changes Requested\n\n## review summary\n",
        ],
    )
    result = _run([str(target), str(comments)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Changes Requested"


def test_file_verdict_wins_over_comments(tmp_path: Path) -> None:
    """Strict superset of pre-#625 behavior: anthropic provider
    produces an assistant message with the verdict; the comments
    file may have stale / different data — the FILE wins."""
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [_assistant_msg("Verdict: Approve")],
    )
    comments = tmp_path / "comments.json"
    _write_comments(
        comments,
        ["Verdict: Blocked\n\n<!-- stale from previous run -->\n"],
    )
    result = _run([str(target), str(comments)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Approve"


def test_comments_fallback_returns_empty_if_nothing(tmp_path: Path) -> None:
    """Both file missing and comments empty → empty stdout (no-file
    tolerance path), NOT PARSE_FAILED. PARSE_FAILED is reserved for
    'agent ran and produced parseable content but no verdict'."""
    target = tmp_path / "nope.json"
    comments = tmp_path / "comments.json"
    _write_comments(comments, ["<!-- just an audit comment, no verdict -->\n"])
    result = _run([str(target), str(comments)])
    assert result.returncode == 0
    assert result.stdout == ""


def test_comments_fallback_last_wins(tmp_path: Path) -> None:
    """Multiple comments with verdicts — LAST one wins (mirrors the
    file-path 'last assistant message wins' semantics)."""
    target = tmp_path / "nope.json"
    comments = tmp_path / "comments.json"
    _write_comments(
        comments,
        [
            "Verdict: Approve\n",
            "Verdict: Changes Requested\n",
            "Verdict: Blocked\n",
        ],
    )
    result = _run([str(target), str(comments)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Blocked"


def test_comments_file_malformed_returns_empty(tmp_path: Path) -> None:
    """Tolerate malformed JSON / non-list shapes — returns empty so
    the caller's no-file tolerance still applies. Never raises."""
    target = tmp_path / "nope.json"
    comments = tmp_path / "comments.json"
    comments.write_text("{not valid json at all", encoding="utf-8")
    result = _run([str(target), str(comments)])
    assert result.returncode == 0
    assert result.stdout == ""


def test_comments_file_missing_returns_empty(tmp_path: Path) -> None:
    """Caller forgot to pass the file — empty stdout, no exception."""
    target = tmp_path / "nope.json"
    result = _run([str(target), str(tmp_path / "missing.json")])
    assert result.returncode == 0
    assert result.stdout == ""



# ---------------------------------------------------------------------------
# Issue #625: MINIMAX provider envelope shapes.
#
# The MINIMAX wrapper (CI_REVIEW_PROVIDER=minimax, via
# https://api.minimax.io/anthropic) drops the assistant-message stream from
# claude-execution-output.json — the file is parseable JSONL but contains
# only `type=preset`, `type=system` init, and `type=result` summary
# messages. The verdict IS in one of those result messages, in one of
# three shapes:
#
#   - top-level `result` string  (the canonical MINIMAX envelope)
#   - top-level `content` list of text blocks  (alternative wrapper)
#   - nested `message.content` list of text blocks  (the claude-code SDK
#     shape the parser already supported — sanity-check it still works
#     on a result-type message that uses the SDK shape)
#
# Pre-#625 the parser only scanned `type=assistant` messages, so the
# MINIMAX envelope always returned PARSE_FAILED even on a clean review,
# hard-failing the severity gate. These tests pin the post-#625 contract.
# ---------------------------------------------------------------------------


def test_minimax_envelope_with_verdict_in_result_string_content(tmp_path: Path) -> None:
    """Issue #625: canonical MINIMAX envelope — verdict in top-level `result`.

    Wrapper emits only preset + system + result summary messages. The
    result message carries the verdict as a bare `result` string (the
    shape MINIMAX uses in production). Parser must extract it so the
    severity gate does not hard-fail on a clean review.
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "preset", "content": "system preset"},
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "subtype": "success",
                "result": "Review complete.\nVerdict: Approve",
            },
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Approve"


def test_minimax_envelope_with_verdict_in_result_content_list(tmp_path: Path) -> None:
    """Issue #625: alternate wrapper shape — verdict in `content` list.

    Some MINIMAX wrapper versions put the summary in the top-level
    `content` field as a list of text blocks (same shape as the
    claude-code SDK `message.content`). Parser must recognise this
    shape on a `type=result` message.
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "preset", "content": "system preset"},
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "subtype": "success",
                "content": [
                    {"type": "text", "text": "Review complete.\nVerdict: Changes Requested"},
                ],
            },
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Changes Requested"


def test_minimax_envelope_no_assistant_messages_no_verdict_falls_to_parse_failed(tmp_path: Path) -> None:
    """Issue #625 + #612: MINIMAX envelope with no verdict anywhere → PARSE_FAILED.

    Preserves the #612 contract — a parseable JSONL with no
    recognisable `Verdict:` line MUST emit PARSE_FAILED so the severity
    gate hard-fails with the dedicated remediation message. The new
    `type=result` trust does NOT weaken this contract: if no candidate
    message carries a verdict, the sentinel still fires (vs the old
    silent-Approve consumer bug).
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "preset", "content": "system preset"},
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success"},  # no result/content -> no verdict
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == PARSE_FAILED


def test_minimax_envelope_last_verdict_in_result_message_wins(tmp_path: Path) -> None:
    """Issue #625: two `type=result` messages, second carries final verdict → second wins.

    Mirrors the assistant-stream "last assistant message wins" semantic
    on the result-type stream: when the MINIMAX wrapper emits multiple
    summary messages (rare but observed), the parser must take the LAST
    one, which is the final emitted verdict.
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "preset", "content": "system preset"},
            {
                "type": "result",
                "subtype": "intermediate",
                "result": "First pass.\nVerdict: Approve",
            },
            {
                "type": "result",
                "subtype": "success",
                "result": "Revised after follow-up.\nVerdict: Blocked",
            },
        ],
    )
    result = _run([str(target)])
    assert result.returncode == 0
    assert result.stdout.strip() == "Blocked"
