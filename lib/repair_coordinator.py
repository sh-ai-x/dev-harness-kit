#!/usr/bin/env python3
"""Deterministic state and audit primitives shared by PR repair workers.

The model may diagnose and patch; this module owns the durable boundaries:
attempt limits, failure identity, progress detection, duplicate suppression,
and append-only repair events.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from lib.trace_log import append_event as append_trace_event

SCHEMA_VERSION = "1.0.0"
MAX_REPAIR_ATTEMPTS = 2
REPAIR_STATE_DIR = ".dev-kit/repair"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def failure_signature(
    *,
    category: str,
    checks: Iterable[str] = (),
    findings: Iterable[str] = (),
    evidence: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable identity for the current repair blocker."""
    payload = {
        "category": category,
        "checks": sorted(str(item) for item in checks),
        "findings": sorted(str(item) for item in findings),
        "evidence": dict(evidence or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def has_progress(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Return true when the latest observation materially improves state."""
    if previous.get("failure_signature") != current.get("failure_signature"):
        return True
    previous_findings = set(previous.get("finding_ids") or [])
    current_findings = set(current.get("finding_ids") or [])
    if len(current_findings) < len(previous_findings):
        return True
    previous_successes = int(previous.get("successful_checks", 0))
    current_successes = int(current.get("successful_checks", 0))
    return current_successes > previous_successes


@dataclass(frozen=True)
class RepairState:
    parent_pr: int
    current_pr: int
    attempt: int
    failure_signature: str
    run_id: str
    commit_sha: str
    status: str = "observing"
    updated_at: str = ""

    def validate(self) -> None:
        if self.parent_pr <= 0 or self.current_pr <= 0:
            raise ValueError("parent_pr and current_pr must be positive")
        if not 0 <= self.attempt <= MAX_REPAIR_ATTEMPTS:
            raise ValueError(f"attempt must be between 0 and {MAX_REPAIR_ATTEMPTS}")
        if not self.failure_signature:
            raise ValueError("failure_signature is required")
        if not self.run_id or not self.commit_sha:
            raise ValueError("run_id and commit_sha are required")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


def next_state(
    state: RepairState,
    *,
    current_observation: Mapping[str, Any],
    new_pr: Optional[int] = None,
    new_commit_sha: Optional[str] = None,
) -> RepairState:
    """Advance after verification, creating at most two repair attempts."""
    if has_progress({"failure_signature": state.failure_signature}, current_observation):
        result = RepairState(
            parent_pr=state.parent_pr,
            current_pr=state.current_pr,
            attempt=state.attempt,
            failure_signature=str(current_observation.get("failure_signature", state.failure_signature)),
            run_id=state.run_id,
            commit_sha=new_commit_sha or state.commit_sha,
            status="rechecking",
            updated_at=_now(),
        )
        result.validate()
        return result

    if state.attempt < MAX_REPAIR_ATTEMPTS:
        if not new_pr or not new_commit_sha:
            raise ValueError("a repair PR and commit SHA are required for a new attempt")
        result = RepairState(
            parent_pr=state.parent_pr,
            current_pr=new_pr,
            attempt=state.attempt + 1,
            failure_signature=state.failure_signature,
            run_id=state.run_id,
            commit_sha=new_commit_sha,
            status="repair_pr_required",
            updated_at=_now(),
        )
        result.validate()
        return result

    result = RepairState(
        parent_pr=state.parent_pr,
        current_pr=state.current_pr,
        attempt=state.attempt,
        failure_signature=state.failure_signature,
        run_id=state.run_id,
        commit_sha=state.commit_sha,
        status="human_exception",
        updated_at=_now(),
    )
    result.validate()
    return result


def repair_key(state: RepairState) -> str:
    """Return the duplicate-suppression key for a repair attempt."""
    state.validate()
    return f"{state.parent_pr}:{state.attempt}:{state.failure_signature}"


def append_event(root: Path, event: str, state: RepairState, **details: Any) -> Path:
    """Append one compact, queryable event without blocking the repair loop."""
    state.validate()
    path = root / REPAIR_STATE_DIR / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": state.run_id,
        "parent_pr": state.parent_pr,
        "current_pr": state.current_pr,
        "attempt": state.attempt,
        "failure_signature": state.failure_signature,
        "commit_sha": state.commit_sha,
        "event": event,
        "status": state.status,
        "timestamp": _now(),
        **details,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    # Keep the existing repair audit log as the source of truth and project
    # the same boundary into the shared effectiveness stream. This is
    # additive and best-effort: a telemetry failure must not stop repair.
    event_type = {
        "verification_failed": "verify.failed",
        "checks_failed": "verify.failed",
        "repair_attempted": "heal.attempted",
        "repair_pr_created": "heal.attempted",
        "verification_passed": "verify.passed",
        "checks_passed": "verify.passed",
        "recovered": "verify.passed",
    }.get(event, "repair.observed")
    timestamp = record["timestamp"]
    try:
        append_trace_event(root, {
            "schema_version": 1,
            "event_id": hashlib.sha256(
                f"{state.run_id}:{state.failure_signature}:{event}:{timestamp}".encode()
            ).hexdigest()[:16],
            "run_id": state.run_id,
            "workflow_id": f"repair:{state.parent_pr}",
            "stage": "repair",
            "event_type": event_type,
            "subject_id": details.get("subject_id", state.failure_signature),
            "parent_id": details.get("parent_id"),
            "ts": timestamp,
            "outcome": event,
            "source": "lib.repair_coordinator",
            "evidence_ref": {
                "attempt": state.attempt,
                "failure_signature": state.failure_signature,
                "commit_sha": state.commit_sha,
                **details,
            },
        })
    except (OSError, ValueError):
        pass
    return path


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Repair coordinator state primitives")
    parser.add_argument("--event", required=True)
    parser.add_argument("--parent-pr", type=int, required=True)
    parser.add_argument("--current-pr", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--failure-signature", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    state = RepairState(
        parent_pr=args.parent_pr,
        current_pr=args.current_pr,
        attempt=args.attempt,
        failure_signature=args.failure_signature,
        run_id=args.run_id,
        commit_sha=args.commit_sha,
        status=args.event,
        updated_at=_now(),
    )
    print(append_event(args.root, args.event, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
