"""tests/conftest.py — emit one ``contract.test`` event per CI pytest run.

This conftest hooks ``pytest_sessionfinish`` and appends one structured
event to ``.dev-kit/trace/events.jsonl`` so the harness stability
submetric's ``contract_test_pass_rate`` signal is wired automatically
in CI. Without this hook, every CI run reports
``INSUFFICIENT_EVIDENCE`` for the contract pass rate, which masks
real regressions.

The filename matters: pytest auto-registers hook implementations only
from files named exactly ``conftest.py``. ``pytest.ini`` here is
``testpaths``-only (no ``addopts``, no ``-p``) and the repo defines no
``pytest_plugins``, so naming this module anything else makes the hook
dead code that never fires. See ``tests/test_conftest_contract.py``.

CI-gated: a local ``pytest`` run would otherwise append an event into
the developer's own trace log on every invocation, inflating the
trajectory the reducer scores. Best-effort: the hook runs in a
try/except so a missing ``lib`` module or a non-git root never breaks
the test run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ANN001
    """Append one contract.test event after a CI session ends."""
    if os.environ.get("CI") != "true":
        # Local run — do not pollute the developer's trace log.
        return
    try:
        cwd = Path(os.getcwd())
        outcome = "passed" if exitstatus == 0 else "failed"
        run_id = os.environ.get("DEV_KIT_RUN_ID") or f"pytest-{os.getpid()}"
        python = sys.executable or shutil.which("python3") or "python3"
        env = os.environ.copy()
        env.setdefault("DEV_KIT_AGENT", "pytest")
        cmd = [
            python, "-m", "lib.trace_log", "append-event",
            "--type", "contract.test",
            "--subject-id", "harness-contract",
            "--run-id", run_id,
            "--workflow-id", "contract",
            "--stage", "contract",
            "--outcome", outcome,
            "--root", str(cwd),
        ]
        # Best-effort: never let telemetry break the test run.
        subprocess.run(cmd, env=env, cwd=str(cwd), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:
        # Telemetry is best-effort — swallow everything.
        pass
