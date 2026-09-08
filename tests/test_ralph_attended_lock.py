"""Regression suite for /dev-kit:ralph attended-lock PreToolUse hook.

Pins the contract that ``hooks/ralph-attended-lock.sh`` exits 2 with a
deny JSON when the canonical ralph state reports
``attended_lock=True`` or ``current_stage=ATTENDED_RUN``, and exits 0
otherwise.

The hook reads stdin for the PreToolUse payload (matching all other
dev-kit hooks) and inspects ``.dev-kit/ralph/<session>.json`` via the
canonical ``ralph_state`` module. Tests construct a real state file
on disk so the hook's PYTHONPATH-based import path is exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "ralph-attended-lock.sh"
SKILL_LIB = ROOT / "skills" / "ralph" / "lib"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".dev-kit").mkdir()
    # Make ralph_state importable from the hook via PYTHONPATH.
    return tmp_path


@pytest.fixture()
def session_file(project_root: Path) -> Path:
    return project_root / ".dev-kit" / "ralph" / "default.json"


def _run_hook(
    payload: str,
    project_root: Path,
    *,
    session: str = "default",
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env["PYTHONPATH"] = f"{SKILL_LIB}:{env.get('PYTHONPATH', '')}"
    if session != "default":
        env["RALPH_SESSION"] = session
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=str(project_root),
    )


def _write_state(project_root: Path, *, attended_lock: bool, current_stage: str) -> Path:
    """Construct a real RalphState on disk via the canonical module."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(SKILL_LIB))
    import ralph_state as rs  # type: ignore  # noqa: E402

    state = rs.RalphState(
        session="default",
        current_stage=current_stage,
        attended_lock=attended_lock,
    )
    state.save(project_root)
    return project_root / ".dev-kit" / "ralph" / "default.json"


PROBE_PAYLOAD = json.dumps(
    {
        "session_id": "test",
        "transcript_path": "/tmp/x",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "approve?", "options": []}]},
    }
)


# ============================================================================
# 1. Pass-through paths
# ============================================================================


def test_empty_payload_passes_through(project_root: Path):
    result = _run_hook("", project_root)
    assert result.returncode == 0
    assert result.stderr == ""


def test_no_state_file_passes_through(project_root: Path):
    """No active ralph session → rule does not apply → exit 0."""
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 0
    assert result.stderr == ""


def test_research_gate_passes_through(project_root: Path):
    _write_state(project_root, attended_lock=False, current_stage="RESEARCH_GATE")
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 0


def test_plan_gate_passes_through(project_root: Path):
    _write_state(project_root, attended_lock=False, current_stage="PLAN_GATE")
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 0


def test_ship_confirm_gate_without_lock_passes_through(project_root: Path):
    _write_state(
        project_root,
        attended_lock=False,
        current_stage="SHIP_CONFIRM_GATE",
    )
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 0


# ============================================================================
# 2. Deny paths — lock set, or in ATTENDED_RUN
# ============================================================================


def test_attended_run_with_lock_denies(project_root: Path):
    _write_state(project_root, attended_lock=True, current_stage="ATTENDED_RUN")
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 2
    assert "permissionDecision" in result.stderr
    assert "deny" in result.stderr
    assert "ATTENDED_RUN" in result.stderr or "attended_lock" in result.stderr


def test_lock_set_on_done_terminal_still_denies(project_root: Path):
    """Defence-in-depth: a stale DONE state with attended_lock=True
    must still deny — the lock is the one-way wall.

    Like ``test_lock_alone_is_sufficient_to_deny``, this constructs an
    impossible state (``DONE + attended_lock=True``). ``transition()``
    never lands there. The test pins that the hook's deny logic is
    keyed off ``attended_lock`` first and stage second, regardless of
    state-machine reachability.
    """
    _write_state(project_root, attended_lock=True, current_stage="DONE")
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 2


def test_lock_alone_is_sufficient_to_deny(project_root: Path):
    """Edge case: lock is set but stage was reset. The lock alone is
    enough to deny — it is the one-way boundary, not the stage.

    Production code never produces ``RESEARCH_GATE + attended_lock=True``:
    ``RalphState.transition()`` only sets the lock on the
    ``SHIP_CONFIRM_GATE → ATTENDED_RUN`` edge. The test still exercises
    the hook's contract: the ``LOCK=true`` branch trips before the
    stage check matters.
    """
    _write_state(
        project_root,
        attended_lock=True,
        current_stage="RESEARCH_GATE",
    )
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 2


# ============================================================================
# 3. Toolchain-missing — fail-OPEN with stderr warning
# ============================================================================


def _build_fake_bin(parent: Path, names: list[str]) -> Path:
    """Build a fake binary directory inside ``parent`` with symlinks to
    the host binaries named in ``names``. Symlinks dodge macOS
    Gatekeeper quarantine that SIGKILLs copied binaries. Returns the
    path to the fake dir."""
    fake = parent / "fake-bin"
    fake.mkdir(exist_ok=True)
    for name in names:
        src = None
        for cand in (Path("/bin") / name, Path("/usr/bin") / name):
            if cand.exists():
                src = cand
                break
        if src is None:
            continue
        dst = fake / name
        if not dst.exists():
            try:
                dst.symlink_to(src)
            except OSError:
                continue
    return fake


