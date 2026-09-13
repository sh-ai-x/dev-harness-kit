"""Black-box contracts for the interactive trace evidence producers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "hooks"


def _run(hook: str, payload: dict, root: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "DEV_KIT_TRACE_ROOT": str(root),
        "DEV_KIT_AGENT": "pytest",
        "DEV_KIT_WORKFLOW_ID": "producer-test",
        **env_extra,
    })
    return subprocess.run(
        ["bash", str(HOOKS / hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=20,
    )


def _events(root: Path) -> list[dict]:
    path = root / ".dev-kit" / "trace" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_payload(root: Path, content: str = "changed") -> dict:
    return {
        "tool_name": "Write",
        "cwd": str(root),
        "session_id": "producer-session",
        "tool_input": {"file_path": str(root / "src" / "example.py"), "content": content},
    }


def _verify_payload(root: Path, exit_code: int) -> dict:
    return {
        "tool_name": "Bash",
        "cwd": str(root),
        "session_id": "producer-session",
        "tool_input": {"command": "pytest -q tests"},
        "tool_response": {"exit_code": exit_code},
    }


def test_write_verify_and_heal_events_form_reducer_chain(tmp_path: Path) -> None:
    assert _run("trace-write.sh", _write_payload(tmp_path), tmp_path).returncode == 0
    assert _run("trace-verify.sh", _verify_payload(tmp_path, 1), tmp_path).returncode == 0
    assert _run("trace-write.sh", _write_payload(tmp_path, "fixed"), tmp_path).returncode == 0
    assert _run("trace-verify.sh", _verify_payload(tmp_path, 0), tmp_path).returncode == 0

    events = _events(tmp_path)
    assert [event["event_type"] for event in events] == [
        "write.observed", "verify.failed", "heal.attempted", "write.observed", "verify.passed",
    ]
    first_write, failed, heal, second_write, passed = events
    assert first_write["subject_id"] == second_write["subject_id"] == passed["subject_id"]
    assert failed["parent_id"] == first_write["event_id"]
    assert heal["parent_id"] == failed["event_id"]
    assert passed["parent_id"] == heal["event_id"]
    assert passed["evidence_ref"]["required_checks_passed"] is True
    assert passed["evidence_ref"]["independent"] is True
    assert passed["evidence_ref"]["retry_count"] == 1
    assert len(passed["evidence_ref"]["command"]) <= 240


def test_guard_allow_and_deny_preserve_safety_outcome(tmp_path: Path) -> None:
    safe = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": "git status"},
    }
    allowed = _run("git-guard.sh", safe, tmp_path)
    assert allowed.returncode == 0, allowed.stderr

    blocked = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": "rm -rf /"},
    }
    denied = _run("bash-guard.sh", blocked, tmp_path, DEV_KIT_STRICT="1")
    assert denied.returncode == 2
    assert '"deny"' in denied.stderr

    events = _events(tmp_path)
    assert any(event["event_type"] == "guard.allowed" and event["outcome"] == "allowed" for event in events)
    assert any(event["event_type"] == "guard.blocked" and event["outcome"] == "blocked" for event in events)


def test_consumer_project_uses_plugin_root_and_logs_emitter_failure(tmp_path: Path) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    payload = _write_payload(consumer)

    working = _run("trace-write.sh", payload, consumer)
    assert working.returncode == 0
    assert _events(consumer)

    broken = _run(
        "trace-write.sh", payload, consumer,
        CLAUDE_PLUGIN_ROOT=str(tmp_path / "missing-plugin"),
    )
    assert broken.returncode == 0
    error_log = consumer / ".dev-kit" / "trace" / "emitter-errors.log"
    assert error_log.is_file()
    assert error_log.read_text()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_trace_root_env_override_is_used_for_producers(tmp_path: Path) -> None:
    root = tmp_path / "trace-root"
    root.mkdir()
    proc = _run("trace-write.sh", _write_payload(tmp_path), root)
    assert proc.returncode == 0
    assert (root / ".dev-kit" / "trace" / "events.jsonl").is_file()
