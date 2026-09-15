"""Contract tests for explicit durable Ralph phase promotion."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import ralph_promote as promote  # noqa: E402


def _write_runtime(root: Path, *, include_output: bool = True) -> None:
    runtime = root / ".dev-kit" / "round-1"
    phase = runtime / "phases" / "0-mvp"
    phase.mkdir(parents=True)
    (runtime / "PRD.md").write_text("# Plan\n\nShip the feature.\n", encoding="utf-8")
    (phase / "step1.md").write_text(
        "# Step 1\n\nImplement the feature.\n", encoding="utf-8"
    )
    (phase / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "0-mvp",
                "steps": [
                    {"step": 1, "name": "implement", "status": "completed"}
                ],
            }
        ),
        encoding="utf-8",
    )
    if include_output:
        (phase / "step1-output.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "0-mvp",
                    "step": 1,
                    "exit_code": 0,
                    "stdout": "pytest: 3 passed",
                    "stderr": "",
                    "duration_seconds": 1.25,
                    "timestamp": "2026-09-13T00:00:00+0900",
                    "checks_run": ["pytest tests/test_feature.py -q"],
                    "independent": True,
                    "changed_files": ["src/feature.py"],
                    "cost_usd": 0.42,
                }
            ),
            encoding="utf-8",
        )
    state_dir = root / ".dev-kit" / "ralph"
    state_dir.mkdir(parents=True)
    (state_dir / "run-1.json").write_text(
        json.dumps(
            {
                "session": "run-1",
                "current_stage": "USER_MERGE_REQUIRED",
                "last_action": "BABYSIT approved PR #123",
                "next_action": "operator runs gh pr merge",
                "blockers": ["human merge required"],
            }
        ),
        encoding="utf-8",
    )


def test_promote_copies_bundle_and_renders_auditable_summary(tmp_path: Path) -> None:
    _write_runtime(tmp_path)

    result = promote.promote(
        tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1"
    )

    destination = tmp_path / "docs" / "build-evidence" / "demo-plan"
    assert result.destination == destination
    assert (destination / "PRD.md").read_text(encoding="utf-8") == (
        "# Plan\n\nShip the feature.\n"
    )
    assert (destination / "phases" / "0-mvp" / "index.json").exists()
    assert (destination / "phases" / "0-mvp" / "step1.md").exists()
    assert (destination / "phases" / "0-mvp" / "step1-output.json").exists()

    summary = (destination / "SUMMARY.md").read_text(encoding="utf-8")
    assert "USER_MERGE_REQUIRED" in summary
    assert "BABYSIT approved PR #123" in summary
    assert "operator runs gh pr merge" in summary
    assert "human merge required" in summary
    assert "1.25" in summary
    assert "| 1 | implement | completed | 0 |" in summary
    assert "0.42" in summary
    assert "pytest tests/test_feature.py -q" in summary
    assert "src/feature.py" in summary


def test_promote_is_idempotent(tmp_path: Path) -> None:
    _write_runtime(tmp_path)

    first = promote.promote(
        tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1"
    )
    destination = first.destination
    before = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    second = promote.promote(
        tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1"
    )
    after = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert second.destination == destination
    assert after == before


def test_dry_run_validates_without_writing(tmp_path: Path) -> None:
    _write_runtime(tmp_path)

    result = promote.promote(
        tmp_path,
        plan_id="demo-plan",
        phase="0-mvp",
        session="run-1",
        dry_run=True,
    )

    assert result.dry_run is True
    assert not (tmp_path / "docs" / "build-evidence").exists()
    assert "SUMMARY.md" in result.planned_paths


def test_missing_step_output_fails_closed_without_partial_bundle(tmp_path: Path) -> None:
    _write_runtime(tmp_path, include_output=False)

    with pytest.raises(promote.PromotionError, match="step1-output.json"):
        promote.promote(
            tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1"
        )
    assert not (tmp_path / "docs" / "build-evidence" / "demo-plan").exists()


def test_unsafe_plan_id_is_rejected(tmp_path: Path) -> None:
    _write_runtime(tmp_path)

    with pytest.raises(promote.PromotionError, match="plan_id"):
        promote.promote(
            tmp_path, plan_id="../escape", phase="0-mvp", session="run-1"
        )


def test_shell_entrypoint_supports_dry_run(tmp_path: Path) -> None:
    _write_runtime(tmp_path)
    command = ROOT / "bin" / "ralph-promote-phases.sh"

    completed = subprocess.run(
        [
            str(command),
            "--project-root",
            str(tmp_path),
            "--plan-id",
            "demo-plan",
            "--phase",
            "0-mvp",
            "--session",
            "run-1",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "would promote" in completed.stdout
    assert not (tmp_path / "docs" / "build-evidence").exists()


def test_differing_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    _write_runtime(tmp_path)
    promote.promote(tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1")
    summary = tmp_path / "docs" / "build-evidence" / "demo-plan" / "SUMMARY.md"
    summary.write_text("operator-owned record\n", encoding="utf-8")

    with pytest.raises(promote.PromotionError, match="different content"):
        promote.promote(tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1")
    assert summary.read_text(encoding="utf-8") == "operator-owned record\n"


def test_nonterminal_session_fails_closed(tmp_path: Path) -> None:
    _write_runtime(tmp_path)
    state_path = tmp_path / ".dev-kit" / "ralph" / "run-1.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["current_stage"] = "ATTENDED_RUN"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(promote.PromotionError, match="must be terminal"):
        promote.promote(tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1")
