#!/usr/bin/env python3
"""
execute.py — harness-runner engine (per step executor).

Adapted from harness_framework/scripts/execute.py (sh-ai-x/harness_framework) — plan §5.
Adds:
- 2-commit protocol (feat + chore)
- atomic step<N>-output.json
- status state-machine:
    unimplemented → pending → in_progress → completed
                                       ↘ error  → pending (resume)
                                       ↘ blocked → pending (human unblock)
    completed → pending (manual reset)
- per-step timing: started_at set on in_progress; completed_at + duration_seconds on completed
- dispatch mode auto-classified via lib.dispatch_classifier (parallel/sequential decision logged as the first build line)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic import atomic_write_json, now_iso  # noqa: E402
from dispatch_classifier import classify  # noqa: E402 — top-level (no cycle)
from git_worktree import cut_worktree  # noqa: E402 — canonical helper (issue #310)
from trace_log import append_event  # noqa: E402 — additive effectiveness evidence

SCHEMA_VERSION = "1.0.0"
# Sub-agent stdout marker. If the per-step `claude -p` emits this line, the
# runner transitions the step to `blocked` (with reason) instead of `completed`
# so the human gets unblocked instead of a silent zero-file PR (issue #221).
BLOCKED_MARKER = "<!-- status: blocked -->"
# Tools the per-step sub-agent needs to do anything useful. Required so a
# restrictive parent Claude Code sandbox (issue #221 RC1: consumer project
# does not pre-allow .worktrees/**) does not silently block all writes.
SUBAGENT_ALLOWED_TOOLS = "Write,Edit,Bash"
DEFAULT_AGENT_TIMEOUT_SECONDS = 3600


def _agent_timeout_seconds() -> int:
    """Return a bounded per-step timeout for unattended builds."""
    raw = os.environ.get("DEV_KIT_AGENT_TIMEOUT_SECONDS", str(DEFAULT_AGENT_TIMEOUT_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_AGENT_TIMEOUT_SECONDS
    return max(60, min(value, 24 * 60 * 60))


def _output_text(value: object) -> str:
    """Normalize subprocess output, including bytes from timeout exceptions."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value or "")


def _agent_command(worktree: Path, prompt: str) -> list[str]:
    """Build the non-interactive command for the selected agent runtime.

    Claude remains the default for compatibility. Codex is selected with
    ``DEV_KIT_BUILD_AGENT=codex`` and receives the same worktree + prompt.
    """
    agent = os.environ.get("DEV_KIT_BUILD_AGENT", "claude").strip().lower()
    if agent == "claude":
        return [
            "claude", "-p", "--add-dir", str(worktree),
            "--allowedTools", SUBAGENT_ALLOWED_TOOLS,
            "--workdir", str(worktree), prompt,
        ]
    if agent == "codex":
        return [
            "codex", "exec", "--cd", str(worktree),
            "--add-dir", str(worktree), prompt,
        ]
    raise ValueError(f"unsupported DEV_KIT_BUILD_AGENT={agent!r}; use claude or codex")
# Step lifecycle. Order roughly matches the typical progression; entries are
# enforced by update_step_status() and indexed/queried by tests/CLI.
VALID_STATUSES = (
    "unimplemented",  # step.md not yet written; stub registered in index.json
    "pending",        # step.md written, runner hasn't started
    "in_progress",    # runner executing this step
    "completed",      # finished successfully
    "error",          # execution failed; resume by transitioning to pending
    "blocked",        # user intervention required; unblock by transitioning to pending
)
# Statuses from which the runner can RESUME a step (i.e. start it now).
RESUMABLE_STATUSES = ("pending", "error", "in_progress")
# Statuses the runner SKIPS without doing anything.
SKIPPABLE_STATUSES = ("completed", "unimplemented")

