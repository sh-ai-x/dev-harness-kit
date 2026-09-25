"""Contract tests for explicit durable Ralph phase promotion."""

from __future__ import annotations

import hashlib
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
    receipt = json.loads(
        (destination / "COMPLETION_RECEIPT.json").read_text(encoding="utf-8")
    )
    assert receipt["certificate_type"] == "ralph.completion-receipt"
    assert receipt["completion_status"] == "verified"
    assert receipt["verifier_kind"] == "declared"
    assert receipt["harness_candidate"] == "working-tree"
    assert receipt["acceptance_checks"]["terminal_state"] is True
    assert len(receipt["artifact_hash"]) == 64
    # The receipt references a manifest that carries the identity fields
    # (harness_candidate, verifier_kind, terminal_state, completion_status)
    # inside the integrity boundary.
    assert receipt["manifest_file"] == "RECEIPT_MANIFEST.json"
    manifest = json.loads(
        (destination / "RECEIPT_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["certificate_type"] == "ralph.receipt-manifest"
    assert manifest["harness_candidate"] == "working-tree"
    assert manifest["verifier_kind"] == "declared"
    assert manifest["terminal_state"] == "USER_MERGE_REQUIRED"
    assert manifest["completion_status"] == "verified"


def test_promote_artifact_hash_covers_receipt_manifest(tmp_path: Path) -> None:
    _write_runtime(tmp_path)

    result = promote.promote(
        tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1"
    )
    destination = result.destination
    artifact_hash = json.loads(
        (destination / "COMPLETION_RECEIPT.json").read_text(encoding="utf-8")
    )["artifact_hash"]

    # Recompute the hash independently over the published files. The
    # manifest is inside the boundary; the receipt itself is excluded.
    files: dict[str, bytes] = {}
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(destination))
        if rel == "COMPLETION_RECEIPT.json":
            continue
        files[rel] = path.read_bytes()
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[rel])
        digest.update(b"\0")
    assert digest.hexdigest() == artifact_hash


def test_promote_verifier_kind_requires_explicit_opt_in(tmp_path: Path) -> None:
    # Default: declared, never inferred from step output.
    default_root = tmp_path / "default"
    default_root.mkdir()
    _write_runtime(default_root)
    result = promote.promote(
        default_root, plan_id="demo-plan", phase="0-mvp", session="run-1"
    )
    receipt = json.loads(
        (result.destination / "COMPLETION_RECEIPT.json").read_text(encoding="utf-8")
    )
    assert receipt["verifier_kind"] == "declared"
    manifest = json.loads(
        (result.destination / "RECEIPT_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["verifier_kind"] == "declared"

    # Explicit opt-in to independent is honored.
    independent_root = tmp_path / "independent"
    independent_root.mkdir()
    _write_runtime(independent_root)
    result = promote.promote(
        independent_root,
        plan_id="demo-plan",
        phase="0-mvp",
        session="run-1",
        verifier_kind="independent",
    )
    receipt = json.loads(
        (result.destination / "COMPLETION_RECEIPT.json").read_text(encoding="utf-8")
    )
    assert receipt["verifier_kind"] == "independent"
    manifest = json.loads(
        (result.destination / "RECEIPT_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["verifier_kind"] == "independent"

    # Bogus value is rejected.
    bogus_root = tmp_path / "bogus"
    bogus_root.mkdir()
    _write_runtime(bogus_root)
    with pytest.raises(promote.RalphPromoteError, match="verifier_kind"):
        promote.promote(
            bogus_root,
            plan_id="demo-plan",
            phase="0-mvp",
            session="run-1",
            verifier_kind="bogus",
        )


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

    with pytest.raises(promote.RalphPromoteError, match="step1-output.json"):
        promote.promote(
            tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1"
        )
    assert not (tmp_path / "docs" / "build-evidence" / "demo-plan").exists()


def test_unsafe_plan_id_is_rejected(tmp_path: Path) -> None:
    _write_runtime(tmp_path)

    with pytest.raises(promote.RalphPromoteError, match="plan_id"):
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


def test_completion_receipt_candidate_is_validated(tmp_path: Path) -> None:
    _write_runtime(tmp_path)
    result = promote.promote(
        tmp_path,
        plan_id="demo-plan",
        phase="0-mvp",
        session="run-1",
        harness_candidate="candidate-v1",
    )
    receipt = json.loads(
        (result.destination / "COMPLETION_RECEIPT.json").read_text(encoding="utf-8")
    )
    assert receipt["harness_candidate"] == "candidate-v1"

    with pytest.raises(promote.RalphPromoteError):
        promote.promote(
            tmp_path,
            plan_id="other-plan",
            phase="0-mvp",
            session="run-1",
            harness_candidate="../escape",
        )


def test_differing_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    _write_runtime(tmp_path)
    promote.promote(tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1")
    summary = tmp_path / "docs" / "build-evidence" / "demo-plan" / "SUMMARY.md"
    summary.write_text("operator-owned record\n", encoding="utf-8")

    with pytest.raises(promote.RalphPromoteError, match="different content"):
        promote.promote(tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1")
    assert summary.read_text(encoding="utf-8") == "operator-owned record\n"


def test_nonterminal_session_fails_closed(tmp_path: Path) -> None:
    _write_runtime(tmp_path)
    state_path = tmp_path / ".dev-kit" / "ralph" / "run-1.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["current_stage"] = "ATTENDED_RUN"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(promote.RalphPromoteError, match="must be terminal"):
        promote.promote(tmp_path, plan_id="demo-plan", phase="0-mvp", session="run-1")
