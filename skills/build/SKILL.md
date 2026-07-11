---
name: build
category: build
description: 0-arg. Per-step sub-agent delegation + self-fix loop (MUST-36~38). Uses harness-runner engine. TDD + verify + debug integrated.
when_to_use: |
  - User types /dev-kit:build
  - After plan+design (PRD.md + phases/<name>/ exist)
  - After /dev-kit:ci-setup has written .dev-kit/ci-config.json (REQUIRED — refuse if marker missing or ci_setup_version < "0.1.0")
allowed-tools: Read Write Bash Glob Grep Agent
disallowed-tools: WebFetch
model: opus
disable-model-invocation: false
---

## What it does

Executes `phases/<name>/step{1..N}.md` end-to-end by spawning one `claude -p` sub-agent per step inside an isolated per-step git worktree, persisting real `step<N>-output.json` (subprocess exit code, stdout, stderr, measured duration), and emitting the 2-commit protocol on the per-step branch. Honors MUST-36 (one sub-agent per step), MUST-37 (3-cycle self-fix guard), MUST-38 (per-step worktree isolation).

## Pre-flight gate

No preconditions. `/dev-kit:build` and `/dev-kit:ci-setup` are independent skills — install the CI templates when you want them (one-shot, no contract), skip the install otherwise. The dev-harness-kit plugin ships both skills; neither is a precondition for the other.

## Behavior

1. `lib/execute.py:main` parses args; branches on `--parallel N`:
   - `--parallel 0` → `_run_sequential` (default).
   - `--parallel N` → `_run_parallel` (N concurrent slots, each with its own worktree).
2. Read `phases/<name>/index.json` (must contain `worktree: "<branch-base>"`); derive per-step branch = `<branch-base>-step<N>` and worktree path = `<root>/.claude/worktrees/<phase>-step<N>`.
3. Skip entries where `status` ∈ `SKIPPABLE_STATUSES` (`completed`, `unimplemented`).
4. Bail with exit 2 if any step has `status == "blocked"` (no implicit resume).
5. For each RESUMABLE step:
   - `git worktree add -B <branch> <wt> origin/main` (MUST-38).
   - Read `step<N>.md` as preamble; append AC guard + `3-cycle self-fix max`.
   - `update_step_status(... status="in_progress")` (stamps `started_at`).
   - `subprocess.run(["claude", "-p", "--workdir", str(wt), full_prompt], capture_output=True, text=True)` (MUST-36).
   - Write `phases/<name>/step<N>-output.json` with REAL `exit_code`, `stdout`, `stderr`, `duration_seconds` (no fake `0.01` or `stub completed`).
   - On non-zero exit: `status="error"`, stash `error_message`, return non-zero — no commits.
   - On success: 2 commits on the per-step branch — `feat({phase}): step {N}[ — <name>]` then `chore({phase}): step {N} output`. Push the per-step branch to `origin` if `--push`.

## Status state machine (lib/execute.py)

`unimplemented → pending → in_progress → completed`, with two resume paths: `error → pending` (retry) and `blocked → pending` (human unblock). `SKIPPABLE_STATUSES = ("completed", "unimplemented")`. `RESUMABLE_STATUSES = ("pending", "error", "in_progress")`.

## Hook integration (Stage B)

| Hook | Mode |
|---|---|
| tdd-guard | ON (when methodology=tdd) |
| bash-guard | ON |
| secret-scan | ON (PostToolUse) |
| slop-detector | ON |
| stop-verify | ON |

## Output

- `phases/<name>/step<N>-output.json` per step with `{exit_code, stdout, stderr, duration_seconds, timestamp}` — real subprocess output.
- `.dev-kit/hand-off/build→review.md` auto.
- 2-commit protocol on per-step branch: `feat({phase}): step {N} — {name}` + `chore({phase}): step {N} output`.

## Test evidence

29 tests in `tests/test_execute.py` covering runner behavior: skippable status skips runner, blocked returns 2, pending step creates worktree + invokes claude with preamble+AC, 2-commit protocol per step, no commits on failure, push gated on `--push`. Plus 10 state-machine tests for `update_step_status` (in_progress idempotency, duration rounding, reset semantics).

## Next step

`/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) then `/dev-kit:ship`.
