"""session_monitor_render.py -- JSON + eval-handshake rendering.

Splits the ``print_json`` / ``build_eval_handshake`` pair out of
``tools/session_monitor.py``. The two functions are pure emitters: a
``--json`` consumer (the /dev-kit:session-monitor skill's
AskUserQuestion flow) and a contract advertisement for the
``--session-log`` judge in lib/eval_runner.py. Pulling them out keeps
``session_monitor.py`` focused on discovery + status + agent graph +
CLI.

Public surface (re-exported by ``tools/session_monitor.py`` so callers
keep using ``sm.print_json``, ``sm.EVAL_AXES``,
``sm.build_eval_handshake``):
- ``EVAL_AXES``
- ``build_eval_handshake``
- ``print_json``
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import skill_usage  # noqa: E402  (tools/ on sys.path from parent)
from session_monitor import (  # noqa: E402
    Status,
    WorktreeInfo,
    _per_worktree_top_skills,
    _rel_time,
)

EVAL_AXES: tuple = (
    "intent_alignment", "ambiguity_unresolved", "repeated_mistakes",
    "rule_adherence", "inefficiency", "structural_improvement",
    "over_engineering", "thoroughness",
)
"""The 8 axes the --session-log judge consumes (eval_runner.SESSION_AXES).

Mirrored here so tools/session_monitor.py can advertise the contract in
its JSON handshake without importing lib/ (which would pull in the LLM
judge deps). Keep in sync with eval/prompts/judge-session.md."""


def build_eval_handshake(model: list[WorktreeInfo]) -> Dict:
    """Per-session payload for the /dev-kit:eval `--session-log` judge.

    The monitor never calls the judge itself — it surfaces the contract
    (axes + per-session log path) so an external script can pipe
    ``--session-log <log_path>`` into ``lib/eval_runner.py`` for any
    session in the model. ``opt_in=True`` flags the user that the
    judge is never auto-invoked.
    """
    sessions: List[Dict] = []
    for w in model:
        for s in w.sessions:
            if not s.log_path:
                continue
            sessions.append({
                "session_id": s.session_id,
                "worktree": w.dirname,
                "source": s.source,
                "log_path": s.log_path,
                "judge_command": (
                    f"python3 lib/eval_runner.py --project-root . "
                    f"--session-log {s.log_path}"
                ),
                "axes": list(EVAL_AXES),
            })
    return {
        "opt_in": True,
        "axes": list(EVAL_AXES),
        "sessions": sessions,
        "notes": (
            "Pass --session-log <log_path> to lib/eval_runner.py to "
            "judge a session on the 8-axis rubric. NEVER auto-invoked; "
            "the monitor only emits this handshake."
        ),
    }


def print_json(model: list[WorktreeInfo], logs_dir: Path,
               *, skill_usage_agg: dict | None = None,
               skill_top_n: int = 5) -> None:
    """Machine-readable JSON for the skill-driven AskUserQuestion picker.

    Carries the full session_id, worktree abs path, and log path so the
    skill can synthesize the exact ``cd <wt> && claude --resume <sid>``
    command without re-running the tool. Stable shape: top-level keys
    ``logs_dir``, ``generated_at``, ``total_sessions``, ``live_sessions``,
    ``worktrees`` (list of worktree records with ``sessions`` list nested).

    The ``eval_handshake`` block carries the per-session log paths the
    ``--session-log`` judge consumes. The monitor only emits the
    handshake — it never invokes the LLM judge itself.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "logs_dir": str(logs_dir),
        "generated_at": now.isoformat(),
        "total_sessions": sum(len(w.sessions) for w in model),
        "live_sessions": sum(1 for w in model for s in w.sessions
                             if s.status is Status.LIVE),
        "worktrees": [
            {
                "name": w.dirname,
                "state": w.state,
                "path": str(w.path) if w.path else None,
                "last_commit_subject": w.last_commit_subject,
                "has_live": any(s.status is Status.LIVE for s in w.sessions),
                "skill_usage": _per_worktree_top_skills(
                    skill_usage.filter_by_cwd_prefix(
                        skill_usage_agg, str(w.path))
                    if (skill_usage_agg and w.path is not None) else {},
                    top_n=skill_top_n) or None,
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "source": s.source,
                        "branch": s.branch,
                        "model": s.model,
                        "status": s.status.value,
                        "last_ts": s.last_ts.isoformat() if s.last_ts else None,
                        "last_rel": _rel_time(s.last_ts, now),
                        "pids": list(s.pids),
                        "subagent_count": s.subagent_count,
                        "log_path": s.log_path,
                    }
                    for s in w.sessions
                ],
            }
            for w in model
        ],
        "skill_usage_total": (skill_usage_agg or {}),
        "eval_handshake": build_eval_handshake(model),
    }
    print(json.dumps(payload, indent=2, sort_keys=False))
