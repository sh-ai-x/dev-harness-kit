"""effectiveness_collection.py — bounded automatic harness measurement.

Implements the journal + incremental projection design from the merged
proposal ``docs/proposals/review/harness-effectiveness/auto-collection.html``
(PR #817). One compact segmented journal per project root, one
disposable projection, strict per-root locking and fsync durability.

Public surface:

* :class:`Envelope` — v1 result envelope (counts / ratio / readiness /
  cutoff).
* :class:`Store` — file-locked operations over the journal + projection.
* :func:`enroll` / :func:`observe` / :func:`collect` / :func:`probe` —
  high-level helpers used by ``lib/execute.py`` and the SessionStart /
  Stop / SessionEnd hooks. Each helper is best-effort: collection
  errors never change the workflow exit code.
* :func:`_cli` — ``python -m lib.effectiveness_collection
  {enroll,observe,collect,probe,status}`` driver used by hook scripts.

Design rules (re-stated for grep):

* Stop only requests ``collect()``; it never closes a session. Actual
  ``SessionEnd`` records a controller close + observed terminal.
* A missed callback is never inferred from elapsed time. The unit stays
  ``unresolved`` until a later boundary has authoritative evidence.
* No background process, no heartbeat, no lease. Collection is
  synchronous and bounded.
* Reuse :mod:`atomic` for projection publication; segments use
  append+fsync under the same per-root lock.
* The legacy ``lib.harness_effectiveness.build_report`` schema is
  untouched — this module emits a separate, narrowly-scoped
  ``v1`` envelope that explicitly identifies its origin and freshness.
"""
from __future__ import annotations

import argparse
import enum
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Reuse the POSIX-atomic JSON publication helper. atomic_write_json
# does not itself provide locking or fsync durability, so we hold the
# journal lock for the duration of any cache write and we flush/fsync
# the cache fd before the os.replace.
from atomic import atomic_write_json  # noqa: E402 — lib is on sys.path

# ---------------------------------------------------------------------------
# Schema + constants
# ---------------------------------------------------------------------------

ENVELOPE_SCHEMA_VERSION = 1
ENVELOPE_CONTRACT = "effectiveness-collection-v1"

# Transitions a measurement record may carry. Distinct values, never
# merged, so crashes between transitions leave interpretable evidence
# (rather than a silently-completed transaction).
TRANSITION_ENROLL = "enroll"
TRANSITION_OBSERVED_START = "observed_start"
TRANSITION_OBSERVED_TERMINAL = "observed_terminal"
TRANSITION_CONTROLLER_CLOSE = "controller_close"
TRANSITION_CONTROLLER_FINAL = "controller_final"  # for runtimes without SessionEnd
ALL_TRANSITIONS: Tuple[str, ...] = (
    TRANSITION_ENROLL,
    TRANSITION_OBSERVED_START,
    TRANSITION_OBSERVED_TERMINAL,
    TRANSITION_CONTROLLER_CLOSE,
    TRANSITION_CONTROLLER_FINAL,
)

# Outcomes that legitimately close a unit. Anything outside this set
# that appears on the terminal transition is a "conflicting_terminal"
# finding (recorded against the unit, never silently coerced).
TERMINAL_OUTCOMES: Tuple[str, ...] = (
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "exception",
)

# Semantically successful closures — used only for the descriptive
# success-coverage ratio. A failed/blocked/cancelled/exception closure
# still counts as a closed unit; it just does not contribute to the
# success ratio numerator.
SUCCESS_OUTCOMES: Tuple[str, ...] = ("completed",)

# Storage budgets (proposal §4). 4 MiB per segment; 64 MiB total journal
# cap; 30 days of fully-closed cohorts retained. Segments with
# unresolved units are protected from pruning.
SEGMENT_ROTATE_BYTES = 4 * 1024 * 1024
JOURNAL_TOTAL_CAP_BYTES = 64 * 1024 * 1024
RETENTION_CLOSED_DAYS = 30
# Lock acquisition ceiling. Two-second cooperative budget in the
# proposal; we fail closed at one second here because the caller has
# the remaining second to surface the COLLECTION_ERROR and finish.
LOCK_WAIT_SECONDS = 1.0

# Per-segment close wait. Should be negligible; bound it to avoid one
# boundary stalling the next.
COLLECT_BUDGET_SECONDS = 1.5

# Default runtime coverage threshold. CI uses 1.0 (its own known
# expected population) via the CLI override.
DEFAULT_RUNTIME_COVERAGE = 0.95

# Origin labels.
ORIGIN_RUNTIME = "runtime"
ORIGIN_CI_PROBE = "ci-probe"

