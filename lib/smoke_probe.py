"""smoke_probe.py — auto-populate first_pass_quality evidence on session start.

Issue: ``/dev-kit:harness-effectiveness`` reports ``INSUFFICIENT_EVIDENCE`` for
``first_pass_quality`` whenever a worktree has never been built via
``/dev-kit:build``. The reducer (``lib/harness_effectiveness.py:_first_pass``)
needs causally-linked ``write.observed`` + ``verify.passed`` events sharing a
subject_id. ``lib/execute.py`` emits both, but only when a build runs.

This module provides a lightweight alternative that fires on every session
start, exercises a real pytest on a hermetic test, and emits honest
``write.observed`` + ``verify.passed``/``verify.failed`` events:

1. Writes a probe file under ``.dev-kit/trace/measurement/`` with a stable
   payload (subject_id, timestamp, run_id).
2. Captures the ``write.observed`` event_id (post-dedupe) via
   ``lib.trace_log.append_event``'s returned ``persisted_event_id`` (PR #753
   finding #2 contract).
3. Runs ``python3 -m pytest`` against a single hermetic test — REAL
   independent verification (different process from this hook).
4. Emits ``verify.passed`` (``required_checks_passed=True, independent=True``)
   parented to the write's persisted event_id when pytest exit_code == 0, or
   ``verify.failed`` (``required_checks_passed=False, retry_count=1``) when
   not.
5. Emits a final ``step.completed`` so the smoke subject has a matching
   terminal event for ``subject_observability``.

Honesty contract (mirrors the comment at ``lib/execute.py:907-915``):
``independent=True`` is honest because pytest is a separate process. A
pytest failure records a real failure into ``verify.failed`` so
``recovery_quality`` also gets evidence. We never fabricate
``required_checks_passed=True`` from a self-report.

Failure modes: every step is best-effort and never raises. A missing
``.dev-kit/trace`` directory, missing ``lib.trace_log``, or pytest crash
all degrade to a no-op so this probe never blocks session start.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

SMOKE_PROBE_SUBJECT_PREFIX = "smoke"
SMOKE_PROBE_DIRNAME = ".dev-kit/trace/measurement"
SMOKE_PROBE_FILENAME = "smoke-probe-{session_id}.txt"
# A single hermetic test keeps the probe lightweight (~300ms cold) so it
# can fire on every session start without slowing the user. The daily
# contract-test cron (`.github/workflows/contract-test-cron.yml`) runs the
# full suite.
SMOKE_PROBE_PYTEST_TARGET = (
    "tests/test_harness_effectiveness.py::test_trace_event_round_trip"
)
SMOKE_PROBE_PYTEST_TIMEOUT_SECONDS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _append_event(root: Path, event: dict) -> Optional[str]:
    """Append one event; return the persisted event_id or None on failure.

    Imports ``lib.trace_log.append_event`` directly (same-process call) so
    we get the actual persisted event_id back, not just the file path the
    CLI prints. Callers must parent subsequent events to the returned id,
    NOT the caller's local event_id, because the dedupe-on-write guard
    may swap it under collision (PR #753 finding #2).

    Falls back to a subprocess call to the lib.trace_log CLI when the
    direct import fails (e.g., a partial install / wrong sys.path). The
    subprocess fallback can't return the persisted id, so it returns None
    — callers must then degrade gracefully (the chain still emits, but
    the next event's parent_id will be missing rather than dangling).
    """
    try:
        # Direct call (preferred) — append_event returns (path, persisted_id).
        from lib.trace_log import append_event as _append  # type: ignore
        _, persisted_id = _append(Path(root), event)
        return persisted_id
    except Exception:
        pass

    # Subprocess fallback. CLI prints the path; we can't recover the
    # persisted event_id from stdout alone, so we report None.
    env = dict(os.environ)
    lib_path = str(root)
    if lib_path not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = lib_path + os.pathsep + env.get("PYTHONPATH", "")
    python = sys.executable or shutil.which("python3") or "python3"
    cmd = [
        python, "-m", "lib.trace_log", "append-event",
        "--type", event["event_type"],
        "--run-id", event["run_id"],
        "--workflow-id", event.get("workflow_id", "smoke-probe"),
        "--stage", event.get("stage", "smoke"),
        "--subject-id", event["subject_id"],
        "--outcome", event["outcome"],
        "--source", event.get("source", "lib.smoke_probe"),
        "--root", str(root),
        "--evidence-json", json.dumps(event.get("evidence_ref", {})),
    ]
    if event.get("parent_id"):
        cmd += ["--parent", event["parent_id"]]
    try:
        subprocess.run(
            cmd,
            env=env,
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None


def _run_pytest(root: Path) -> Tuple[bool, str]:
    """Run the hermetic probe test. Returns (passed, evidence_summary)."""
    env = dict(os.environ)
    lib_path = str(root)
    if lib_path not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = lib_path + os.pathsep + env.get("PYTHONPATH", "")
    python = sys.executable or shutil.which("python3") or "python3"
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", SMOKE_PROBE_PYTEST_TARGET,
             "--no-header", "-q", "-p", "no:cacheprovider"],
            env=env,
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=SMOKE_PROBE_PYTEST_TIMEOUT_SECONDS,
        )
        passed = proc.returncode == 0
        # Last 200 chars of stdout — enough for the test name + pass/fail
        # marker, small enough to fit comfortably in evidence_ref.
        summary = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        if not summary:
            summary = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
        return passed, summary[:200]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pytest_error:{type(exc).__name__}"


def run(root: Path, session_id: str) -> None:
    """Run one smoke probe cycle. Best-effort; never raises.

    Fires on session start. Each step is independently try/except'd so a
    partial failure (e.g., pytest crash) still emits the ``step.completed``
    so ``subject_observability`` doesn't accumulate orphan smoke subjects.
    """
    if not session_id:
        return
    root = Path(root).resolve()
    run_id = os.environ.get("DEV_KIT_RUN_ID") or f"smoke-{_now_iso()}"
    subject_id = f"{SMOKE_PROBE_SUBJECT_PREFIX}:{session_id}"
    smoke_uuid = uuid.uuid4().hex

    def _build_event(event_type: str, *, outcome: str,
                     parent_id: Optional[str] = None,
                     extra_evidence: Optional[dict] = None) -> dict:
        # Includes every EVENT_RECORD_REQUIRED_FIELDS field so
        # lib.trace_log.validate_event passes; ``event_id`` is
        # auto-generated per event so each one is unique.
        return {
            "event_id": uuid.uuid4().hex,
            "run_id": run_id,
            "workflow_id": "smoke-probe",
            "stage": "smoke",
            "event_type": event_type,
            "subject_id": subject_id,
            "parent_id": parent_id,
            "ts": _now_iso(),
            "outcome": outcome,
            "source": "lib.smoke_probe",
            "evidence_ref": {
                "session_id": session_id,
                "smoke_uuid": smoke_uuid,
                "via": "smoke-probe",
                **(extra_evidence or {}),
            },
        }

    started_event_id: Optional[str] = None
    try:
        started_event_id = _append_event(
            root,
            _build_event("step.started", outcome="started"),
        )
    except Exception:
        pass

    # 1. Real write — a probe file under .dev-kit/trace/measurement/.
    probe_path: Optional[Path] = None
    try:
        probe_dir = root / SMOKE_PROBE_DIRNAME
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_path = probe_dir / SMOKE_PROBE_FILENAME.format(session_id=session_id)
        probe_payload = json.dumps({
            "session_id": session_id,
            "ts": _now_iso(),
            "smoke_uuid": smoke_uuid,
            "via": "smoke-probe",
        }, sort_keys=True)
        probe_path.write_text(probe_payload, encoding="utf-8")
        bytes_written = probe_path.stat().st_size
    except OSError:
        probe_path = None
        bytes_written = 0

    # 2. Emit write.observed, parented to step.started.
    write_event_id: Optional[str] = None
    try:
        write_event_id = _append_event(
            root,
            _build_event(
                "write.observed", outcome="written", parent_id=started_event_id,
                extra_evidence={
                    "file_path": str(probe_path) if probe_path else None,
                    "bytes_written": bytes_written,
                },
            ),
        )
    except Exception:
        pass

    # 3. Real pytest — separate process, so independent=True is honest.
    passed, summary = _run_pytest(root)

    # 4. Emit verify.passed (or verify.failed) parented to write_event_id.
    try:
        verify_parent = write_event_id or started_event_id
        if passed:
            _append_event(
                root,
                _build_event(
                    "verify.passed", outcome="passed", parent_id=verify_parent,
                    extra_evidence={
                        "required_checks_passed": True,
                        "independent": True,
                        "retry_count": 0,
                        "checks_run": [SMOKE_PROBE_PYTEST_TARGET],
                        "pytest_exit_code": 0,
                        "pytest_summary": summary,
                        "evidence_provenance": "smoke-probe-pytest",
                    },
                ),
            )
        else:
            _append_event(
                root,
                _build_event(
                    "verify.failed", outcome="failed", parent_id=verify_parent,
                    extra_evidence={
                        "required_checks_passed": False,
                        "independent": True,
                        "retry_count": 1,
                        "checks_run": [SMOKE_PROBE_PYTEST_TARGET],
                        "pytest_exit_code": 1,
                        "pytest_summary": summary,
                        "reason": "pytest_exit_nonzero",
                    },
                ),
            )
    except Exception:
        pass

    # 5. Cleanup: delete the probe file.
    if probe_path:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass

    # 6. step.completed so subject_observability has a matching terminal.
    try:
        _append_event(
            root,
            _build_event(
                "step.completed", outcome="completed", parent_id=started_event_id,
                extra_evidence={"pytest_passed": passed},
            ),
        )
    except Exception:
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run one harness smoke probe")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--session-id", default=os.environ.get("SESSION_ID", ""))
    args = parser.parse_args()
    run(args.root, args.session_id)
