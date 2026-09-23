"""Regression tests for tools.contract_test_emitter — daily cron contract.test producer.

The emitter reads hermetic test outcomes and emits one ``contract.test``
event with ``outcome=passed`` or ``failed``. The reducer's
``stability.contract_test_pass_rate`` waits for it (issue #663).

Three things must hold:

1. A passing test run emits exactly one ``contract.test`` event with
   ``outcome="passed"`` and ``subject_id="harness-contract"``.
2. A failing test run emits exactly one ``contract.test`` event with
   ``outcome="failed"``. The reducer's pass-rate numerator stays at
   zero until CI is green again, surfacing the breakage honestly.
3. The emitter never raises — pytest subprocess errors degrade to
   ``outcome="failed"`` rather than crashing CI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_events(root: Path) -> list:
    path = root / ".dev-kit" / "trace" / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_emitter_writes_passed_event_on_passing_pytest(tmp_path: Path, monkeypatch) -> None:
    """A stubbed pytest that returns exit 0 → outcome=passed."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT))
    import tools.contract_test_emitter as cte
    # Stub subprocess.run to return a fake pytest success.
    class FakeProc:
        returncode = 0
        stdout = "42 passed in 0.5s\n"
        stderr = ""
    def fake_run(*args, **kwargs):
        # Write the captured output to the requested output_path so
        # _summary_from_output can parse it.
        # The CLI prints nothing for pytest — _run_pytest captures into
        # the output file directly.
        return FakeProc()
    monkeypatch.setattr(cte.subprocess, "run", fake_run)

    rc = cte.emit(tmp_path, run_id="test-pass")
    assert rc == 0

    events = _read_events(tmp_path)
    contract_events = [e for e in events if e["event_type"] == "contract.test"]
    assert len(contract_events) == 1, f"expected exactly 1 contract.test, got {len(contract_events)}"
    assert contract_events[0]["outcome"] == "passed"
    assert contract_events[0]["subject_id"] == "harness-contract"
    assert contract_events[0]["evidence_ref"]["via"] == "contract_test_emitter"


def test_emitter_writes_failed_event_on_failing_pytest(tmp_path: Path, monkeypatch) -> None:
    """A stubbed pytest that returns exit 1 → outcome=failed."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT))
    import tools.contract_test_emitter as cte
    class FakeProc:
        returncode = 1
        stdout = "1 failed, 41 passed in 0.5s\n"
        stderr = ""
    monkeypatch.setattr(cte.subprocess, "run", lambda *a, **kw: FakeProc())

    rc = cte.emit(tmp_path, run_id="test-fail")
    assert rc == 1

    events = _read_events(tmp_path)
    contract_events = [e for e in events if e["event_type"] == "contract.test"]
    assert len(contract_events) == 1
    assert contract_events[0]["outcome"] == "failed"


def test_emitter_swallows_subprocess_exceptions(tmp_path: Path, monkeypatch) -> None:
    """A subprocess crash must not raise; outcome degrades to failed."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT))
    import tools.contract_test_emitter as cte
    def boom(*args, **kwargs):
        raise OSError("simulated subprocess crash")
    monkeypatch.setattr(cte.subprocess, "run", boom)

    # Must NOT raise — telemetry is best-effort.
    rc = cte.emit(tmp_path, run_id="test-boom")
    assert rc == 1

    events = _read_events(tmp_path)
    contract_events = [e for e in events if e["event_type"] == "contract.test"]
    assert len(contract_events) == 1
    assert contract_events[0]["outcome"] == "failed"


def test_emitter_uses_fixed_subject_id(tmp_path: Path, monkeypatch) -> None:
    """The contract.test event must use subject_id=harness-contract.

    tests/test_harness_stability.py:338 already pins this as the
    canonical fixture id; verify the emitter honors it so the
    stability submetric's contract_test_pass_rate picks up the events.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT))
    import tools.contract_test_emitter as cte
    class FakeProc:
        returncode = 0
        stdout = "10 passed\n"
        stderr = ""
    monkeypatch.setattr(cte.subprocess, "run", lambda *a, **kw: FakeProc())

    cte.emit(tmp_path, run_id="test-subject")

    events = _read_events(tmp_path)
    contract_events = [e for e in events if e["event_type"] == "contract.test"]
    assert contract_events[0]["subject_id"] == "harness-contract", (
        f"contract.test events must use subject_id='harness-contract' for the "
        f"stability contract_test_pass_rate to pick them up; got "
        f"{contract_events[0]['subject_id']!r}"
    )