def test_jq_missing_fails_open(project_root: Path, monkeypatch):
    _write_state(project_root, attended_lock=True, current_stage="ATTENDED_RUN")
    # PATH has bash + env + cat + python3 (symlinked) but NO jq. The
    # hook's `command -v jq` should fail, and the hook should exit 0
    # (fail-OPEN) with a jq WARN on stderr.
    fake = _build_fake_bin(project_root, ["bash", "env", "cat", "python3"])
    stripped = {k: v for k, v in os.environ.items() if k != "PATH"}
    stripped["PATH"] = str(fake)
    for key in ("JQ", "JQBIN"):
        stripped.pop(key, None)
    result = _run_hook(PROBE_PAYLOAD, project_root, extra_env=stripped)
    assert result.returncode == 0  # fail-OPEN
    assert "jq" in result.stderr.lower()


def test_python3_missing_fails_open(project_root: Path, monkeypatch):
    _write_state(project_root, attended_lock=True, current_stage="ATTENDED_RUN")
    # PATH has bash + env + cat + jq (symlinked) but NO python3. The
    # hook's `command -v python3` should fail, and the hook should exit
    # 0 (fail-OPEN) with a python3 WARN on stderr.
    fake = _build_fake_bin(project_root, ["bash", "env", "cat", "jq"])
    stripped = {k: v for k, v in os.environ.items() if k != "PATH"}
    stripped["PATH"] = str(fake)
    result = _run_hook(PROBE_PAYLOAD, project_root, extra_env=stripped)
    assert result.returncode == 0  # fail-OPEN
    assert "python3" in result.stderr.lower()


# ============================================================================
# 4. Hook wiring in hooks.json — matchers + command path
# ============================================================================


def test_hook_registered_for_askuserquestion_in_hooks_json():
    """The matcher `AskUserQuestion` is wired with this hook. This
    regression guards against an accidental drop during a hook-matrix
    refactor."""
    import re

    hooks_json = (ROOT / "hooks" / "hooks.json").read_text()
    # Find the AskUserQuestion matcher block.
    pattern = re.compile(
        r'"matcher"\s*:\s*"AskUserQuestion".*?"hooks"\s*:\s*\[(.*?)\]',
        re.DOTALL,
    )
    match = pattern.search(hooks_json)
    assert match is not None, "AskUserQuestion matcher block missing from hooks.json"
    assert "ralph-attended-lock.sh" in match.group(1)


def test_askuserquestion_matcher_is_under_pretooluse_not_posttooluse():
    """CRITICAL — the hook is a PreToolUse denial gate, not a
    PostToolUse advisory. Per `hooks/injection-content-guard.sh:14-17`:
    PostToolUse hooks cannot block — `permissionDecision` is decorative.
    The matcher MUST live in the PreToolUse array.

    Regression guard for the PR #828 review finding F1 (CRITICAL):
    a prior version inserted the matcher under PostToolUse, which
    silently no-op'd the entire mechanical layer.
    """
    import json

    for hooks_path in (
        ROOT / "hooks" / "hooks.json",
        ROOT / ".codex-plugin" / "hooks" / "hooks.json",
    ):
        data = json.loads(hooks_path.read_text())
        pre_blocks = data["hooks"].get("PreToolUse", [])
        post_blocks = data["hooks"].get("PostToolUse", [])
        pre_matchers = [b.get("matcher") for b in pre_blocks]
        post_matchers = [b.get("matcher") for b in post_blocks]
        assert "AskUserQuestion" in pre_matchers, (
            f"{hooks_path}: AskUserQuestion matcher missing from PreToolUse. "
            "PostToolUse cannot block — the hook would silently no-op."
        )
        assert "AskUserQuestion" not in post_matchers, (
            f"{hooks_path}: AskUserQuestion matcher is under PostToolUse. "
            "Move to PreToolUse so the deny envelope is honoured."
        )


# ============================================================================
# 5. Multi-session — RALPH_SESSION env var honoured
# ============================================================================


def test_alt_session_lock_denies(project_root: Path):
    """A different session name (RALPH_SESSION=foo) with its own lock
    must still deny. Tests the env-var path through the hook."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(SKILL_LIB))
    import ralph_state as rs  # type: ignore  # noqa: E402

    state = rs.RalphState(
        session="foo",
        current_stage="ATTENDED_RUN",
        attended_lock=True,
    )
    state.save(project_root)

    result = _run_hook(PROBE_PAYLOAD, project_root, session="foo")
    assert result.returncode == 2
    assert "session=foo" in result.stderr


def test_alt_session_unlocked_passes(project_root: Path):
    """Default session locked, alt session unlocked → alt session
    passes. The hook operates on the named session only."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(SKILL_LIB))
    import ralph_state as rs  # type: ignore  # noqa: E402

    rs.RalphState(
        session="default",
        current_stage="ATTENDED_RUN",
        attended_lock=True,
    ).save(project_root)
    rs.RalphState(
        session="foo",
        current_stage="RESEARCH_GATE",
        attended_lock=False,
    ).save(project_root)

    result = _run_hook(PROBE_PAYLOAD, project_root, session="foo")
    assert result.returncode == 0


# ============================================================================
# 6. Smoke — the deny JSON envelope is well-formed
# ============================================================================


def test_deny_envelope_is_valid_json(project_root: Path):
    _write_state(project_root, attended_lock=True, current_stage="ATTENDED_RUN")
    result = _run_hook(PROBE_PAYLOAD, project_root)
    assert result.returncode == 2
    # The hook writes the envelope to stderr; the host CLI strips
    # the leading line and feeds it to the model. We pull the JSON
    # object out of the stderr stream.
    envelope = None
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            envelope = json.loads(line)
            break
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ATTENDED_LOCK" in envelope["hookSpecificOutput"]["permissionDecisionReason"]
