"""Black-box coverage for the Ralph operator CLI."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVE = REPO_ROOT / "skills" / "ralph" / "scripts" / "ralph_drive.sh"


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"PROJECT_ROOT": str(root), "RALPH_SESSION": "cli"})
    return subprocess.run(
        ["bash", str(DRIVE), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _approve_to_attended(root: Path) -> None:
    assert _run(root, "init", "exercise cli", "--deadline", "2099-01-01T00:00:00Z").returncode == 0
    for stage in ("PROPOSAL_GATE", "PLAN_GATE", "SHIP_CONFIRM_GATE", "ATTENDED_RUN"):
        result = _run(root, "advance", stage, "--action", f"approve {stage}")
        assert result.returncode == 0, result.stderr


def test_cli_drives_noop_and_reports_session_scoped_evidence(tmp_path: Path) -> None:
    _approve_to_attended(tmp_path)

    result = _run(tmp_path, "run-attended", "--dispatch", "noop")
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["current_stage"] == "USER_MERGE_REQUIRED"
    assert state["completed_sub_stages"] == ["BUILD", "BABYSIT", "SHIP"]

    events = _run(tmp_path, "events")
    assert events.returncode == 0, events.stderr
    records = json.loads(events.stdout)
    assert records
    assert {record["run_id"] for record in records} == {state["run_id"]}

    metrics = _run(tmp_path, "metrics", "--format", "json")
    assert metrics.returncode == 0, metrics.stderr
    report = json.loads(metrics.stdout)
    assert report["status"] == "OK"
    assert report["metrics"]["run_convergence_rate"]["value"] == 100.0
    assert report["metrics"]["recovery_success_rate"]["status"] == "NOT_APPLICABLE"
    assert report["context_metrics"]["usage_coverage"]["status"] == "INSUFFICIENT_EVIDENCE"

    text = _run(tmp_path, "metrics", "--format", "text")
    assert text.returncode == 0, text.stderr
    assert "status: OK" in text.stdout

    status = _run(tmp_path, "status-report")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["state"]["run_id"] == state["run_id"]
