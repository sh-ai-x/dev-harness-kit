"""Tests for templates/ci/scripts/extract-verdict.py.

Verifies the verdict extraction contract that review.yml + security
post-steps rely on (issue #244, boilerplate-web PR #19 verification;
issue #612, consumer silent-Approve bug):

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
    """JSONL with only init/result (no assistant messages) → PARSE_FAILED.

    The agent ran (file existed, was parseable JSON) but produced no
    assistant text at all. Same outcome as no-Verdict: hard-fail.
    """
    target = tmp_path / "agent.json"
    _write_jsonl(
        target,
        [
            {"type": "init"},
            {"type": "result"},
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
