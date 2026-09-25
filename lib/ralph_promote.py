"""Promote Ralph's ephemeral phase artifacts into tracked build evidence.

Promotion is an explicit boundary adapter. It validates the complete minimum
bundle before publication and does not modify the Ralph state machine or step
executor lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_ROUND = "1"
DEFAULT_EVIDENCE_DIR = Path("docs") / "build-evidence"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VALID_STATUSES = {"unimplemented", "pending", "in_progress", "completed", "error", "blocked"}
TERMINAL_STAGES = {"DONE", "RECOVERY_REQUIRED", "USER_MERGE_REQUIRED"}


class RalphPromoteError(ValueError):
    """Raised when the source bundle is incomplete, unsafe to publish, or
    contains an unsafe identifier.

    Consolidated from the prior ``PromotionError`` + ``InvalidIdentifierError``
    pair (two-class hierarchy collapsed in refactor/ralph-babysit-collapse;
    the subclass distinction carried no semantic value for callers — every
    raise site already bubbled into a single CLI exit code).
    """


@dataclass(frozen=True)
class PromotionResult:
    """Result of validation and optional publication."""

    destination: Path
    planned_paths: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True)
class _StepEvidence:
    number: int
    name: str
    status: str
    output: dict[str, Any]
    plan_path: Path
    output_path: Path


def _validate_identifier(value: str, label: str) -> str:
    if not value or IDENTIFIER_RE.fullmatch(value) is None:
        raise RalphPromoteError(
            f"{label} must be one safe path segment using letters, digits, '.', '_' or '-'")
    return value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise RalphPromoteError(f"refusing symlink {label}: {path}")


def _reject_symlink_components(project_root: Path, path: Path, label: str) -> None:
    """Reject symlinks in a path below the repository root."""
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:
        raise RalphPromoteError(f"{label} must be inside --project-root: {path}") from exc
    current = project_root
    for component in relative.parts:
        current /= component
        _reject_symlink(current, label)


def _read_regular(path: Path, label: str) -> bytes:
    _reject_symlink(path, label)
    if not path.is_file():
        raise RalphPromoteError(f"required {label} is missing: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RalphPromoteError(f"cannot read {label}: {path}: {exc}") from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    raw = _read_regular(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RalphPromoteError(f"invalid JSON in {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RalphPromoteError(f"{label} must contain a JSON object: {path}")
    return value


def _validate_schema_version(data: dict[str, Any], label: str, path: Path) -> None:
    version = data.get("schema_version")
    if version in (1, "1", "1.0.0"):
        return
    raise RalphPromoteError(f"{label} has unsupported schema_version at {path}")


def _safe_runtime_path(project_root: Path, round_name: str) -> Path:
    round_name = _validate_identifier(round_name, "round")
    candidate = project_root / ".dev-kit" / f"round-{round_name}"
    _reject_symlink_components(project_root, candidate, "runtime directory")
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, project_root):
        raise RalphPromoteError("runtime directory must be inside --project-root")
    if not candidate.is_dir():
        raise RalphPromoteError(f"runtime directory is missing: {candidate}")
    return resolved


def _discover_phase(runtime_dir: Path, phase: str | None) -> str:
    phases_dir = runtime_dir / "phases"
    if not phases_dir.is_dir():
        raise RalphPromoteError(f"runtime phases directory is missing: {phases_dir}")
    if phase is not None:
        return _validate_identifier(phase, "phase")

    candidates = sorted(
        item.name
        for item in phases_dir.iterdir()
        if item.is_dir() and not item.is_symlink() and (item / "index.json").is_file()
    )
    if len(candidates) != 1:
        detail = ", ".join(candidates) if candidates else "none"
        raise RalphPromoteError(
            f"phase is ambiguous; pass --phase explicitly (candidates: {detail})")
    return _validate_identifier(candidates[0], "phase")


def _validate_step_output(
    data: dict[str, Any], phase: str, step: int, path: Path
) -> None:
    _validate_schema_version(data, "step output", path)
    if data.get("phase") != phase or data.get("step") != step:
        raise RalphPromoteError(f"step output has mismatched identity at {path}")
    exit_code = data.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise RalphPromoteError(f"step output has invalid exit_code at {path}")
    duration = data.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise RalphPromoteError(f"step output has invalid duration_seconds at {path}")
    if duration < 0:
        raise RalphPromoteError(f"step output has negative duration_seconds at {path}")
    if not isinstance(data.get("timestamp"), str) or not data["timestamp"].strip():
        raise RalphPromoteError(f"step output has no timestamp at {path}")
    for key in ("stdout", "stderr"):
        if key in data and not isinstance(data[key], str):
            raise RalphPromoteError(f"step output field {key!r} must be text at {path}")


def _load_steps(runtime_dir: Path, phase: str) -> tuple[_StepEvidence, ...]:
    phase_dir = runtime_dir / "phases" / phase
    _reject_symlink(phase_dir, "phase directory")
    if not phase_dir.is_dir():
        raise RalphPromoteError(f"phase directory is missing: {phase_dir}")
    index_path = phase_dir / "index.json"
    index = _read_json(index_path, "phase index")
    _validate_schema_version(index, "phase index", index_path)
    if index.get("phase") not in (None, phase):
        raise RalphPromoteError(f"phase index has mismatched phase at {index_path}")
    raw_steps = index.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RalphPromoteError(f"phase index must contain a non-empty steps list: {index_path}")

    steps: list[_StepEvidence] = []
    seen: set[int] = set()
    for entry in raw_steps:
        if not isinstance(entry, dict):
            raise RalphPromoteError(f"phase index contains a non-object step: {index_path}")
        number = entry.get("step")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise RalphPromoteError(f"phase index contains an invalid step number: {index_path}")
        if number in seen:
            raise RalphPromoteError(f"phase index contains duplicate step {number}: {index_path}")
        seen.add(number)
        status = entry.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise RalphPromoteError(f"phase index contains invalid status for step {number}: {index_path}")
        plan_path = phase_dir / f"step{number}.md"
        output_path = phase_dir / f"step{number}-output.json"
        _read_regular(plan_path, f"step {number} plan")
        output = _read_json(output_path, f"step {number} output")
        _validate_step_output(output, phase, number, output_path)
        steps.append(
            _StepEvidence(
                number=number,
                name=str(entry.get("name", "")),
                status=status,
                output=output,
                plan_path=plan_path,
                output_path=output_path,
            )
        )
    return tuple(sorted(steps, key=lambda item: item.number))


def _load_state(project_root: Path, session: str) -> dict[str, Any]:
    state_path = project_root / ".dev-kit" / "ralph" / f"{session}.json"
    state = _read_json(state_path, "Ralph session state")
    if state.get("session") != session:
        raise RalphPromoteError(f"Ralph session state does not match --session: {state_path}")
    current_stage = state.get("current_stage")
    if not isinstance(current_stage, str) or current_stage not in TERMINAL_STAGES:
        raise RalphPromoteError(
            f"Ralph session must be terminal before promotion (got {current_stage!r})")
    for key in ("last_action", "next_action"):
        if key in state and not isinstance(state[key], str):
            raise RalphPromoteError(f"Ralph session state field {key!r} must be text")
    if "blockers" in state and not isinstance(state["blockers"], list):
        raise RalphPromoteError("Ralph session state field 'blockers' must be a list")
    return state


def _markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "unknown")
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _text_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _stderr_tail(stderr: str) -> str:
    lines = stderr.splitlines()
    tail = "\n".join(lines[-10:])
    return tail[-1200:].strip() if len(tail) > 1200 else tail.strip()


def _numeric_cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _total_cost(state: dict[str, Any], steps: Iterable[_StepEvidence]) -> float | None:
    costs = [_numeric_cost(step.output.get("cost_usd")) for step in steps]
    recorded = [cost for cost in costs if cost is not None]
    if recorded:
        return sum(recorded)
    for key in ("total_cost_usd", "cost_usd", "total_cost"):
        cost = _numeric_cost(state.get(key))
        if cost is not None:
            return cost
    return None


def _render_summary(
    *,
    plan_id: str,
    session: str,
    round_name: str,
    phase: str,
    state: dict[str, Any],
    steps: tuple[_StepEvidence, ...],
) -> str:
    lines = [
        f"# Ralph build evidence: {_markdown_cell(plan_id)}",
        "",
        "## Identity",
        "",
        f"- Plan ID: `{_markdown_cell(plan_id)}`",
        f"- Session: `{_markdown_cell(session)}`",
        f"- Runtime source: `.dev-kit/round-{_markdown_cell(round_name)}`",
        f"- Phase: `{_markdown_cell(phase)}`",
        f"- State: `{_markdown_cell(state['current_stage'])}`",
        f"- Last action: {_markdown_cell(state.get('last_action') or 'not recorded')}",
        f"- Next action: {_markdown_cell(state.get('next_action') or 'not recorded')}",
        "- Blockers:",
    ]
    blockers = _text_lines(state.get("blockers"))
    lines.extend(f"  - {_markdown_cell(item)}" for item in blockers or ["None recorded"])
    lines.extend(
        [
            "",
            "## Step results",
            "",
            "| Step | Name | Status | Exit code | Duration (s) | Output |",
            "|---:|---|---|---:|---:|---|",
        ]
    )
    for step in steps:
        output_rel = f"phases/{phase}/step{step.number}-output.json"
        lines.append(
            f"| {step.number} | {_markdown_cell(step.name)} | "
            f"{_markdown_cell(step.status)} | {step.output['exit_code']} | "
            f"{step.output['duration_seconds']} | `{output_rel}` |"
        )

    exit_matrix = ", ".join(
        f"step{step.number}={step.output['exit_code']}" for step in steps
    )
    failures = [step for step in steps if step.output["exit_code"] != 0]
    total_cost = _total_cost(state, steps)
    lines.extend(
        [
            "",
            "## Checks",
            "",
            f"- Exit-code matrix: `{exit_matrix}`",
            "- Total recorded cost (USD): "
            + (f"`{total_cost:g}`" if total_cost is not None else "not recorded"),
            "- Recorded checks (source metadata; not independently re-run):",
        ]
    )
    recorded_checks = []
    for step in steps:
        for check in _text_lines(step.output.get("checks_run")):
            recorded_checks.append(f"step {step.number}: {check}")
    lines.extend(f"  - {_markdown_cell(check)}" for check in recorded_checks or ["None recorded"])
    lines.append("- Independent verification metadata (source-reported):")
    provenance = []
    for step in steps:
        if "independent" in step.output:
            provenance.append(f"step {step.number}: independent={step.output['independent']}")
    lines.extend(f"  - {_markdown_cell(item)}" for item in provenance or ["None recorded"])
    lines.append("- Failed-step stderr tails:")
    for step in failures:
        stderr = _stderr_tail(str(step.output.get("stderr", ""))) or "(empty)"
        lines.append(f"  - step {step.number}: `{_markdown_cell(stderr)}`")
    if not failures:
        lines.append("  - None")

    changed_files: list[str] = []
    for step in steps:
        for item in _text_lines(step.output.get("changed_files")):
            if item not in changed_files:
                changed_files.append(item)
    lines.extend(["", "## Changed files", ""])
    lines.extend(f"- `{_markdown_cell(item)}`" for item in changed_files or ["Not recorded"])
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            "- This bundle was structurally validated before publication.",
            "- Agent stdout/stderr and exit codes are preserved in each step output JSON.",
            "- Independent checks are listed only when the source artifact recorded them; this command does not invent verification.",
        ]
    )
    return "\n".join(lines) + "\n"


def _expected_files(
    prd_path: Path,
    index_path: Path,
    steps: tuple[_StepEvidence, ...],
    phase: str,
    summary: str,
) -> dict[str, bytes]:
    expected = {
        "PRD.md": _read_regular(prd_path, "PRD.md"),
        f"phases/{phase}/index.json": _read_regular(index_path, "phase index"),
        "SUMMARY.md": summary.encode("utf-8"),
    }
    for step in steps:
        expected[f"phases/{phase}/step{step.number}.md"] = _read_regular(
            step.plan_path, "step plan"
        )
        expected[f"phases/{phase}/step{step.number}-output.json"] = _read_regular(
            step.output_path, "step output"
        )
    return expected


RECEIPT_MANIFEST_FILENAME = "RECEIPT_MANIFEST.json"
RECEIPT_MANIFEST_SCHEMA_VERSION = 1


def _artifact_hash(files: dict[str, bytes]) -> str:
    """Hash the deterministic evidence inputs (including the receipt manifest)."""
    digest = hashlib.sha256()
    for relative, data in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _completion_receipt(
    *,
    plan_id: str,
    session: str,
    phase: str,
    state: dict[str, Any],
    steps: tuple[_StepEvidence, ...],
    files: dict[str, bytes],
    harness_candidate: str,
    verifier_kind: str,
) -> tuple[bytes, bytes, str]:
    """Build a bounded, honest completion receipt + manifest.

    The integrity-bearing identity fields (``harness_candidate``,
    ``verifier_kind``, ``terminal_state``, ``completion_status``) live in
    ``RECEIPT_MANIFEST.json`` so they fall inside ``artifact_hash``.  The
    receipt itself references the manifest by filename so downstream
    consumers can verify the identity even though the receipt is
    technically outside the hash (chicken-and-egg: the receipt must
    contain ``artifact_hash`` to be self-describing, but the hash covers
    the receipt only if the manifest is treated as the integrity
    boundary).

    Returns ``(receipt_bytes, manifest_bytes, artifact_hash)``.
    """
    if verifier_kind not in {"declared", "independent"}:
        raise RalphPromoteError(
            f"verifier_kind must be 'declared' or 'independent'; got {verifier_kind!r}"
        )
    all_exit_zero = all(step.output["exit_code"] == 0 for step in steps)
    all_completed = all(step.status == "completed" for step in steps)
    blockers = _text_lines(state.get("blockers"))
    created_at = str(state.get("started_at") or "")
    if not created_at and steps:
        created_at = str(steps[0].output.get("timestamp") or "")
    created_at = created_at or "not-recorded"
    completion_status = "verified" if all_exit_zero and all_completed else "unverified"
    manifest_fields: dict[str, Any] = {
        "schema_version": RECEIPT_MANIFEST_SCHEMA_VERSION,
        "certificate_type": "ralph.receipt-manifest",
        "created_at": created_at,
        "plan_id": plan_id,
        "session": session,
        "phase": phase,
        "terminal_state": state["current_stage"],
        "harness_candidate": harness_candidate,
        "verifier_kind": verifier_kind,
        "completion_status": completion_status,
    }
    manifest_bytes = (
        json.dumps(manifest_fields, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    # Build the hash over evidence + manifest so the manifest identity is
    # inside the integrity boundary; the receipt itself is excluded.
    hashed_files = dict(files)
    hashed_files[RECEIPT_MANIFEST_FILENAME] = manifest_bytes
    artifact_hash = _artifact_hash(hashed_files)
    receipt = {
        "schema_version": 1,
        "certificate_type": "ralph.completion-receipt",
        "created_at": created_at,
        "plan_id": plan_id,
        "session": session,
        "phase": phase,
        "terminal_state": state["current_stage"],
        "harness_candidate": harness_candidate,
        "artifact_hash": artifact_hash,
        "manifest_file": RECEIPT_MANIFEST_FILENAME,
        "evidence_refs": sorted(files),
        "verifier_kind": verifier_kind,
        "completion_status": completion_status,
        "unresolved_risk": bool(blockers) or not all_completed,
        "acceptance_checks": {
            "terminal_state": state["current_stage"] in TERMINAL_STAGES,
            "all_steps_exit_zero": all_exit_zero,
            "all_steps_completed": all_completed,
            "blockers_empty": not blockers,
        },
        "blockers": blockers,
    }
    receipt_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return receipt_bytes, manifest_bytes, artifact_hash


def _existing_files(destination: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise RalphPromoteError(f"refusing symlink in evidence destination: {path}")
        if path.is_file():
            files[str(path.relative_to(destination))] = path.read_bytes()
    return files


def _publish(destination: Path, expected: dict[str, bytes]) -> None:
    destination_root = destination.parent
    destination_root.mkdir(parents=True, exist_ok=True)
    _reject_symlink(destination_root, "evidence parent")
    if destination.exists() and not destination.is_dir():
        raise RalphPromoteError(f"evidence destination is not a directory: {destination}")
    _reject_symlink(destination, "evidence destination")

    if destination.exists():
        if _existing_files(destination) != expected:
            raise RalphPromoteError(
                f"evidence destination already exists with different content: {destination}")
        return

    stage: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=str(destination_root))
    )
    try:
        for relative, data in expected.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        os.replace(stage, destination)
        stage = None
    except OSError as exc:
        raise RalphPromoteError(f"could not publish evidence bundle: {exc}") from exc
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def promote(
    project_root: Path,
    *,
    plan_id: str,
    phase: str | None = None,
    session: str = "default",
    round_name: str = DEFAULT_ROUND,
    harness_candidate: str = "working-tree",
    verifier_kind: str = "declared",
    dry_run: bool = False,
) -> PromotionResult:
    """Validate and optionally publish one completed runtime phase bundle.

    ``verifier_kind`` defaults to ``"declared"``: this module never
    re-runs verification, so flipping to ``"independent"`` requires an
    explicit operator opt-in (and, in production, an independent
    re-verification pass that this module cannot perform).
    """
    project_root = project_root.resolve()
    plan_id = _validate_identifier(plan_id, "plan_id")
    session = _validate_identifier(session, "session")
    round_name = _validate_identifier(round_name, "round")
    harness_candidate = _validate_identifier(harness_candidate, "harness_candidate")
    if verifier_kind not in {"declared", "independent"}:
        raise RalphPromoteError(
            f"verifier_kind must be 'declared' or 'independent'; got {verifier_kind!r}"
        )
    runtime = _safe_runtime_path(project_root, round_name)
    resolved_phase = _discover_phase(runtime, phase)
    prd_path = runtime / "PRD.md"
    _read_regular(prd_path, "PRD.md")
    index_path = runtime / "phases" / resolved_phase / "index.json"
    _read_regular(index_path, "phase index")
    steps = _load_steps(runtime, resolved_phase)
    state = _load_state(project_root, session)
    summary = _render_summary(
        plan_id=plan_id,
        session=session,
        round_name=round_name,
        phase=resolved_phase,
        state=state,
        steps=steps,
    )
    base_expected = _expected_files(prd_path, index_path, steps, resolved_phase, summary)
    expected = dict(base_expected)
    receipt_bytes, manifest_bytes, _artifact_hash_value = _completion_receipt(
        plan_id=plan_id,
        session=session,
        phase=resolved_phase,
        state=state,
        steps=steps,
        files=base_expected,
        harness_candidate=harness_candidate,
        verifier_kind=verifier_kind,
    )
    expected["RECEIPT_MANIFEST.json"] = manifest_bytes
    expected["COMPLETION_RECEIPT.json"] = receipt_bytes
    destination = project_root / DEFAULT_EVIDENCE_DIR / plan_id
    planned_paths = tuple(expected)
    if not dry_run:
        _publish(destination, expected)
    return PromotionResult(destination, planned_paths, dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Promote validated Ralph phase artifacts into tracked build evidence")
    parser.add_argument("--project-root", default=".", help="repository root (default: current directory)")
    parser.add_argument("--round", dest="round_name", default=DEFAULT_ROUND, help="Ralph runtime round (default: 1)")
    parser.add_argument("--plan-id", required=True, help="safe destination identity under docs/build-evidence/")
    parser.add_argument("--phase", help="phase name; inferred only when exactly one phase exists")
    parser.add_argument("--session", default="default", help="Ralph session state name (default: default)")
    parser.add_argument(
        "--harness-candidate",
        default="working-tree",
        help="candidate id recorded in COMPLETION_RECEIPT.json",
    )
    parser.add_argument(
        "--verifier-kind",
        default="declared",
        choices=["declared", "independent"],
        help=(
            "integrity provenance recorded in the receipt manifest "
            "(default: declared — this module never re-runs verification, "
            "so 'independent' requires an explicit operator opt-in)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="validate and list files without writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = promote(
            Path(args.project_root),
            plan_id=args.plan_id,
            phase=args.phase,
            session=args.session,
            round_name=args.round_name,
            harness_candidate=args.harness_candidate,
            verifier_kind=args.verifier_kind,
            dry_run=args.dry_run,
        )
    except RalphPromoteError as exc:
        # Restore the prior exit-code contract: identifier-validation
        # failures (`_validate_identifier`) return 1; bundle-validation
        # and other promotion failures return 2. After the exception
        # hierarchy collapsed (refactor/ralph-babysit-collapse) both
        # raised RalphPromoteError, so we discriminate by message
        # prefix — uniquely emitted by `_validate_identifier` at the
        # path `_validate_identifier(value, label) → raise
        # RalphPromoteError(f"{label} must be one safe path segment
        # using letters, digits, '.', '_' or '-'")`.
        msg = str(exc)
        if msg.endswith("must be one safe path segment using letters, digits, '.', '_' or '-'"):
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: filesystem failure: {exc}", file=sys.stderr)
        return 3
    action = "would promote" if result.dry_run else "promoted"
    print(f"{action} {len(result.planned_paths)} files to {result.destination}")
    for path in result.planned_paths:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
