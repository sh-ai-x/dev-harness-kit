"""Retention marker for worktrees owned by the long-running babysit-pr loop.

Normal worktree cleanup remains unchanged. A babysit-pr run writes this marker
inside its owning worktree so cleanup tooling can distinguish an ordinary
completed task from a telemetry-bearing long-running PR context.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

RETENTION_FILE = ".dev-kit/babysit-retention.json"
ACTIVE = "active"
TERMINAL = "terminal"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def retention_path(worktree: str | os.PathLike[str]) -> Path:
    return Path(worktree) / RETENTION_FILE


def write_retention(
    worktree: str | os.PathLike[str],
    *,
    parent_pr: int,
    current_pr: int,
    branch: str,
    phase: str = ACTIVE,
    log_root: str = "",
    now: str | None = None,
) -> Path:
    """Atomically mark a babysit-pr worktree as retained forever."""
    if phase not in {ACTIVE, TERMINAL}:
        raise ValueError(f"unknown retention phase: {phase}")
    if parent_pr <= 0 or current_pr <= 0:
        raise ValueError("PR numbers must be positive")
    if not branch:
        raise ValueError("branch is required")
    target = retention_path(worktree)
    target.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "schema_version": "1.0.0",
        "owner": "babysit-pr",
        "parent_pr": parent_pr,
        "current_pr": current_pr,
        "branch": branch,
        "phase": phase,
        "retain_worktree": True,
        "retain_logs": True,
        "retention": "indefinite_until_explicit_operator_removal",
        "log_root": log_root,
        "updated_at": now or _now(),
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load_retention(worktree: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        raw = json.loads(retention_path(worktree).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def is_retained(worktree: str | os.PathLike[str]) -> bool:
    record = load_retention(worktree)
    return bool(record and record.get("owner") == "babysit-pr" and record.get("retain_worktree"))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Persist babysit-pr worktree retention state")
    parser.add_argument("worktree")
    parser.add_argument("--parent-pr", type=int, required=True)
    parser.add_argument("--current-pr", type=int, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--phase", choices=[ACTIVE, TERMINAL], default=ACTIVE)
    parser.add_argument("--log-root", default="")
    args = parser.parse_args()
    print(write_retention(**vars(args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