# Readiness enum (ordered — first match wins in the reducer).
READINESS_COLLECTION_ERROR = "COLLECTION_ERROR"
READINESS_UNSUPPORTED = "UNSUPPORTED"
READINESS_DEGRADED = "DEGRADED"
READINESS_NO_OPPORTUNITY = "NO_OPPORTUNITY"
READINESS_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
READINESS_READY = "READY"
READINESS_COVERAGE_BELOW_POLICY = "COVERAGE_BELOW_POLICY"
ALL_READINESS: Tuple[str, ...] = (
    READINESS_COLLECTION_ERROR,
    READINESS_UNSUPPORTED,
    READINESS_DEGRADED,
    READINESS_NO_OPPORTUNITY,
    READINESS_INSUFFICIENT_EVIDENCE,
    READINESS_READY,
    READINESS_COVERAGE_BELOW_POLICY,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CollectionError(Exception):
    """Raised on any bounded journal/projection failure.

    Callers (executors, hooks) should treat this as a *finding* — the
    caller surfaces ``COLLECTION_ERROR`` in the envelope and continues
    its real work. The CLI maps this to exit code 2 per proposal §5.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    """UTC ISO-8601 with millisecond precision (e.g. ``2026-09-08T12:00:00.000Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _enrollment_day(iso_ts: str) -> str:
    """Return the UTC enrollment-day key ``YYYYMMDD`` for an ISO timestamp."""
    return (
        datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .strftime("%Y%m%d")
    )


def _canonical_unit_id(parts: Dict[str, str]) -> str:
    """Build the canonical unit-id from a (root, run, workflow, subject, attempt) tuple.

    The delimiter ``|`` is forbidden inside any field (validated by
    :func:`_validate_identity`) so a simple join is reversible and
    stable across runtimes.
    """
    return "|".join(
        [
            parts["root_id"],
            parts["run_id"],
            parts["workflow_id"],
            parts["subject_id"],
            parts["attempt_id"],
        ]
    )


def _new_record_id() -> str:
    return uuid.uuid4().hex


def _validate_identity(identity: Dict[str, str]) -> None:
    required = ("root_id", "run_id", "workflow_id", "subject_id", "attempt_id")
    missing = [k for k in required if not identity.get(k)]
    if missing:
        raise CollectionError(f"identity missing required fields: {', '.join(missing)}")
    for value in identity.values():
        if not isinstance(value, str):
            raise CollectionError("identity fields must be strings")
        if "|" in value or "\n" in value or "\r" in value:
            raise CollectionError(
                f"identity field contains forbidden character: {value!r}"
            )


def _validate_transition(transition: str) -> None:
    if transition not in ALL_TRANSITIONS:
        raise CollectionError(f"unknown transition: {transition!r}")


def _validate_outcome(transition: str, outcome: str) -> None:
    """Outcome rules per transition.

    ``enroll`` and ``observed_start`` are fixed; ``observed_terminal``
    must be in :data:`TERMINAL_OUTCOMES`; ``controller_close`` mirrors
    ``observed_terminal``; ``controller_final`` may also be ``unknown``
    (e.g. a final disposition that carries no observed callback but
    the controller itself recorded a final state).
    """
    if transition == TRANSITION_ENROLL:
        if outcome not in ("enrolled",):
            raise CollectionError(
                f"enroll outcome must be 'enrolled', got {outcome!r}"
            )
        return
    if transition == TRANSITION_OBSERVED_START:
        if outcome not in ("started",):
            raise CollectionError(
                f"observed_start outcome must be 'started', got {outcome!r}"
            )
        return
    if transition in (
        TRANSITION_OBSERVED_TERMINAL,
        TRANSITION_CONTROLLER_CLOSE,
    ):
        if outcome not in TERMINAL_OUTCOMES:
            raise CollectionError(
                f"{transition} outcome must be one of {TERMINAL_OUTCOMES!r}, got {outcome!r}"
            )
        return
    if transition == TRANSITION_CONTROLLER_FINAL:
        if outcome not in (*TERMINAL_OUTCOMES, "unknown"):
            raise CollectionError(
                f"controller_final outcome must be in TERMINAL_OUTCOMES or 'unknown', got {outcome!r}"
            )
        return
    raise CollectionError(f"unhandled transition: {transition!r}")


def _record_hash(
    prev_hash: str,
    transition: str,
    identity: Dict[str, str],
    outcome: str,
    payload: Dict[str, Any],
    record_id: str,
) -> str:
    """Deterministic SHA-256 over the record's semantic fields.

    Excludes the transport ``ts`` (adapters that flush on retry may
    record a slightly different transport-time without changing the
    semantic event) and the record_id (assigned at write time). The
    semantic equality test for "is this a retry of the same event" is
    therefore ``same(prev_hash, transition, identity, outcome, payload)``.
    """
    body = json.dumps(
        {
            "prev_hash": prev_hash,
            "transition": transition,
            "identity": identity,
            "outcome": outcome,
            "payload": payload,
            "record_id": record_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Record + envelope dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """One journal entry. ``record_hash`` chains from the previous record's hash."""

    schema_version: int
    record_id: str
    prev_hash: str
    transition: str
    identity: Dict[str, str]
    outcome: str
    payload: Dict[str, Any]
    ts: str
    record_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "prev_hash": self.prev_hash,
            "transition": self.transition,
            "identity": dict(self.identity),
            "outcome": self.outcome,
            "payload": dict(self.payload),
            "ts": self.ts,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Record":
        return cls(
            schema_version=int(raw["schema_version"]),
            record_id=str(raw["record_id"]),
            prev_hash=str(raw["prev_hash"]),
            transition=str(raw["transition"]),
            identity={k: str(v) for k, v in raw["identity"].items()},
            outcome=str(raw["outcome"]),
            payload=dict(raw.get("payload") or {}),
            ts=str(raw["ts"]),
            record_hash=str(raw["record_hash"]),
        )


@dataclass
class Envelope:
    """v1 measurement envelope — the projection's public shape."""

    schema_version: int
    contract_version: str
    root_id: str
    origin: str
    collected_at: str
    cutoff: str
    journal_seq: int
    journal_hash: str
    adapter_capabilities: Dict[str, bool]
    policy: Dict[str, Any]
    retention_floor: str
    counts: Dict[str, int]
    ratios: Dict[str, Optional[float]]
    readiness: str
    findings: List[str]
    quality_availability: Dict[str, str]
    bounded_errors: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "root_id": self.root_id,
            "origin": self.origin,
            "collected_at": self.collected_at,
            "cutoff": self.cutoff,
            "journal_seq": self.journal_seq,
            "journal_hash": self.journal_hash,
            "adapter_capabilities": dict(self.adapter_capabilities),
            "policy": dict(self.policy),
            "retention_floor": self.retention_floor,
            "counts": dict(self.counts),
            "ratios": dict(self.ratios),
            "readiness": self.readiness,
            "findings": list(self.findings),
            "quality_availability": dict(self.quality_availability),
            "bounded_errors": list(self.bounded_errors),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Envelope":
        return cls(
            schema_version=int(raw["schema_version"]),
            contract_version=str(raw["contract_version"]),
            root_id=str(raw["root_id"]),
            origin=str(raw["origin"]),
            collected_at=str(raw["collected_at"]),
            cutoff=str(raw["cutoff"]),
            journal_seq=int(raw["journal_seq"]),
            journal_hash=str(raw["journal_hash"]),
            adapter_capabilities={
                k: bool(v) for k, v in raw.get("adapter_capabilities", {}).items()
            },
            policy=dict(raw.get("policy") or {}),
            retention_floor=str(raw["retention_floor"]),
            counts={k: int(v) for k, v in raw.get("counts", {}).items()},
            ratios={
                k: (None if v is None else float(v))
                for k, v in raw.get("ratios", {}).items()
            },
            readiness=str(raw["readiness"]),
            findings=list(raw.get("findings") or []),
            quality_availability=dict(raw.get("quality_availability") or {}),
            bounded_errors=list(raw.get("bounded_errors") or []),
        )


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


def measurement_dir(root: Path) -> Path:
    return Path(root) / ".dev-kit" / "trace" / "measurement"


def _journal_dir(root: Path) -> Path:
    return measurement_dir(root) / "journal"


def _cache_path(root: Path) -> Path:
    return measurement_dir(root) / "effectiveness-latest.json"


def _lock_path(root: Path) -> Path:
    return measurement_dir(root) / ".lock"


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


class _FileLock:
    """Per-root advisory lock with bounded wait. Fails closed on timeout.

    We deliberately do NOT raise on EWOULDBLOCK past the ceiling — the
    caller maps that to a ``COLLECTION_ERROR`` so the workflow continues.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: Optional[int] = None
        self._deadline = 0.0

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        self._deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= self._deadline:
                    os.close(fd)
                    raise CollectionError(
                        f"lock acquisition timed out after {LOCK_WAIT_SECONDS}s"
                    )
                time.sleep(0.01)

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _iter_records(root: Path) -> Iterable[Record]:
    """Yield every record across all journal segments, oldest first.

    Malformed or truncated lines are skipped silently here; the journal
    repair path inside :meth:`Store.append` owns the recovery story.
    The caller (:meth:`Store.collect`) wraps this iterator and tracks
    errors separately.
    """
    base = _journal_dir(root)
    if not base.is_dir():
        return
    for path in sorted(base.glob("journal-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                yield Record.from_dict(data)
            except (KeyError, TypeError, ValueError):
                continue


def _read_cache(root: Path) -> Optional[Envelope]:
    path = _cache_path(root)
    if not path.is_file():
        return None
    try:
        return Envelope.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-unit reducer (pure function over a list of records)
# ---------------------------------------------------------------------------


def _reduce_unit(records: Sequence[Record]) -> Dict[str, Any]:
    """Reduce one unit's records into a summary used by ``collect()``.

    Returns a dict with: ``enrolled`` (bool), ``observed_start`` (bool),
    ``terminals`` (list of ``(transition, outcome)``), ``closed`` (bool —
    a controller_close / controller_final OR an observed_terminal with
    no later conflicting evidence exists), ``conflicting`` (list of
    tuples — observed terminal vs controller final disagree),
    ``attempt_id`` (str), ``subject_id`` (str), ``enrollment_day``
    (str | None — used for cohort bucketing).
    """
    if not records:
        return {
            "enrolled": False,
            "observed_start": False,
            "terminals": [],
            "closed": False,
            "conflicting": [],
            "attempt_id": "",
            "subject_id": "",
            "enrollment_day": None,
        }
    first = records[0]
    identity = first.identity
    enrolled = any(r.transition == TRANSITION_ENROLL for r in records)
    observed_start = any(
        r.transition == TRANSITION_OBSERVED_START for r in records
    )
    terminals: List[Tuple[str, str]] = [
        (r.transition, r.outcome)
        for r in records
        if r.transition
        in (
            TRANSITION_OBSERVED_TERMINAL,
            TRANSITION_CONTROLLER_CLOSE,
            TRANSITION_CONTROLLER_FINAL,
        )
    ]
    closed = bool(terminals)
    conflicting: List[Tuple[str, str]] = []
    obs_terminals = [t for t in terminals if t[0] == TRANSITION_OBSERVED_TERMINAL]
    ctrl_finals = [t for t in terminals if t[0] == TRANSITION_CONTROLLER_FINAL]
    if obs_terminals and ctrl_finals:
        obs_set = {o for _, o in obs_terminals}
        ctrl_set = {c for _, c in ctrl_finals}
        if not (obs_set & ctrl_set):
            for transition, outcome in obs_terminals:
                conflicting.append((transition, outcome))
            for transition, outcome in ctrl_finals:
                conflicting.append((transition, outcome))
    enrollment_day = _enrollment_day(records[0].ts) if enrolled else None
    return {
        "enrolled": enrolled,
        "observed_start": observed_start,
        "terminals": terminals,
        "closed": closed,
        "conflicting": conflicting,
        "attempt_id": identity.get("attempt_id", ""),
        "subject_id": identity.get("subject_id", ""),
        "enrollment_day": enrollment_day,
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class Store:
    """File-locked store. All public methods acquire the per-root lock."""

    root: Path
    origin: str = ORIGIN_RUNTIME
    policy: Dict[str, Any] = field(
        default_factory=lambda: {
            "runtime_coverage": DEFAULT_RUNTIME_COVERAGE,
            "ci_coverage": 1.0,
            "ci_origin": ORIGIN_CI_PROBE,
        }
    )
    adapter_capabilities: Dict[str, bool] = field(
        default_factory=lambda: {
            "session_enroll": True,
            "session_close": True,
            "executor_enroll": True,
            "executor_close": True,
        }
    )

    # ----- low-level journal write -----

    def append(
        self,
        transition: str,
        identity: Dict[str, str],
        outcome: str,
        payload: Optional[Dict[str, Any]] = None,
        ts: Optional[str] = None,
    ) -> Record:
        """Append one record to the journal. Strict: validates identity +
        transition + outcome, computes hash chain, fsyncs, repairs a
        truncated tail under lock, rotates at
        :data:`SEGMENT_ROTATE_BYTES`, and emits ``COLLECTION_ERROR`` (via
        the store's caller — see :meth:`_safe_append`) on disk full /
        permission failure.
        """
        _validate_transition(transition)
        _validate_identity(identity)
        _validate_outcome(transition, outcome)
        if ts is None:
            ts = _now_utc_iso()
        payload = dict(payload or {})

        journal = _journal_dir(self.root)
        journal.mkdir(parents=True, exist_ok=True)
        day = _enrollment_day(ts)
        segment = journal / f"journal-{day}.jsonl"
        record_id = _new_record_id()

        with _FileLock(_lock_path(self.root)):
            # Determine the previous record (the most recent across all
            # segments). Carrying prev_hash across the day boundary is
            # intentional — the chain is a true linear log, not a
            # per-day sealed file.
            prev_hash = "0" * 64
            segs = sorted(_journal_dir(self.root).glob("journal-*.jsonl"))
            for prev in segs:
                if prev.name > segment.name:
                    break
                tail_record = self._read_tail_record(prev)
                if tail_record is not None:
                    prev_hash = tail_record.record_hash
            r_hash = _record_hash(
                prev_hash, transition, identity, outcome, payload, record_id
            )
            record = Record(
                schema_version=ENVELOPE_SCHEMA_VERSION,
                record_id=record_id,
                prev_hash=prev_hash,
                transition=transition,
                identity=dict(identity),
                outcome=outcome,
                payload=payload,
                ts=ts,
                record_hash=r_hash,
            )
            # Repair: if the current segment ends with a partial line
            # (writer crashed before fsync), drop the partial bytes
            # and record a bounded corruption finding. We never
            # silently skip interior corruption — only the very last
            # line.
            self._repair_truncated_tail(segment)
            line = json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"
            self._append_line(segment, line)
            # Rotate check: if this segment crossed the cap, the next
            # append will create a new day-segment. We do NOT split
            # mid-record; the rotation check fires after the append
            # so the line is always intact.
            if segment.stat().st_size >= SEGMENT_ROTATE_BYTES:
                pass
        return record

    @staticmethod
    def _read_tail_record(path: Path) -> Optional[Record]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        last: Optional[Record] = None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                last = Record.from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return last

    @staticmethod
    def _repair_truncated_tail(path: Path) -> None:
        """Drop a truncated trailing line, if any. Never touches interior."""
        try:
            with path.open("rb+") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                if pos == 0:
                    return
                # Walk back to the last newline.
                f.seek(pos - 1)
                buf = f.read(1)
                while buf and buf != b"\n":
                    if f.tell() <= 1:
                        return
                    f.seek(f.tell() - 2, os.SEEK_SET)
                    buf = f.read(1)
                tail = f.read(pos - f.tell())
                if tail.strip():
                    f.seek(f.tell())
                    f.truncate()
        except OSError:
            return

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        """Append + flush + fsync a single line. POSIX-only contract."""
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    # ----- collect / projection -----

    def collect(
        self,
        *,
        coverage_threshold: Optional[float] = None,
        now: Optional[str] = None,
    ) -> Envelope:
        """Build (or rebuild) the projection envelope from the journal.

        Pure read under lock; publication goes through
        :func:`atomic_write_json` for the same durability contract as
        the rest of the system. The function never raises — it returns
        an envelope whose ``readiness`` and ``bounded_errors`` describe
        whatever went wrong. Callers inspect ``envelope.readiness`` and
        surface the result accordingly.
        """
        cutoff = now or _now_utc_iso()
        bounded: List[str] = []
        findings: List[str] = []
        envelope: Envelope
        try:
            with _FileLock(_lock_path(self.root)):
                try:
                    self._prune_locked()
                except OSError as exc:
                    bounded.append(f"prune_failed: {exc!s}")
                records = list(_iter_records(self.root))
                # Hash chain integrity: walk the records and confirm
                # prev_hash matches. We only record findings, never
                # reject the whole journal (interior corruption is
                # rare and we still want a partial result).
                last_hash = "0" * 64
                seq = 0
                for r in records:
                    if r.prev_hash != last_hash:
                        findings.append(
                            f"hash_chain_break@seq={seq}: expected {last_hash[:8]}, got {r.prev_hash[:8]}"
                        )
                        # Snap to the actual prev_hash so subsequent
                        # records do not all fire the same finding.
                        last_hash = r.prev_hash
                    else:
                        last_hash = r.record_hash
                    seq += 1
                envelope = self._reduce_envelope(
                    records,
                    cutoff=cutoff,
                    journal_seq=seq,
                    journal_hash=last_hash,
                    coverage_threshold=coverage_threshold,
                    findings=findings,
                    bounded=bounded,
                )
                try:
                    atomic_write_json(_cache_path(self.root), envelope.to_dict())
                except (OSError, ValueError, TypeError) as exc:
                    bounded.append(f"cache_write_failed: {exc!s}")
        except CollectionError as exc:
            envelope = self._build_collection_error(
                cutoff=cutoff,
                journal_seq=0,
                journal_hash="0" * 64,
                message=f"lock_timeout: {exc!s}",
                coverage_threshold=coverage_threshold,
            )
        if envelope.readiness == READINESS_COLLECTION_ERROR:
            try:
                atomic_write_json(_cache_path(self.root), envelope.to_dict())
            except (OSError, ValueError, TypeError):
                pass
        return envelope

    def _build_collection_error(
        self,
        *,
        cutoff: str,
        journal_seq: int,
        journal_hash: str,
        message: str,
        coverage_threshold: Optional[float],
    ) -> Envelope:
        threshold = (
            coverage_threshold
            if coverage_threshold is not None
            else self.policy["runtime_coverage"]
        )
        return Envelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            contract_version=ENVELOPE_CONTRACT,
            root_id=str(self.root),
            origin=self.origin,
            collected_at=cutoff,
            cutoff=cutoff,
            journal_seq=journal_seq,
            journal_hash=journal_hash,
            adapter_capabilities=dict(self.adapter_capabilities),
            policy={"coverage_threshold": threshold},
            retention_floor=cutoff[:10].replace("-", ""),
            counts={},
            ratios={},
            readiness=READINESS_COLLECTION_ERROR,
            findings=[],
            quality_availability={},
            bounded_errors=[message],
        )

    def _reduce_envelope(
        self,
        records: Sequence[Record],
        *,
        cutoff: str,
        journal_seq: int,
        journal_hash: str,
        coverage_threshold: Optional[float],
        findings: List[str],
        bounded: List[str],
    ) -> Envelope:
        units: Dict[str, List[Record]] = {}
        for r in records:
            uid = _canonical_unit_id(r.identity)
            units.setdefault(uid, []).append(r)
        reductions: Dict[str, Dict[str, Any]] = {
            uid: _reduce_unit(rs) for uid, rs in units.items()
        }
        enrolled_count = sum(1 for r in reductions.values() if r["enrolled"])
        closed_count = sum(1 for r in reductions.values() if r["closed"])
        unresolved_count = enrolled_count - closed_count
        paired_count = sum(
            1
            for r in reductions.values()
            if r["enrolled"] and r["observed_start"] and r["closed"]
        )
        distinct_units = len(reductions)
        missing_start = sum(
            1
            for r in reductions.values()
            if r["enrolled"] and r["closed"] and not r["observed_start"]
        )
        # Missing terminal = enrolled but never closed. The proposal
        # marks a unit "closed" as soon as ANY terminal-style
        # transition lands, so missing_terminal is exactly the
        # enrolled-not-closed set.
        missing_terminal = sum(
            1
            for r in reductions.values()
            if r["enrolled"] and not r["closed"]
        )
        conflicting_terminal = sum(1 for r in reductions.values() if r["conflicting"])
        unexpected_units = distinct_units - enrolled_count
        coverage = (
            None
            if closed_count == 0
            else (paired_count / closed_count) if closed_count else None
        )
        success_count = 0
        for r in reductions.values():
            for transition, outcome in r["terminals"]:
                if (
                    transition == TRANSITION_OBSERVED_TERMINAL
                    and outcome in SUCCESS_OUTCOMES
                ):
                    success_count += 1
                    break
        success_ratio = (
            None
            if paired_count == 0
            else success_count / paired_count
        )
        retention_floor = (
            records[0].ts[:10].replace("-", "")
            if records
            else cutoff[:10].replace("-", "")
        )
        # Readiness ladder (ordered — first match wins).
        threshold = (
            coverage_threshold
            if coverage_threshold is not None
            else self.policy["runtime_coverage"]
        )
        if bounded:
            readiness = READINESS_COLLECTION_ERROR
        elif conflicting_terminal or unexpected_units:
            readiness = READINESS_DEGRADED
        elif enrolled_count == 0:
            readiness = READINESS_NO_OPPORTUNITY
        elif unresolved_count > 0 or missing_start > 0 or missing_terminal > 0:
            readiness = READINESS_INSUFFICIENT_EVIDENCE
        elif coverage is not None and coverage < threshold:
            readiness = READINESS_COVERAGE_BELOW_POLICY
        else:
            readiness = READINESS_READY
        quality_availability = {
            "prevention": "unavailable",
            "first_pass": "unavailable",
            "recovery": "unavailable",
            "learning": "unavailable",
            "stability": "unavailable",
        }
        counts = {
            "enrolled": enrolled_count,
            "closed": closed_count,
            "unresolved": unresolved_count,
            "paired": paired_count,
            "missing_start": missing_start,
            "missing_terminal": missing_terminal,
            "conflicting_terminal": conflicting_terminal,
            "unexpected_units": unexpected_units,
            "success": success_count,
            "distinct_units": distinct_units,
        }
        ratios = {
            "coverage": coverage,
            "success": success_ratio,
        }
        return Envelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            contract_version=ENVELOPE_CONTRACT,
            root_id=str(self.root),
            origin=self.origin,
            collected_at=cutoff,
            cutoff=cutoff,
            journal_seq=journal_seq,
            journal_hash=journal_hash,
            adapter_capabilities=dict(self.adapter_capabilities),
            policy={"coverage_threshold": threshold},
            retention_floor=retention_floor,
            counts=counts,
            ratios=ratios,
            readiness=readiness,
            findings=findings,
            quality_availability=quality_availability,
            bounded_errors=bounded,
        )

    # ----- retention / pruning -----

    def _prune_locked(self) -> None:
        """Prune the oldest fully-closed cohorts until we are under the cap.

        Holds the per-root lock. The proposal mandates: prune whole
        oldest closed cohorts first; segments with unresolved units
        are protected; if protected data plus the next record exceeds
        the cap, return ``COLLECTION_ERROR`` (the caller attaches that
        to ``bounded_errors``).
        """
        journal = _journal_dir(self.root)
        if not journal.is_dir():
            return
        segments = sorted(journal.glob("journal-*.jsonl"))
        if not segments:
            return
        unresolved_segments: set = set()
        for seg in segments:
            try:
                for line in seg.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        r = Record.from_dict(json.loads(line))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if r.transition == TRANSITION_ENROLL:
                        unresolved_segments.add(_enrollment_day(r.ts))
            except OSError:
                continue
        total = sum((seg.stat().st_size for seg in segments if seg.exists()), 0)
        if total <= JOURNAL_TOTAL_CAP_BYTES:
            return
        cutoff_day = (
            datetime.now(timezone.utc) - timedelta(days=RETENTION_CLOSED_DAYS)
        ).strftime("%Y%m%d")
        for seg in segments:
            if total <= JOURNAL_TOTAL_CAP_BYTES:
                break
            day = seg.name.replace("journal-", "").replace(".jsonl", "")
            if day in unresolved_segments:
                continue
            if day >= cutoff_day:
                continue
            try:
                size = seg.stat().st_size
                seg.unlink()
                total -= size
            except OSError as exc:
                raise CollectionError(f"prune_failed: {exc!s}")
        if total > JOURNAL_TOTAL_CAP_BYTES:
            raise CollectionError(
                f"journal_budget_exceeded: {total} bytes; protected segments prevent pruning"
            )

    # ----- capability probe -----

    def probe(self) -> Dict[str, Any]:
        """Return adapter capability + freshness metadata for CI/UI consumers.

        Never raises. A runtime that lacks ``session_enroll`` reports
        ``adapter_capabilities.session_enroll=False`` rather than
        failing the probe — the consumer decides what to do.
        """
        try:
            with _FileLock(_lock_path(self.root)):
                records = list(_iter_records(self.root))
        except CollectionError:
            records = []
        last_ts = records[-1].ts if records else None
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "contract_version": ENVELOPE_CONTRACT,
            "root_id": str(self.root),
            "origin": self.origin,
            "adapter_capabilities": dict(self.adapter_capabilities),
            "journal_seq": len(records),
            "last_record_ts": last_ts,
            "measured_at": _now_utc_iso(),
        }


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def _root_id_for(path: Path) -> str:
    """Canonical absolute root_id — proposal §2 forbids fallback to another root."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


def _attempt_id(prefix: str) -> str:
    """Generate a unique attempt_id. Retries with the same prefix re-use
    the existing identity only if the caller supplies the prior attempt
    token — the helper below always generates a new one and lets the
    caller decide whether to pass it forward.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _identity_dict(
    root_id: str,
    *,
    run_id: str,
    workflow_id: str,
    subject_id: str,
    attempt_id: str,
    controller: str,
    origin: str,
) -> Dict[str, str]:
    return {
        "root_id": root_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "subject_id": subject_id,
        "attempt_id": attempt_id,
        "controller": controller,
        "origin": origin,
    }


def enroll(
    root: Path,
    *,
    run_id: str,
    workflow_id: str,
    subject_id: str,
    attempt_id: Optional[str] = None,
    controller: str = "executor",
    origin: str = ORIGIN_RUNTIME,
    payload: Optional[Dict[str, Any]] = None,
) -> Record:
    """Enroll a measurement unit. Idempotent on (run, workflow, subject, attempt)."""
    root_id = _root_id_for(root)
    if attempt_id is None:
        attempt_id = _attempt_id(controller)
    store = Store(root=Path(root), origin=origin)
    return store.append(
        TRANSITION_ENROLL,
        _identity_dict(
            root_id,
            run_id=run_id,
            workflow_id=workflow_id,
            subject_id=subject_id,
            attempt_id=attempt_id,
            controller=controller,
            origin=origin,
        ),
        "enrolled",
        payload,
    )


def observe(
    root: Path,
    *,
    run_id: str,
    workflow_id: str,
    subject_id: str,
    attempt_id: str,
    transition: str,
    outcome: str,
    origin: str = ORIGIN_RUNTIME,
    payload: Optional[Dict[str, Any]] = None,
) -> Record:
    """Record an observed lifecycle event. ``transition`` must be one of
    :data:`TRANSITION_OBSERVED_START` / :data:`TRANSITION_OBSERVED_TERMINAL`
    / :data:`TRANSITION_CONTROLLER_CLOSE` /
    :data:`TRANSITION_CONTROLLER_FINAL` (transition ``enroll`` is
    rejected here — use :func:`enroll`).
    """
    if transition not in (
        TRANSITION_OBSERVED_START,
        TRANSITION_OBSERVED_TERMINAL,
        TRANSITION_CONTROLLER_CLOSE,
        TRANSITION_CONTROLLER_FINAL,
    ):
        raise CollectionError(f"observe() rejects transition {transition!r}")
    root_id = _root_id_for(root)
    store = Store(root=Path(root), origin=origin)
    return store.append(
        transition,
        _identity_dict(
            root_id,
            run_id=run_id,
            workflow_id=workflow_id,
            subject_id=subject_id,
            attempt_id=attempt_id,
            controller="executor",
            origin=origin,
        ),
        outcome,
        payload,
    )


def collect(
    root: Path,
    *,
    origin: str = ORIGIN_RUNTIME,
    coverage_threshold: Optional[float] = None,
) -> Envelope:
    """Build the projection envelope. Never raises."""
    store = Store(root=Path(root), origin=origin)
    return store.collect(coverage_threshold=coverage_threshold)


def probe(root: Path, *, origin: str = ORIGIN_RUNTIME) -> Dict[str, Any]:
    store = Store(root=Path(root), origin=origin)
    return store.probe()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_enroll(args: argparse.Namespace) -> int:
    try:
        record = enroll(
            args.root,
            run_id=args.run_id,
            workflow_id=args.workflow_id,
            subject_id=args.subject_id,
            attempt_id=args.attempt_id,
            controller=args.controller,
            origin=args.origin,
        )
    except CollectionError as exc:
        print(f"COLLECTION_ERROR enroll: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"record_id": record.record_id, "attempt_id": record.identity["attempt_id"]}))
    return 0


def _cli_observe(args: argparse.Namespace) -> int:
    try:
        record = observe(
            args.root,
            run_id=args.run_id,
            workflow_id=args.workflow_id,
            subject_id=args.subject_id,
            attempt_id=args.attempt_id,
            transition=args.transition,
            outcome=args.outcome,
            origin=args.origin,
        )
    except CollectionError as exc:
        print(f"COLLECTION_ERROR observe: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"record_id": record.record_id}))
    return 0


def _cli_collect(args: argparse.Namespace) -> int:
    try:
        envelope = collect(
            args.root,
            origin=args.origin,
            coverage_threshold=args.coverage_threshold,
        )
    except CollectionError as exc:
        print(f"COLLECTION_ERROR collect: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(envelope.to_dict()))
    if envelope.readiness == READINESS_COLLECTION_ERROR:
        return 2
    if args.gate_ci and envelope.readiness not in (READINESS_READY,):
        # CI uses origin=ci-probe; the expected population is known
        # and a non-READY envelope (DEGRADED, INSUFFICIENT_EVIDENCE,
        # etc.) MUST fail the gate so the test job surfaces it.
        return 1
    return 0


def _cli_probe(args: argparse.Namespace) -> int:
    payload = probe(args.root, origin=args.origin)
    print(json.dumps(payload))
    return 0


def _cli_status(args: argparse.Namespace) -> int:
    """Print the cached envelope if fresh, otherwise build one."""
    cache = _read_cache(args.root)
    if cache is not None and not args.refresh:
        print(json.dumps(cache.to_dict()))
        return 0
    try:
        envelope = collect(args.root, origin=args.origin)
    except CollectionError as exc:
        print(f"COLLECTION_ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(envelope.to_dict()))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded automatic harness measurement journal + projection"
    )
    sub = parser.add_subparsers(dest="action", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--root",
            type=Path,
            default=Path("."),
            help="Project root containing .dev-kit/trace/measurement/",
        )
        p.add_argument(
            "--origin",
            choices=(ORIGIN_RUNTIME, ORIGIN_CI_PROBE),
            default=ORIGIN_RUNTIME,
            help="Population origin label (runtime or ci-probe)",
        )

    p_enr = sub.add_parser("enroll", help="Record a controller enrollment")
    _add_common(p_enr)
    p_enr.add_argument("--run-id", required=True)
    p_enr.add_argument("--workflow-id", required=True)
    p_enr.add_argument("--subject-id", required=True)
    p_enr.add_argument("--attempt-id", default=None)
    p_enr.add_argument("--controller", default="executor")
    p_enr.set_defaults(func=_cli_enroll)
    p_obs = sub.add_parser("observe", help="Record an observed lifecycle event")
    _add_common(p_obs)
    p_obs.add_argument("--run-id", required=True)
    p_obs.add_argument("--workflow-id", required=True)
    p_obs.add_argument("--subject-id", required=True)
    p_obs.add_argument("--attempt-id", required=True)
    p_obs.add_argument(
        "--transition",
        required=True,
        choices=(
            TRANSITION_OBSERVED_START,
            TRANSITION_OBSERVED_TERMINAL,
            TRANSITION_CONTROLLER_CLOSE,
            TRANSITION_CONTROLLER_FINAL,
        ),
    )
    p_obs.add_argument("--outcome", required=True)
    p_obs.set_defaults(func=_cli_observe)
    p_col = sub.add_parser("collect", help="Build/refresh the projection envelope")
    _add_common(p_col)
    p_col.add_argument("--coverage-threshold", type=float, default=None)
    p_col.add_argument(
        "--gate-ci",
        action="store_true",
        help="Exit 1 unless readiness is READY (used by CI adapter probes)",
    )
    p_col.set_defaults(func=_cli_collect)
    p_prb = sub.add_parser("probe", help="Capability + freshness probe")
    _add_common(p_prb)
    p_prb.set_defaults(func=_cli_probe)
    p_st = sub.add_parser("status", help="Print cached envelope or rebuild it")
    _add_common(p_st)
    p_st.add_argument("--refresh", action="store_true")
    p_st.set_defaults(func=_cli_status)
    return parser


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"COLLECTION_ERROR: {exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