# Upper bound on concurrent sub-agents in _run_parallel. The auto-classifier
# defaults to sequential and only opens the parallel gate for N >= 4
# eligible steps, but does NOT cap the upper end. A 50-step phase that
# clears the parallel gate would otherwise fork 50 concurrent `claude -p`
# subprocesses, each holding its own worktree + Popen — fork-bomb risk
# on small CI runners. 8 was chosen as a balance: large enough to
# parallelize typical multi-file work, small enough to bound the OS
# process / file-descriptor / memory footprint on a 4-vCPU runner.
_PARALLEL_MAX_CONCURRENT = 8


def _emit_effectiveness_event(
    root: Path,
    phase: str,
    step_num: int,
    event_type: str,
    outcome: str,
    evidence_ref: dict,
    parent_id: Optional[str] = None,
) -> Optional[str]:
    """Best-effort append-only event emission for the existing runner lifecycle.

    Evidence logging must never change the build result.  A stable subject id
    lets the reducer join start/write/verify/repair records across retries.
    """
    run_id = os.environ.get("DEV_KIT_RUN_ID", f"build-{phase}")
    workflow_id = os.environ.get("DEV_KIT_WORKFLOW_ID", f"execute:{phase}")
    subject_id = f"{phase}:step:{step_num}"
    ts = now_iso()
    event_id = f"{run_id}:{subject_id}:{event_type}:{ts}"
    event = {
        "schema_version": 1,
        "event_id": event_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "stage": "execute",
        "event_type": event_type,
        "subject_id": subject_id,
        "parent_id": parent_id,
        "ts": ts,
        "outcome": outcome,
        "source": "lib.execute",
        "evidence_ref": evidence_ref,
    }
    try:
        append_event(root, event)
        return event_id
    except (OSError, ValueError):
        return None


# ---------- Phase / Step readers ----------

def parse_step_index(idx_path: Path) -> List[Dict]:
    """Parse phases/<phase>/index.json into the steps list."""
    return json.loads(idx_path.read_text(encoding="utf-8")).get("steps", [])


def read_step(project_root: Path, phase: str, step: int) -> str:
    """Read phases/<phase>/step<N>.md prompt verbatim."""
    path = project_root / "phases" / phase / f"step{step}.md"
    if not path.exists():
        raise FileNotFoundError(f"step file not found: {path}")
    return path.read_text(encoding="utf-8")


def register_step(
    project_root: Path,
    phase: str,
    step: int,
    name: str,
) -> None:
    """Register a step in phases/<phase>/index.json as `unimplemented`.

    Idempotent: if a stub for this step number already exists, this is a no-op.
    Used by the plan skill to pre-register step counts BEFORE writing step<N>.md —
    gives external observers visibility into "this phase plans N steps, K of which
    are written so far".

    The `unimplemented` status is in SKIPPABLE_STATUSES, so the runner ignores it.
    Once plan writes step<N>.md, it transitions the stub to `pending` via
    update_step_status() and the runner picks it up on the next run.
    """
    idx_path = project_root / "phases" / phase / "index.json"
    if idx_path.exists():
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    else:
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": SCHEMA_VERSION, "phase": phase, "steps": []}
    for s in data.get("steps", []):
        if s.get("step") == step:
            return  # already registered — preserve any user-set fields
    data.setdefault("steps", []).append({
        "step": step,
        "name": name,
        "status": "unimplemented",
    })
    atomic_write_json(idx_path, data)


# ---------- Step status state machine ----------

def _transition_in_progress(step: dict, now: str, **_kwargs) -> None:
    """Idempotent started_at stamp (issue #94 transition table)."""
    if "started_at" not in step:
        step["started_at"] = now


def _transition_completed(step: dict, now: str, *, duration_seconds: Optional[float] = None, **_kwargs) -> None:
    """Set completed_at + duration_seconds; clear error/blocked fields."""
    step["completed_at"] = now
    if duration_seconds is None and "started_at" in step:
        try:
            started = datetime.fromisoformat(step["started_at"])
            finished = datetime.fromisoformat(now)
            duration_seconds = max(0.0, (finished - started).total_seconds())
        except Exception:
            duration_seconds = None
    if duration_seconds is not None:
        step["duration_seconds"] = float(duration_seconds)
    step.pop("error_message", None)
    step.pop("blocked_reason", None)
    step.pop("failed_at", None)


