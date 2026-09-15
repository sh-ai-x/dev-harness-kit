"""Promote Ralph's ephemeral phase artifacts into tracked build evidence.

Promotion is an explicit boundary adapter. It validates the complete minimum
bundle before publication and does not modify the Ralph state machine or step
executor lifecycle.
"""

from __future__ import annotations

import argparse
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


class PromotionError(ValueError):
    """Raised when the source bundle is incomplete or unsafe to publish."""


class InvalidIdentifierError(PromotionError):
    """Raised for an unsafe round, plan, phase, or session identifier."""


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
        raise InvalidIdentifierError(
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
        raise PromotionError(f"refusing symlink {label}: {path}")


def _reject_symlink_components(project_root: Path, path: Path, label: str) -> None:
    """Reject symlinks in a path below the repository root."""
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:
        raise PromotionError(f"{label} must be inside --project-root: {path}") from exc
    current = project_root
    for component in relative.parts:
        current /= component
        _reject_symlink(current, label)


def _read_regular(path: Path, label: str) -> bytes:
    _reject_symlink(path, label)
    if not path.is_file():
        raise PromotionError(f"required {label} is missing: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PromotionError(f"cannot read {label}: {path}: {exc}") from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    raw = _read_regular(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"invalid JSON in {label}: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"{label} must contain a JSON object: {path}")
    return value


def _validate_schema_version(data: dict[str, Any], label: str, path: Path) -> None:
    version = data.get("schema_version")
    if version in (1, "1", "1.0.0"):
        return
    raise PromotionError(f"{label} has unsupported schema_version at {path}")


def _safe_runtime_path(project_root: Path, round_name: str) -> Path:
    round_name = _validate_identifier(round_name, "round")
    candidate = project_root / ".dev-kit" / f"round-{round_name}"
    _reject_symlink_components(project_root, candidate, "runtime directory")
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, project_root):
        raise PromotionError("runtime directory must be inside --project-root")
    if not candidate.is_dir():
        raise PromotionError(f"runtime directory is missing: {candidate}")
    return resolved


def _discover_phase(runtime_dir: Path, phase: str | None) -> str:
    phases_dir = runtime_dir / "phases"
    if not phases_dir.is_dir():
        raise PromotionError(f"runtime phases directory is missing: {phases_dir}")
    if phase is not None:
        return _validate_identifier(phase, "phase")

    candidates = sorted(
        item.name
        for item in phases_dir.iterdir()
        if item.is_dir() and not item.is_symlink() and (item / "index.json").is_file()
    )
    if len(candidates) != 1:
        detail = ", ".join(candidates) if candidates else "none"
        raise PromotionError(
            f"phase is ambiguous; pass --phase explicitly (candidates: {detail})")
    return _validate_identifier(candidates[0], "phase")


def _validate_step_output(
    data: dict[str, Any], phase: str, step: int, path: Path
) -> None:
    _validate_schema_version(data, "step output", path)
    if data.get("phase") != phase or data.get("step") != step:
        raise PromotionError(f"step output has mismatched identity at {path}")
    exit_code = data.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise PromotionError(f"step output has invalid exit_code at {path}")
    duration = data.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise PromotionError(f"step output has invalid duration_seconds at {path}")
    if duration < 0:
        raise PromotionError(f"step output has negative duration_seconds at {path}")
    if not isinstance(data.get("timestamp"), str) or not data["timestamp"].strip():
        raise PromotionError(f"step output has no timestamp at {path}")
    for key in ("stdout", "stderr"):
        if key in data and not isinstance(data[key], str):
            raise PromotionError(f"step output field {key!r} must be text at {path}")


def _load_steps(runtime_dir: Path, phase: str) -> tuple[_StepEvidence, ...]:
    phase_dir = runtime_dir / "phases" / phase
    _reject_symlink(phase_dir, "phase directory")
    if not phase_dir.is_dir():
        raise PromotionError(f"phase directory is missing: {phase_dir}")
    index_path = phase_dir / "index.json"
    index = _read_json(index_path, "phase index")
    _validate_schema_version(index, "phase index", index_path)
    if index.get("phase") not in (None, phase):
        raise PromotionError(f"phase index has mismatched phase at {index_path}")
    raw_steps = index.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PromotionError(f"phase index must contain a non-empty steps list: {index_path}")

    steps: list[_StepEvidence] = []
    seen: set[int] = set()
    for entry in raw_steps:
        if not isinstance(entry, dict):
            raise PromotionError(f"phase index contains a non-object step: {index_path}")
        number = entry.get("step")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise PromotionError(f"phase index contains an invalid step number: {index_path}")
        if number in seen:
            raise PromotionError(f"phase index contains duplicate step {number}: {index_path}")
        seen.add(number)
        status = entry.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise PromotionError(f"phase index contains invalid status for step {number}: {index_path}")
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
        raise PromotionError(f"Ralph session state does not match --session: {state_path}")
    current_stage = state.get("current_stage")
    if not isinstance(current_stage, str) or current_stage not in TERMINAL_STAGES:
        raise PromotionError(
            f"Ralph session must be terminal before promotion (got {current_stage!r})")
    for key in ("last_action", "next_action"):
        if key in state and not isinstance(state[key], str):
            raise PromotionError(f"Ralph session state field {key!r} must be text")
    if "blockers" in state and not isinstance(state["blockers"], list):
        raise PromotionError("Ralph session state field 'blockers' must be a list")
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


def _existing_files(destination: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise PromotionError(f"refusing symlink in evidence destination: {path}")
        if path.is_file():
            files[str(path.relative_to(destination))] = path.read_bytes()
    return files


def _publish(destination: Path, expected: dict[str, bytes]) -> None:
    destination_root = destination.parent
    destination_root.mkdir(parents=True, exist_ok=True)
    _reject_symlink(destination_root, "evidence parent")
    if destination.exists() and not destination.is_dir():
        raise PromotionError(f"evidence destination is not a directory: {destination}")
    _reject_symlink(destination, "evidence destination")

    if destination.exists():
        if _existing_files(destination) != expected:
            raise PromotionError(
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
        raise PromotionError(f"could not publish evidence bundle: {exc}") from exc
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
    dry_run: bool = False,
) -> PromotionResult:
    """Validate and optionally publish one completed runtime phase bundle."""
    project_root = project_root.resolve()
    plan_id = _validate_identifier(plan_id, "plan_id")
    session = _validate_identifier(session, "session")
    round_name = _validate_identifier(round_name, "round")
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
    expected = _expected_files(prd_path, index_path, steps, resolved_phase, summary)
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
            dry_run=args.dry_run,
        )
    except InvalidIdentifierError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PromotionError as exc:
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
