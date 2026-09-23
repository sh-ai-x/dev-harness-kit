"""Regression tests for the contract.test session hook (issue #663).

The hook feeds `measurement_integrity.stability.contract_test_pass_rate`.
Two things have to be true for it to work, and neither is self-evident
from reading the hook body:

1. It must live in a file pytest actually auto-loads. pytest registers
   hook implementations only from files named exactly ``conftest.py`` —
   a module named ``conftest_contract.py`` is never imported at all
   (it matches neither the conftest name nor ``python_files``), so the
   hook silently fires zero times and the submetric stays ``None``.
2. It must actually append a well-formed ``contract.test`` event.

These tests pin both.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_conftest_is_named_so_pytest_autoloads_it() -> None:
    """The session hook must live in ``tests/conftest.py``.

    pytest only auto-registers hooks from files named exactly
    ``conftest.py``. ``pytest.ini`` here is ``testpaths``-only (no
    ``addopts``, no ``-p``) and the repo defines no ``pytest_plugins``,
    so any other filename makes the hook dead code.
    """
    conftest = REPO_ROOT / "tests" / "conftest.py"
    assert conftest.is_file(), (
        "tests/conftest.py is missing — pytest will not load "
        "pytest_sessionfinish from any other filename"
    )
    assert "def pytest_sessionfinish" in conftest.read_text(encoding="utf-8")
    assert not (REPO_ROOT / "tests" / "conftest_contract.py").exists(), (
        "tests/conftest_contract.py is never loaded by pytest; it must "
        "not linger alongside tests/conftest.py"
    )


def test_session_hook_appends_contract_test_event(tmp_path: Path, monkeypatch) -> None:
    """Calling the hook emits exactly one valid ``contract.test`` event."""
    # Make `lib` importable explicitly so the subprocess CLI resolves
    # when cwd is a temp dir. Always-on default — no DEV_KIT_TRACE_LOCAL
    # opt-out, no CI gate.
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        import conftest as contract_hook
        contract_hook.pytest_sessionfinish(session=None, exitstatus=0)
    finally:
        sys.path.remove(str(REPO_ROOT / "tests"))

    events_path = tmp_path / ".dev-kit" / "trace" / "events.jsonl"
    assert events_path.is_file(), "hook did not create the trace log"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    contract_events = [e for e in events if e["event_type"] == "contract.test"]
    assert len(contract_events) == 1, events
    assert contract_events[0]["outcome"] == "passed"
    assert contract_events[0]["subject_id"] == "harness-contract"


def test_session_hook_respects_dev_kit_trace_local_opt_out(tmp_path: Path, monkeypatch) -> None:
    """``DEV_KIT_TRACE_LOCAL=0`` disables the hook for that invocation.

    The hook is always-on by default (PR #905 redesign: the previous
    ``CI=true`` gate was broken because CI ephemeral filesystems
    discarded the emitted events). Operators that want a clean trace
    log opt out per-invocation.
    """
    monkeypatch.setenv("DEV_KIT_TRACE_LOCAL", "0")
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        import conftest as contract_hook
        contract_hook.pytest_sessionfinish(session=None, exitstatus=0)
    finally:
        sys.path.remove(str(REPO_ROOT / "tests"))

    events_path = tmp_path / ".dev-kit" / "trace" / "events.jsonl"
    assert not events_path.exists(), (
        "hook must be a no-op when DEV_KIT_TRACE_LOCAL=0; it wrote "
        f"{events_path.read_text() if events_path.exists() else ''!r}"
    )


def test_session_hook_emits_by_default(tmp_path: Path, monkeypatch) -> None:
    """Without ``DEV_KIT_TRACE_LOCAL=0`` the hook fires — even on local runs.

    The always-on design is the only one that actually wires
    ``stability.contract_test_pass_rate`` because the reducer reads
    the user's local ``.dev-kit/trace/events.jsonl``, not the
    ephemeral CI one.
    """
    monkeypatch.delenv("DEV_KIT_TRACE_LOCAL", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        import conftest as contract_hook
        contract_hook.pytest_sessionfinish(session=None, exitstatus=0)
    finally:
        sys.path.remove(str(REPO_ROOT / "tests"))

    events_path = tmp_path / ".dev-kit" / "trace" / "events.jsonl"
    assert events_path.is_file(), "hook should emit by default on local pytest runs"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    contract_events = [e for e in events if e["event_type"] == "contract.test"]
    assert len(contract_events) == 1
    assert contract_events[0]["outcome"] == "passed"


def test_hook_never_raises_when_trace_log_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    """Telemetry is best-effort: a broken environment must not fail the run."""
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        import conftest as contract_hook
        monkeypatch.setattr(subprocess, "run", _boom)
        contract_hook.pytest_sessionfinish(session=None, exitstatus=1)
    finally:
        sys.path.remove(str(REPO_ROOT / "tests"))


def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
    raise OSError("simulated failure")