def _transition_error(step: dict, now: str, *, error_message: Optional[str] = None, **_kwargs) -> None:
    step["failed_at"] = now
    step["error_message"] = error_message


def _transition_blocked(step: dict, now: str, *, blocked_reason: Optional[str] = None, **_kwargs) -> None:
    step["blocked_at"] = now
    step["blocked_reason"] = blocked_reason


def _transition_reset(step: dict, _now: str, **_kwargs) -> None:
    """Resume-retry / stub-registration: clear all timestamps."""
    _clear_step_timestamps(step)


# Status transition table (issue #94). Adding a new status = adding one
# entry here + one entry in VALID_STATUSES. No more forgetting a
# `s.pop("started_at", None)` line in one of 5 branches.
STATUS_TRANSITIONS: Dict[str, Callable[..., None]] = {
    "in_progress": _transition_in_progress,
    "completed": _transition_completed,
    "error": _transition_error,
    "blocked": _transition_blocked,
    "pending": _transition_reset,
    "unimplemented": _transition_reset,
}


def update_step_status(
    project_root: Path,
    phase: str,
    step: int,
    status: str,
    error_message: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> None:
    """Update a single step's status with validation + atomic write.

    Side effects route through `STATUS_TRANSITIONS` (issue #94): each status
    has a single transition function owning its timestamp + field logic.
    Args:
        status: one of VALID_STATUSES.
        duration_seconds: optional wall-clock duration; when transitioning
            to "completed" and not provided, computed from started_at.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}. Valid: {VALID_STATUSES}")
    if status == "blocked" and not blocked_reason:
        raise ValueError("status 'blocked' requires blocked_reason")
    if status == "error" and not error_message:
        raise ValueError("status 'error' requires error_message")

    idx_path = project_root / "phases" / phase / "index.json"
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    for s in data["steps"]:
        if s["step"] == step:
            s["status"] = status
            STATUS_TRANSITIONS[status](
                s, now_iso(),
                error_message=error_message,
                blocked_reason=blocked_reason,
                duration_seconds=duration_seconds,
            )
            break
    else:
        raise ValueError(f"step {step} not found in {phase}")
    atomic_write_json(idx_path, data)


# ---------- Step output writer ----------

def _clear_step_timestamps(step: dict) -> None:
    """Pop all per-step timestamp + error/blocked fields.

    Shared between the `pending` (resume retry) and `unimplemented`
    (stub registration) branches of `update_step_status` so adding a new
    timestamp field lands in one place instead of two.
    """
    for key in (
        "completed_at", "failed_at", "blocked_at",
        "error_message", "blocked_reason",
        "started_at", "duration_seconds",
    ):
        step.pop(key, None)


def write_step_output(
    project_root: Path,
    phase: str,
    step: int,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_seconds: float = 0.0,
    blocked: bool = False,
    blocked_reason: Optional[str] = None,
) -> Path:
    """Atomic write phases/<phase>/step<N>-output.json."""
    path = project_root / "phases" / phase / f"step{step}-output.json"
    data = {
        "schema_version": SCHEMA_VERSION,
        "step": step,
        "phase": phase,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": duration_seconds,
        "timestamp": now_iso(),
    }
    if blocked:
        # Issue #221 RC3: surface the sub-agent's blocked verdict in the
        # output JSON so audits can see WHY a step was held back instead of
        # silently advancing to `completed` on exit_code==0.
        data["blocked"] = True
        data["blocked_reason"] = blocked_reason or ""
    atomic_write_json(path, data)
    return path


def _extract_blocked_reason(stdout: str) -> Optional[str]:
    """If BLOCKED_MARKER is in stdout, return the human-readable reason.

    The convention is: the line(s) immediately BEFORE the marker are the
    human-readable request (e.g. "i need an API key — cannot proceed"). After
    the marker is meaningless chatter. We strip the marker itself and any
    trailing content so the reason recorded in index.json is concise.
    """
    if BLOCKED_MARKER not in stdout:
        return None
    head, _, _ = stdout.partition(BLOCKED_MARKER)
    reason = head.strip().rstrip(",").strip()
    return reason or "sub-agent emitted <!-- status: blocked --> with no preceding reason"


def _commit_step(wt: Path, msg: str) -> bool:
    """Stage ALL writes in the per-step worktree, then commit only if dirty.

    Issue #221 RC2: the previous `git commit --allow-empty` masked a chain of
    failure modes — sub-agent writes blocked by sandbox, empty WorkTree, etc.
    The new contract is: stage first (`git add -A`), then ask git whether there
    is anything to commit (`git diff --cached --quiet`). If no diff, skip the
    commit entirely and return False. Caller branches on the bool to set the
    correct status (committed → continue; no-diff → block-on-marker-only is
    enough; or surface as a step-level "no files written" anomaly).
    """
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(wt), check=True, capture_output=True, text=True,
    )
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(wt), capture_output=True, text=True,
    )
    if diff_check.returncode == 0:
        # Nothing staged → nothing to commit. Do NOT make an empty commit.
        return False
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(wt), check=True, capture_output=True, text=True,
    )
    return True


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="dev-harness-kit harness-runner")
    parser.add_argument("phase", help="phase alias (e.g., 0-mvp)")
    parser.add_argument("--project-root", default=".", help="project root directory")
    parser.add_argument("--push", action="store_true", help="git push after each step")
    parser.add_argument("--skip-blocked", action="store_true",
                        help="continue past steps with status='blocked' instead of bailing; "
                             "skipped steps are listed in .dev-kit/hand-off/build→review.md")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    idx_path = root / "phases" / args.phase / "index.json"
    steps = parse_step_index(idx_path)
    # Filter to the same eligible-step projection the runners use. The
    # classifier must see only steps that will actually run; counting
    # SKIPPABLE_STATUSES (completed/unimplemented) would inflate N past
    # the parallel threshold and log a decision that doesn't match what
    # executes.
    eligible = [s for s in steps if s.get("status") in RESUMABLE_STATUSES
                and s.get("status") != "blocked"]
    # Auto-classify dispatch mode via lib.dispatch_classifier. Replaces the
    # legacy --parallel flag. Decision + reason logged as the first build
    # line so the user can audit why parallelism was rejected.
    decision = classify(eligible)
    print(f"dispatch: {decision.mode} — {decision.reason}", file=sys.stderr)
    if decision.mode == "parallel":
        return _run_parallel(root, args.phase, len(eligible), args.push, args.skip_blocked)
    return _run_sequential(root, args.phase, args.push, args.skip_blocked)


def _intent_integrity_pre_build_gate(root: Path, phase: str) -> int:
    """Pre-build intent-integrity gate. Single source of truth shared by
    `_run_sequential` and `_run_parallel`.

    Behavior split:
      - report missing         -> soft: warn + continue (return 0)
      - report present + high  -> hard: print + return 2, do NOT mutate state
      - report present + low   -> proceed (return 0)
      - invalid `phase` arg    -> hard: print + return 2

    Returns 0 if the build may proceed, 2 if it must stop.
    Read-only: no update_step_status() before this function returns 2.
    """
    import re as _re
    # Path-traversal guard: a malicious `phase` value (e.g. "../../tmp/x")
    # would escape `.dev-kit/integrity/` and either substitute attacker-
    # controlled JSON or fall through to the "missing → continue" branch.
    if not _re.fullmatch(r"[A-Za-z0-9._-]+", phase):
        print(
            f"intent_integrity: refusing to run gate — invalid phase {phase!r} "
            f"(use letters/digits/._-)",
            file=sys.stderr,
        )
        return 2

    pre_report = root / ".dev-kit" / "integrity" / f"{phase}.pre.json"
    if not pre_report.exists():
        print(
            f"intent_integrity: pre-build report missing at {pre_report} — "
            f"continuing without the gate (plan did not run integrity).",
            file=sys.stderr,
        )
        return 0

    try:
        pre_data = json.loads(pre_report.read_text(encoding="utf-8"))
        high = [f for f in pre_data.get("findings", []) if f.get("severity") == "high"]
    except (OSError, json.JSONDecodeError):
        print(
            f"intent_integrity: pre-build report at {pre_report} is unreadable "
            f"— continuing without the gate (corrupt JSON).",
            file=sys.stderr,
        )
        return 0

    if not high:
        return 0

    print(
        f"intent_integrity: {len(high)} high-severity finding(s) in "
        f"{pre_report} — refusing to start build:",
        file=sys.stderr,
    )
    for f in high:
        print(
            f"  [{f.get('finding_id', '?')}/{f.get('category', '?')}] "
            f"{f.get('evidence', '')}",
            file=sys.stderr,
        )
    return 2


def _run_sequential(root: Path, phase: str, push: bool, skip_blocked: bool = False) -> int:
    """Per-step: read → preamble → invoke claude CLI → write output → commit (feat + chore).

    Honors MUST-36 (one sub-agent per step), MUST-37 (3-cycle self-fix, declared in preamble),
    MUST-38 (per-step worktree). The step branch derives from `index.json["worktree"]`; if
    absent, falls back to `feat/<phase>`.

    Returns 0 on success, 2 on `blocked` (unless `skip_blocked=True`), or the subprocess
    returncode on failure.
    """
    idx_path = root / "phases" / phase / "index.json"
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    worktree_branch = data.get("worktree") or f"feat/{phase}"
    steps = data.get("steps", [])
    rc = _intent_integrity_pre_build_gate(root, phase)
    if rc != 0:
        return rc
    for step_meta in steps:
        n = step_meta["step"]
        cur_status = step_meta.get("status")
        # Skip already-done or not-yet-written steps.
        if cur_status in SKIPPABLE_STATUSES:
            continue
        # Blocked → either skip (--skip-blocked) or bail with exit 2 (no implicit resume).
        if cur_status == "blocked":
            reason = step_meta.get("blocked_reason") or "(no reason recorded)"
            print(f"step {n} blocked: {reason}", file=sys.stderr)
            if skip_blocked:
                _record_skipped_blocked(root, phase, n, reason)
                continue
            return 2
        # From any RESUMABLE state, mark in_progress then run.
        if cur_status not in RESUMABLE_STATUSES:
            print(f"step {n}: unexpected status {cur_status!r}, skipping", file=sys.stderr)
            continue
        rc = _run_one_step(root, phase, n, worktree_branch, step_meta.get("name", ""), push)
        if rc != 0:
            return rc
    return 0


def _step_pre_spawn(
    root: Path,
    phase: str,
    step_num: int,
    worktree_branch: str,
) -> dict:
    """Pre-spawn shared stage (issue #79 follow-up). Returns a ctx dict.

    Stages:
      1. Create per-step worktree from origin/main (MUST-38).
      2. Read step<N>.md as preamble; append AC + self-fix guard.
      3. Mark step `in_progress` (sets started_at).

    Returns a dict with the worktree path, branch, full_prompt, and
    started_at_iso for the post-collect stage to consume.
    """
    wt = root / ".worktrees" / f"{phase}-step{step_num}"
    branch = f"{worktree_branch}-step{step_num}"
    # Per-step reset semantics: ``-B`` resets the branch ref to
    # ``origin/main`` so a previous failed run's stale branch at the
    # same name does not block the new step. The worktree dir still
    # must NOT exist (preserve historical invariant — a stale dir
    # means a previous run was interrupted, surface that to the user
    # rather than silently clobber). Routed through the canonical
    # ``cut_worktree`` helper so the policy is shared with future
    # callers (issue #310).
    #
    # Pass ``subprocess.run`` from this module's namespace so tests that
    # patch ``execute.subprocess.run`` to verify the worktree-add call
    # pattern still observe the call (the helper's default would route
    # through ``git_worktree.subprocess.run``, bypassing the patch).
    cut_worktree(
        repo_root=root,
        branch=branch,
        worktree_path=wt,
        reset_branch=True,
        overwrite_worktree=False,
        git_runner=subprocess.run,
    )
    preamble_path = root / "phases" / phase / f"step{step_num}.md"
    preamble = preamble_path.read_text(encoding="utf-8") if preamble_path.exists() else ""
    full_prompt = preamble + "\n\n---\nAC: see step file. 3-cycle self-fix max."
    update_step_status(root, phase, step_num, status="in_progress")
    started_event_id = _emit_effectiveness_event(
        root, phase, step_num, "step.started", "started",
        {"branch": branch, "worktree": str(wt), "step_file": str(preamble_path)},
    )
    return {
        "wt": wt,
        "branch": branch,
        "full_prompt": full_prompt,
        "started_at_iso": now_iso(),
        "started_event_id": started_event_id,
    }


def _step_post_collect(
    root: Path,
    phase: str,
    step_num: int,
    step_name: str,
    ctx: dict,
    *,
    push: bool,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> int:
    """Post-collect shared stage (issue #79 follow-up). Returns the exit code.

    Stages:
      1. write step<N>-output.json with REAL exit_code/stdout/stderr/duration.
      2. On `<!-- status: blocked -->` marker → mark blocked, return 2.
      3. On non-zero exit → mark `error`, return exit code.
      4. 2-commit protocol: feat(scope) + chore(scope) on the per-step branch.
      5. Push per-step branch when `push=True`.
      6. Mark `completed` with measured duration.
    """
    try:
        started = datetime.fromisoformat(ctx["started_at_iso"])
        duration = max(0.0, (datetime.fromisoformat(now_iso()) - started).total_seconds())
    except Exception:
        duration = 0.0

    # Issue #221 RC3: parse `<!-- status: blocked -->` BEFORE marking completed.
    blocked_reason = _extract_blocked_reason(stdout or "")
    # Issue #477: write into the per-step worktree (`wt`), not the
    # orchestrator's main checkout (`root`) — `_commit_step` below stages
    # from `wt`'s independent working directory, so a JSON file written
    # under `root/phases/...` would never be seen by `git add -A` there.
    wt = ctx["wt"]
    write_step_output(
        wt, phase, step_num,
        exit_code=exit_code,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_seconds=duration,
        blocked=bool(blocked_reason),
        blocked_reason=blocked_reason,
    )

    if blocked_reason is not None:
        update_step_status(root, phase, step_num, status="blocked", blocked_reason=blocked_reason)
        _emit_effectiveness_event(
            root, phase, step_num, "step.blocked", "blocked",
            {"reason": blocked_reason, "output": f"phases/{phase}/step{step_num}-output.json"},
            ctx.get("started_event_id"),
        )
        return 2
    if exit_code != 0:
        update_step_status(root, phase, step_num, status="error", error_message=f"claude exited {exit_code}")
        _emit_effectiveness_event(
            root, phase, step_num, "step.failed", "error",
            {"exit_code": exit_code, "output": f"phases/{phase}/step{step_num}-output.json"},
            ctx.get("started_event_id"),
        )
        return exit_code

    # Issue #221 RC2: --allow-empty is GONE. add-A + conditional commit.
    feat_msg = f"feat({phase}): step {step_num}" + (f" — {step_name}" if step_name else "")
    wrote_files = _commit_step(wt, feat_msg)
    output_committed = _commit_step(wt, f"chore({phase}): step {step_num} output")
    if wrote_files or output_committed:
        _emit_effectiveness_event(
            root, phase, step_num, "write.observed", "written",
            {
                "exit_code": exit_code,
                "files_committed": wrote_files,
                "output_committed": output_committed,
                "output": f"phases/{phase}/step{step_num}-output.json",
            },
            ctx.get("started_event_id"),
        )

    if push:
        subprocess.run(
            ["git", "push", "-u", "origin", ctx["branch"]],
            cwd=str(wt), check=False, capture_output=True, text=True,
        )

    update_step_status(root, phase, step_num, status="completed")
    _emit_effectiveness_event(
        root, phase, step_num, "step.completed", "completed",
        {"exit_code": exit_code, "duration_seconds": duration},
        ctx.get("started_event_id"),
    )
    return 0


def _run_step_body(
    root: Path,
    phase: str,
    step_num: int,
    worktree_branch: str,
    step_name: str,
    *,
    push: bool,
    run_proc: Callable[[str, list[str]], Tuple[int, str, str]],
) -> int:
    """Sequential wrapper: pre-spawn + run_proc + post-collect (issue #79 follow-up).

    The actual work now lives in `_step_pre_spawn` + `_step_post_collect`
    (issue #79 review follow-up). This function is the thin orchestrator
    that wires the two halves around a `run_proc` closure.
    """
    ctx = _step_pre_spawn(root, phase, step_num, worktree_branch)
    wt = ctx["wt"]
    # Issue #221 RC1: --add-dir <wt> + --allowedTools so the sub-agent can
    # write into the per-step worktree even when the consumer's parent
    # Claude Code sandbox blocks ".worktrees/**" by default.
    exit_code, stdout, stderr = run_proc(
        str(root),
        _agent_command(wt, ctx["full_prompt"]),
    )
    return _step_post_collect(
        root, phase, step_num, step_name, ctx,
        push=push, exit_code=exit_code, stdout=stdout, stderr=stderr,
    )


def _run_one_step(
    root: Path,
    phase: str,
    step_num: int,
    worktree_branch: str,
    step_name: str,
    push: bool,
) -> int:
    """Sequential wrapper around `_run_step_body`. Uses `subprocess.run`."""
    def _run(cwd: str, args: list[str]) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(
                args, cwd=cwd, capture_output=True, text=True,
                timeout=_agent_timeout_seconds(),
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            stdout = _output_text(exc.stdout)
            stderr = _output_text(exc.stderr) + f"\nagent timed out after {_agent_timeout_seconds()} seconds"
            return 124, stdout, stderr
    return _run_step_body(
        root, phase, step_num, worktree_branch, step_name,
        push=push, run_proc=_run,
    )



def _record_skipped_blocked(root: Path, phase: str, step: int, reason: str) -> None:
    """Append a paragraph to .dev-kit/hand-off/build→review.md naming the skipped blocked step.

    Uses the same atomic-write helper as the rest of the engine so concurrent slot
    appends do not race. The hand-off file is created with a header on first write.
    """
    from atomic import atomic_write_text  # local import to avoid module-load churn
    handoff = root / ".dev-kit" / "hand-off" / "build→review.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not handoff.exists():
        header = (
            "# build → review hand-off\n\n"
            "> Auto-generated by `lib/execute.py` when `--skip-blocked` is set.\n\n"
        )
    line = f"- step {step} skipped in phase {phase} (status=blocked): {reason}\n"
    existing = handoff.read_text(encoding="utf-8") if handoff.exists() else header
    atomic_write_text(handoff, existing + line)


def _run_parallel(root: Path, phase: str, n: int, push: bool, skip_blocked: bool = False) -> int:
    """Run N steps concurrently with per-step worktree isolation.

    Wall-clock bounded by slowest slot, not sum. Each slot gets its own worktree.
    Returns 0 on success, non-zero if any slot failed. Combined pre-flight uses
    the same SKIPPABLE_STATUSES / blocked rules as sequential. When
    `skip_blocked=True`, blocked steps are skipped (with hand-off note) instead
    of bailing the whole run.
    """
    idx_path = root / "phases" / phase / "index.json"
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    worktree_branch = data.get("worktree") or f"feat/{phase}"
    steps = data.get("steps", [])
    rc = _intent_integrity_pre_build_gate(root, phase)
    if rc != 0:
        return rc
    # Collect only steps that are RESUMABLE. Blocked bails the whole run.
    eligible = []
    for step_meta in steps:
        cur_status = step_meta.get("status")
        if cur_status in SKIPPABLE_STATUSES:
            continue
        if cur_status == "blocked":
            reason = step_meta.get("blocked_reason") or "(no reason recorded)"
            print(f"step {step_meta['step']} blocked: {reason}", file=sys.stderr)
            if skip_blocked:
                _record_skipped_blocked(root, phase, step_meta["step"], reason)
                continue
            return 2
        if cur_status not in RESUMABLE_STATUSES:
            print(f"step {step_meta['step']}: unexpected status {cur_status!r}, skipping",
                  file=sys.stderr)
            continue
        eligible.append(step_meta)
        if len(eligible) >= n:
            break

    slot_count = min(_PARALLEL_MAX_CONCURRENT, n, len(eligible))
    slots = [_SlotRunner(root, phase, worktree_branch, push) for _ in range(slot_count)]
    if not slots:
        return 0

    # First pass: launch. Each slot pulls the next eligible step when free.
    for slot in slots:
        slot.next_step = eligible.pop(0) if eligible else None
    while any(s.next_step is not None or s.proc is not None for s in slots):
        for slot in slots:
            if slot.proc is None and slot.next_step is not None:
                slot.launch()
                if eligible:
                    slot.next_step = eligible.pop(0)
                else:
                    slot.next_step = None
            if slot.proc is not None and slot.proc.poll() is not None:
                slot.collect()
    return 0 if all(slot.exit_code == 0 for slot in slots) else 1


class _SlotRunner:
    """One concurrent slot in _run_parallel. Owns worktree, proc, and step status."""

    def __init__(self, root: Path, phase: str, worktree_branch: str, push: bool) -> None:
        self.root = root
        self.phase = phase
        self.worktree_branch = worktree_branch
        self.push = push
        self.next_step: Optional[Dict] = None
        self.current_step: Optional[Dict] = None
        self.proc: Optional[subprocess.Popen] = None
        self.exit_code: int = 0
        self.started_at_iso: Optional[str] = None
        self.wt: Optional[Path] = None
        self.branch: Optional[str] = None
        self._ctx: Optional[dict] = None

    def launch(self) -> None:
        """Spawn the per-step sub-agent via Popen. Pre-spawn is shared.

        The pre-spawn stage (worktree-add, preamble, in_progress stamp)
        routes through `_step_pre_spawn` — the same helper `_run_step_body`
        uses for the sequential path. `collect()` then routes through
        `_step_post_collect` so the parallel path produces the same
        commit-message format + status transitions as the sequential path
        (issue #79 follow-up).
        """
        step = self.next_step
        if step is None:
            return
        n = step["step"]
        self.current_step = step
        self._ctx = _step_pre_spawn(self.root, self.phase, n, self.worktree_branch)
        self.wt = self._ctx["wt"]
        self.branch = self._ctx["branch"]
        self.started_at_iso = self._ctx["started_at_iso"]
        # Issue #221 RC1: same --add-dir + --allowedTools fix as sequential.
        self.proc = subprocess.Popen(
            _agent_command(self.wt, self._ctx["full_prompt"]),
            cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def collect(self) -> None:
        """Communicate with the Popen + route through the shared post-collect stage."""
        assert self.proc is not None
        try:
            stdout, stderr = self.proc.communicate(timeout=_agent_timeout_seconds())
            self.exit_code = self.proc.returncode or 0
        except subprocess.TimeoutExpired as exc:
            self.proc.kill()
            stdout, stderr = self.proc.communicate()
            stdout = _output_text(stdout or exc.stdout)
            stderr = _output_text(stderr or exc.stderr) + (
                f"\nagent timed out after {_agent_timeout_seconds()} seconds"
            )
            self.exit_code = 124
        step = self.current_step
        assert step is not None
        # Route through the SAME post-collect helper the sequential path uses.
        self.exit_code = _step_post_collect(
            self.root, self.phase, step["step"], step.get("name", ""),
            self._ctx,
            push=self.push,
            exit_code=self.exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
        )
        self.proc = None
        self.current_step = None


if __name__ == "__main__":
    sys.exit(main())
