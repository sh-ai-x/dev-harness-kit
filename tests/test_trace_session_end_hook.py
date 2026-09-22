#!/usr/bin/env python3
"""test_trace_session_end_hook.py — regression tests for hooks/trace-session-end.sh.

Verifies the bash-level behavior of the session-scoped `step.completed`
emitter registered on both Stop and SessionEnd (issue #702). Two things
under test:

  1. `evidence_ref.hook_event` reflects the actual trigger (Stop vs
     SessionEnd) instead of the hardcoded "SessionEnd" string an earlier
     revision wrote regardless of which hook fired.
  2. The `agent` field on the persisted event is stamped from
     `DEV_KIT_AGENT` when the calling environment sets it (the hooks.json
     command-line convention added alongside this fix).

We test the script as a black box: feed it JSON via stdin, let it append
to a real `.dev-kit/trace/events.jsonl` under a temp root, and assert on
the persisted record. No mocks.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS = REPO_ROOT / "hooks"


def _run_hook(payload: dict, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the hook with the bash process's cwd at REPO_ROOT.

    `python3 -m lib.trace_log` resolves the `lib` package via the
    interpreter's cwd-on-sys.path default, not the payload's `.cwd`
    field (that field only controls where `EFFECTIVE_CWD`, and thus
    the target `.dev-kit/trace/events.jsonl`, resolves to). Mirrors
    the real dogfooding deployment: dev-kit's own hooks.json invokes
    this script with the process cwd already at the dev-kit repo.
    """
    import os

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOKS / "trace-session-end.sh")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _last_event(root: Path) -> dict:
    events_path = root / ".dev-kit" / "trace" / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


class TestTraceSessionEndHookEvent(unittest.TestCase):
    def test_hook_event_reflects_stop_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = {"session_id": "sess-stop-1", "cwd": str(root), "hook_event_name": "Stop"}
            proc = _run_hook(payload)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            event = _last_event(root)
            self.assertEqual(event["evidence_ref"]["hook_event"], "Stop")

    def test_hook_event_reflects_session_end_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = {"session_id": "sess-end-1", "cwd": str(root), "hook_event_name": "SessionEnd"}
            proc = _run_hook(payload)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            event = _last_event(root)
            self.assertEqual(event["evidence_ref"]["hook_event"], "SessionEnd")

    def test_hook_event_accepts_camel_case_payload_field(self):
        """Some runtimes may send `hookEventName` instead of
        `hook_event_name` — the session_id read two lines above already
        falls back `.session_id // .sessionId`; the hook_event read must
        mirror that same snake/camel fallback instead of silently
        collapsing to `"unknown"`.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = {"session_id": "sess-camel-1", "cwd": str(root), "hookEventName": "Stop"}
            proc = _run_hook(payload)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            event = _last_event(root)
            self.assertEqual(event["evidence_ref"]["hook_event"], "Stop")

    def test_agent_env_is_stamped_on_persisted_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = {"session_id": "sess-agent-1", "cwd": str(root), "hook_event_name": "Stop"}
            proc = _run_hook(payload, env_extra={"DEV_KIT_AGENT": "claude-code"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            event = _last_event(root)
            self.assertEqual(event.get("agent"), "claude-code")

    def test_ralph_session_end_is_worker_closure_not_completed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / ".dev-kit" / "ralph"
            state_dir.mkdir(parents=True)
            (state_dir / "ralph.json").write_text(
                json.dumps({
                    "session": "ralph",
                    "current_stage": "ATTENDED_RUN",
                    "attended_lock": True,
                }),
                encoding="utf-8",
            )
            payload = {
                "session_id": "runtime-session",
                "cwd": str(root),
                "hook_event_name": "SessionEnd",
            }
            proc = _run_hook(
                payload,
                env_extra={"RALPH_MODE": "1", "RALPH_SESSION": "ralph"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            checkpoints = root / ".dev-kit" / "ralph" / "ralph.checkpoints.jsonl"
            record = json.loads(checkpoints.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["event_type"], "ralph.worker.session_closed")
            self.assertEqual(record["outcome"], "cancelled")
            events = root / ".dev-kit" / "trace" / "events.jsonl"
            event = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
            self.assertFalse(event["evidence_ref"]["workflow_completed"])

    def test_ralph_stop_verify_records_checkpoint_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / ".dev-kit" / "ralph"
            state_dir.mkdir(parents=True)
            (state_dir / "ralph.json").write_text(
                json.dumps({
                    "session": "ralph",
                    "current_stage": "ATTENDED_RUN",
                    "attended_lock": True,
                }),
                encoding="utf-8",
            )
            payload = {
                "cwd": str(root),
                "hook_event_name": "Stop",
                "last_assistant_message": "done",
            }
            proc = subprocess.run(
                ["bash", str(HOOKS / "stop-verify.sh")],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(REPO_ROOT),
                env={**__import__("os").environ, "RALPH_MODE": "1", "RALPH_SESSION": "ralph"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            checkpoints = root / ".dev-kit" / "ralph" / "ralph.checkpoints.jsonl"
            record = json.loads(checkpoints.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["event_type"], "ralph.worker.checkpointed")


if __name__ == "__main__":
    unittest.main()
