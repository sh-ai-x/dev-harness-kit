"""contract_test_emitter.py — emit ``contract.test`` evidence from CI / cron.

Issue: ``lib.harness_effectiveness.stability.contract_test_pass_rate`` waits
for ``contract.test`` events (issue #663). No producer exists in the repo.
This script runs a hermetic contract test pass and emits exactly one
``contract.test`` event with outcome ``passed`` or ``failed``.

Two callers:
  - `.github/workflows/contract-test-cron.yml` — daily schedule + manual dispatch.
  - Local invocation: ``python3 tools/contract_test_emitter.py --root .``.

Design rules (mirrors ``lib/trace_log.append_event`` contract):

* Idempotent on rerun: appends one event per invocation. The bounded journal
  projection (``lib/effectiveness_collection.py``) collapses repeat runs by
  their (run_id, workflow_id, subject_id) tuple, so a daily cron produces
  one canonical event per day.
* Never raises. All subprocess failures degrade to ``outcome="failed"`` and
  still emit the event so the metric surfaces the broken CI honestly.
* Subject id is fixed at ``harness-contract`` (matches the fixture in
  ``tests/test_harness_stability.py:338``).
* The contract test suite is hermetic: ``tests/test_harness_effectiveness.py``
  + ``tests/test_harness_stability.py``. Both are fast (<5 s) and
  dependency-free.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

CONTRACT_TEST_SUBJECT_ID = "harness-contract"
HERMETIC_TEST_TARGETS = (
    "tests/test_harness_effectiveness.py",
    "tests/test_harness_stability.py",
)
PYTEST_SUMMARY_RE = re.compile(r"(?P<passed>\d+)\s+passed")


def _run_pytest(root: Path, output_path: Path) -> int:
    """Run the hermetic contract tests; return the pytest exit code."""
    env = dict(os.environ)
    lib_path = str(root)
    if lib_path not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = lib_path + os.pathsep + env.get("PYTHONPATH", "")
    python = sys.executable or shutil.which("python3") or "python3"
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", *HERMETIC_TEST_TARGETS,
             "--no-header", "-q", "-p", "no:cacheprovider"],
            env=env,
            cwd=str(root),
            check=False,
            stdout=output_path.open("w") if False else None,  # see below
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        # Capture stdout/stderr into output_path. The pytest short summary
        # line we need is on stdout.
        output_path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        output_path.write_text(f"contract_test_emitter: pytest failed: {exc!r}", encoding="utf-8")
        return 1


def _summary_from_output(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = PYTEST_SUMMARY_RE.search(text)
    passed = int(match.group("passed")) if match else None
    last_line = next(
        (line.strip() for line in reversed(text.splitlines()) if line.strip()),
        "",
    )
    return {"tests_passed": passed, "summary_line": last_line[:200]}


def _append_event(root: Path, *, event_type: str, subject_id: str,
                  run_id: str, outcome: str, evidence_ref: dict) -> Optional[str]:
    """Append one contract.test event; return persisted event_id or None.

    Imports ``lib.trace_log.append_event`` directly (same-process call)
    so we get the actual persisted event_id back. Falls back to a
    subprocess call when the direct import fails (partial install /
    wrong sys.path). The subprocess fallback cannot recover the
    persisted event_id from the CLI's stdout (which prints the path),
    so it returns None.
    """
    import json as _json

    full_event = {
        "event_id": uuid.uuid4().hex,
        "run_id": run_id,
        "workflow_id": "contract",
        "stage": "contract",
        "event_type": event_type,
        "subject_id": subject_id,
        "parent_id": None,
        "ts": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "outcome": outcome,
        "source": "tools.contract_test_emitter",
        "evidence_ref": evidence_ref,
    }

    try:
        from lib.trace_log import append_event as _append  # type: ignore
        _, persisted_id = _append(Path(root), full_event)
        return persisted_id
    except Exception:
        pass

    # Subprocess fallback. PYTHONPATH must include the repo root so
    # ``python -m lib.trace_log`` resolves; lib lives at <repo>/lib/,
    # NOT at the worktree root (where the events.jsonl file lives).
    env = dict(os.environ)
    lib_path = str(Path(__file__).resolve().parents[1])
    if lib_path not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = lib_path + os.pathsep + env.get("PYTHONPATH", "")
    python = sys.executable or shutil.which("python3") or "python3"
    cmd = [
        python, "-m", "lib.trace_log", "append-event",
        "--type", event_type,
        "--run-id", run_id,
        "--workflow-id", "contract",
        "--stage", "contract",
        "--subject-id", subject_id,
        "--outcome", outcome,
        "--source", "tools.contract_test_emitter",
        "--root", str(root),
        "--evidence-json", _json.dumps(evidence_ref),
    ]
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


def emit(root: Path, *, run_id: Optional[str] = None,
         output_path: Optional[Path] = None) -> int:
    """Run contract tests + emit one ``contract.test`` event.

    Returns the pytest exit code (0 = passed). Never raises; failures
    always emit a ``contract.test`` event with ``outcome="failed"``.
    """
    root = Path(root).resolve()
    output_path = output_path or root / ".dev-kit" / "trace" / "measurement" / "contract-test.out"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if run_id is None:
        run_id = os.environ.get("DEV_KIT_RUN_ID") or "contract-test"

    exit_code = _run_pytest(root, output_path)
    summary = _summary_from_output(output_path)
    outcome = "passed" if exit_code == 0 else "failed"
    _append_event(
        root,
        event_type="contract.test",
        subject_id=CONTRACT_TEST_SUBJECT_ID,
        run_id=run_id,
        outcome=outcome,
        evidence_ref={
            "via": "contract_test_emitter",
            "tests_passed": summary["tests_passed"],
            "summary_line": summary["summary_line"],
            "pytest_exit_code": exit_code,
            "targets": list(HERMETIC_TEST_TARGETS),
        },
    )
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emit one contract.test event")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    sys.exit(emit(args.root, run_id=args.run_id, output_path=args.output))
