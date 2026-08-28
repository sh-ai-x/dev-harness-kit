"""trace_log.py — append-only structured trajectory log per worktree run.

Stores per-step execution data + per-dim scores + verdict + per-dim
evidence. JSON file under `eval/transcripts/<case_id>/<UTC>.json`.

Phase 0 (issue #511): deterministic 4-dim scorers only; LLM judges come
in Phase 1. `judge_scores` is still written so the schema does not
need a v2 bump when LLM judge fields appear.

Public API:
    TraceStep     — one step in the trajectory (frozen dataclass)
    TraceLog      — full trajectory + scores + evidence (frozen dataclass)
    TraceLog.save(worktree)  — write JSON file under worktree
    TraceLog.load(path)      — read JSON file back

Backward compat: schema_version is a positive integer. New fields are
additive; never repurpose existing field names.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
EVENT_REQUIRED_FIELDS = (
    "event_id", "run_id", "workflow_id", "stage", "event_type",
    "subject_id", "parent_id", "ts", "outcome", "source", "evidence_ref",
)
EVENT_RECORD_REQUIRED_FIELDS = EVENT_REQUIRED_FIELDS + ("schema_version",)


def _default_identity() -> Dict[str, str]:
    """Auto-stamp producer identity for stability submetric (issue #663 SSOT).

    Reads from env so the env-based default wins; if env is unset, falls
    back to a best-effort platform-derived value. Empty string for fields
    that have no signal — the reducer treats empty/missing as 'no identity
    recorded' so the user can opt-in by setting DEV_KIT_* env vars.
    """
    agent = os.environ.get("DEV_KIT_AGENT", "claude-code")
    provider = os.environ.get("DEV_KIT_PROVIDER", "")
    model = os.environ.get("DEV_KIT_MODEL", "")
    return {"agent": agent, "provider": provider, "model": model}


@dataclass(frozen=True)
class TraceStep:
    """One step in the agent trajectory.

    All times are ISO-8601 UTC. `extra` carries step-specific metadata
    (file path, command, etc.) that does not fit the fixed fields.
    """

    ts: str
    skill: str
    phase: str
    model: Optional[str] = None
    prompt_hash: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    retries: int = 0
    exit_code: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceLog:
    """Full trajectory + scores + evidence.

    `steps` is the per-step execution log; `judge_scores` is the per-dim
    scoring output (one entry per rubric invocation); `evidence` is the
    per-dim debug data the human reviewer would need.
    """

    schema_version: int = SCHEMA_VERSION
    case_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    harness_version: str = ""
    agent: str = ""
    worktree_branch: str = ""
    worktree_path: str = ""
    steps: List[TraceStep] = field(default_factory=list)
    judge_scores: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "harness_version": self.harness_version,
            "agent": self.agent,
            "worktree_branch": self.worktree_branch,
            "worktree_path": self.worktree_path,
            "steps": [asdict(s) for s in self.steps],
            "judge_scores": list(self.judge_scores),
            "evidence": dict(self.evidence),
        }

    @classmethod
    def load(cls, path: Path) -> "TraceLog":
        """Read a JSON file back into a TraceLog.

        Unknown step fields go into `extra`; missing fields get defaults.
        Raises ValueError on schema_version mismatch (the caller can
        choose to migrate or skip).
        """
        raw = json.loads(Path(path).read_text())
        version = raw.get("schema_version", 0)
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"trace schema_version={version} != current={SCHEMA_VERSION} "
                f"(file={path}); migrate first"
            )
        steps_raw = raw.pop("steps", [])
        steps: List[TraceStep] = []
        for s in steps_raw:
            # Take `extra` (may be absent or empty) and any future
            # fields not in TraceStep's annotations; merge into a single
            # `extra` dict. Then construct with only the known fields.
            step_extra: Dict[str, Any] = dict(s.get("extra") or {})
            for k, v in s.items():
                if k == "extra":
                    continue  # already merged
                if k not in TraceStep.__annotations__:
                    step_extra[k] = v
            known = {k: v for k, v in s.items()
                     if k in TraceStep.__annotations__ and k != "extra"}
            steps.append(TraceStep(extra=step_extra, **known))
        return cls(steps=steps, **raw)

    def save(self, worktree: Path) -> Path:
        """Write the trace JSON to `worktree/eval/transcripts/<case_id>/<UTC>.json`.

        Hardens against the LLM review findings (trace path escape /
        symlink-directed writes):
        - case_id must be a single safe relative component (no `..`,
          no `/`, no leading `-`, no absolute path).
        - the parent transcript directory must resolve strictly under
          `worktree/eval/transcripts/` (containment check).
        - existing symlinks at the destination are refused; the parent
          directory is rejected if it is itself a symlink.
        - collision-safe filename: timestamp + microsecond + uuid4 suffix
          prevents same-second overwrites (the previous version had
          only second resolution).

        Returns the written path. Raises ValueError on path validation
        failure.
        """
        import uuid

        case_id = self.case_id
        if not case_id or case_id.startswith(".") or case_id.startswith("-"):
            raise ValueError(f"case_id must be a safe component: {case_id!r}")
        if "/" in case_id or "\\" in case_id or ".." in case_id:
            raise ValueError(f"case_id must not contain path separators or '..': {case_id!r}")
        if Path(case_id).is_absolute():
            raise ValueError(f"case_id must be relative: {case_id!r}")

        worktree = Path(worktree).resolve()
        transcripts_root = (worktree / "eval" / "transcripts").resolve()
        out_dir = (transcripts_root / case_id).resolve()
        try:
            out_dir.relative_to(transcripts_root)
        except ValueError as exc:
            raise ValueError(
                f"case_id resolves outside transcripts root: {out_dir}"
            ) from exc
        # Refuse symlinked parents (worktree-controlled escape vector).
        if out_dir.exists() and out_dir.is_symlink():
            raise ValueError(f"refusing to write through symlink: {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        out = out_dir / f"{ts}-{suffix}.json"
        out.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False))
        return out


def now_utc() -> str:
    """ISO-8601 UTC timestamp string (e.g. `2026-07-31T10:00:00Z`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_event_id() -> str:
    """Return one canonical collision-resistant opaque event identifier."""
    return uuid.uuid4().hex


def _event_path(root: Path) -> Path:
    return Path(root) / ".dev-kit" / "trace" / "events.jsonl"


def validate_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize one workflow evidence event."""
    missing = [key for key in EVENT_REQUIRED_FIELDS if key not in event]
    if missing:
        raise ValueError(f"event missing required fields: {', '.join(missing)}")
    normalized = dict(event)
    normalized.setdefault("schema_version", EVENT_SCHEMA_VERSION)
    if normalized["schema_version"] != EVENT_SCHEMA_VERSION:
        raise ValueError("unsupported event schema_version")
    for key in ("event_id", "run_id", "workflow_id", "stage", "event_type", "subject_id", "ts", "outcome", "source"):
        if not isinstance(normalized[key], str) or not normalized[key]:
            raise ValueError(f"event field {key!r} must be a non-empty string")
    if normalized["parent_id"] is not None and not isinstance(normalized["parent_id"], str):
        raise ValueError("event parent_id must be a string or null")
    if not isinstance(normalized["evidence_ref"], dict):
        raise ValueError("event evidence_ref must be an object")
    return normalized


# Dedupe scan window: how many trailing lines the append-time guard
# inspects for a colliding event_id, and the byte budget used to reach
# them. Reading only the tail keeps the hot path independent of total
# log length (a full readlines() is O(N) per append, i.e. O(N^2) over a
# trajectory). _DEDUPE_SCAN_BYTES is a generous per-line allowance; if
# the tail happens to hold more than _DEDUPE_SCAN_LINES lines within
# that budget we simply keep the last _DEDUPE_SCAN_LINES of them.
_DEDUPE_SCAN_LINES = 64
_DEDUPE_SCAN_BYTES = _DEDUPE_SCAN_LINES * 4096


def _recent_event_ids(fd: int) -> set:
    """Return event_ids from the last ``_DEDUPE_SCAN_LINES`` log lines.

    Reads only the tail of the file via ``os.pread`` so the caller's
    text-stream position (positioned for append) is never disturbed and
    the cost does not grow with log length. Malformed or truncated lines
    are skipped rather than raising — the dedupe guard is an
    optimisation, and a corrupt tail must not block a valid append.
    """
    try:
        size = os.fstat(fd).st_size
    except OSError:
        return set()
    if size <= 0:
        return set()
    offset = max(0, size - _DEDUPE_SCAN_BYTES)
    try:
        blob = os.pread(fd, size - offset, offset)
    except OSError:
        return set()
    lines = blob.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines:
        # The first line is very likely truncated mid-record — drop it.
        lines = lines[1:]
    ids = set()
    for line in lines[-_DEDUPE_SCAN_LINES:]:
        if not line.strip():
            continue
        try:
            event_id = json.loads(line).get("event_id")
        except (ValueError, AttributeError):
            continue
        if event_id:
            ids.add(event_id)
    return ids


def append_event(root: Path, event: Mapping[str, Any]) -> Path:
    """Append one validated workflow event without changing workflow outcome."""
    import fcntl

    # Auto-stamp producer identity for the stability submetric (issue #663).
    # Only fill fields the caller did not provide — explicit values win so
    # tests / producers can override per-event. Empty string is the
    # documented "no identity recorded" signal.
    stamped = dict(event)
    for key, value in _default_identity().items():
        stamped.setdefault(key, value)
    record = validate_event(stamped)
    path = _event_path(Path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open in "a+" so the dedupe guard can read the tail under the same
    # exclusive lock that guards the append.
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            # Dedupe-on-write guard: if a producer hands us the same
            # event_id twice, regenerate once before persisting.
            recent_ids = _recent_event_ids(handle.fileno())
            if record["event_id"] in recent_ids:
                record["event_id"] = new_event_id()
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return path


def read_events(root: Path) -> List[Dict[str, Any]]:
    """Read valid structured events; malformed lines remain inspectable findings."""
    path = _event_path(Path(root))
    if not path.is_file():
        return []
    result: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            result.append(validate_event(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return result


def _event_cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Append a structured workflow event")
    parser.add_argument("action", choices=("append-event",))
    parser.add_argument("--type", dest="event_type", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workflow-id", default="workflow-unknown")
    parser.add_argument("--stage", default="unknown")
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--parent", dest="parent_id")
    parser.add_argument("--ts", default=None)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--source", default="hook")
    parser.add_argument("--evidence-json", default="{}")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    evidence = json.loads(args.evidence_json)
    timestamp = args.ts or now_utc()
    path = append_event(args.root, {
        "event_id": new_event_id(),
        "run_id": args.run_id,
        "workflow_id": args.workflow_id,
        "stage": args.stage,
        "event_type": args.event_type,
        "subject_id": args.subject_id,
        "parent_id": args.parent_id,
        "ts": timestamp,
        "outcome": args.outcome,
        "source": args.source,
        "evidence_ref": evidence,
    })
    print(path)
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "append-event":
        raise SystemExit(_event_cli())
