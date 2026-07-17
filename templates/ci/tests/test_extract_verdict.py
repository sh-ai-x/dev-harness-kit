"""Tests for templates/ci/scripts/extract-verdict.py.

Verifies the verdict extraction contract that review.yml + security
post-steps rely on (issue #244, boilerplate-web PR #19 verification):

  1. Missing file     → exit 0, empty stdout
  2. HTML file        → exit 0, empty stdout (network error page)
  3. JSONL no verdict → exit 0, empty stdout
  4. JSONL one Approve verdict → exit 0, prints "Approve"
  5. JSONL two verdicts (last wins) → exit 0, prints last verdict
  6. Bad usage        → exit 2 (missing arg)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract-verdict.py"


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


def test_jsonl_no_verdict(tmp_path: Path) -> None:
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
    assert result.stdout == ""


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


def test_garbled_jsonl_skipped(tmp_path: Path) -> None:
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