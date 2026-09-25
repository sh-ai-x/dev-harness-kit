> [← Skills index](README.md) · [Project README](../../README.md)

# `build`

**Category:** `build` · **Alpha:** `state` · **Invocation:** `/dev-kit:build` (human-invoked)

`build` is the execution engine of the harness: it takes the step files a prior `/dev-kit:plan` produced and actually runs them, one isolated sub-agent per step, so that no single sprawling session has to hold the whole phase's state at once. It exists as its own skill because running steps safely requires real process isolation (per-step git worktrees), a hard concurrency policy, and persisted real subprocess evidence — none of which a plain prompt can guarantee on its own.

## When to use it

- The user types `/dev-kit:build`.
- Planning and design are already done — `PRD.md` and `phases/<name>/` exist.
- `/dev-kit:ci-setup` has already written `.dev-kit/ci-config.json` — this is a hard requirement, not a suggestion.

## How it works

`build` refuses to start if `.dev-kit/ci-config.json` is absent, telling the user to run `/dev-kit:ci-setup` (or `--force` to refresh stale templates) first. There is no version comparison gate — only presence of the marker file matters.

Once the pre-flight gate passes, `lib/execute.py:main` reads the phase index, filters to eligible (resumable, non-blocked) steps, calls `lib.dispatch_classifier.classify(...)` to decide parallel vs sequential, and logs the decision as the first build line (`dispatch: <mode> — <reason>`). There is no user-facing toggle — the harness reasons about dispatch from step metadata, and the user audits the emitted line.

**Classifier priority order** (first match wins):
1. **Dependency edge** between any pair (`depends_on` / `consumes`) → sequential.
2. **Vague scope** (TODO/FIXME/TBD/maybe/perhaps/either in preamble or AC) → sequential.
3. **Overlapping writes** between two steps → sequential.
4. **N ≥ 4 eligible steps** AND clean worktree isolation → parallel.
5. **Otherwise** → sequential.

The previous `--parallel N` / `--allow-parallel-build` flags were removed in v0.3.214; argparse now rejects them. Sequential is the default; parallel only fires when steps are genuinely safe.

`build` reads `phases/<name>/index.json`, which must contain a `worktree: "<branch-base>"` field emitted by `/dev-kit:plan` (e.g. `plan/plugin-harness-v3-0-mvp`). From that it derives the per-step branch (`<branch-base>-step<N>`) and worktree path (`<root>/.worktrees/<phase>-step<N>`); if the field is absent it falls back to `feat/<phase>` as a defense-in-depth measure, not as the intended contract.

Steps whose `status` is in `SKIPPABLE_STATUSES` (`completed`, `unimplemented`) are skipped. The runner bails with exit code 2 if any step has `status == "blocked"` — there is no implicit resume. The `--skip-blocked` override lets the runner continue past blocked steps, running only `pending | error | in_progress` ones; any steps skipped this way are listed in `.dev-kit/hand-off/build→review.md` after the run.

For each resumable step, `build`:

1. Runs `git worktree add -B <branch> <wt> origin/main` (MUST-38 — per-step worktree isolation).
2. Reads `step<N>.md` as the preamble and appends the acceptance-criteria guard plus a "3-cycle self-fix max" instruction (MUST-37).
3. Marks the step `in_progress` via `update_step_status`, which stamps `started_at`.
4. Invokes `subprocess.run(["claude", "-p", "--workdir", str(wt), full_prompt], capture_output=True, text=True)` — exactly one sub-agent per step (MUST-36).
5. Writes `phases/<name>/step<N>-output.json` with the real `exit_code`, `stdout`, `stderr`, and `duration_seconds` — never a stubbed `0.01` or "stub completed" value. This write targets the per-step worktree (`wt`), not the orchestrator's root checkout: the chore commit below runs `git add -A` with `cwd=wt`, so a file written under `root/phases/...` would never be staged there.
6. On non-zero exit: marks the step `error`, stashes the `error_message`, and returns non-zero with no commits made.
7. On success: makes two commits on the per-step branch — `feat({phase}): step {N}[ — <name>]` then `chore({phase}): step {N} output` — and pushes the per-step branch to `origin` if `--push` was set.

The status state machine lives in `lib/execute.py` (`VALID_STATUSES`, `SKIPPABLE_STATUSES`, `RESUMABLE_STATUSES`, and the `--skip-blocked` override) as the single source of truth: `/dev-kit:plan` emits `pending`, and `build` drives the step through `in_progress` / `completed` / `error` / `blocked`.

During the build stage, the hook matrix is: `tdd-guard` ON when `methodology=tdd`, `bash-guard` ON, `secret-scan` ON (PostToolUse), `slop-detector` ON, `stop-verify` ON.

## Usage

```bash
/dev-kit:build [--skip-blocked] [--push]
```

| Flag | Effect |
|---|---|
| `--skip-blocked` | Continue past `blocked` steps, running only `pending \| error \| in_progress`; skipped steps are recorded in the hand-off file. |
| `--push` | Push the per-step branch to `origin` after a successful step. |

Dispatch mode is auto-classified per batch (see "How it works"). There is no `--parallel` flag; the classifier decides.

## Output

- `phases/<name>/step<N>-output.json` per step: `{exit_code, stdout, stderr, duration_seconds, timestamp}`, all real subprocess output.
- `.dev-kit/hand-off/build→review.md`, written automatically.
- A 2-commit protocol per successful step on its per-step branch: `feat({phase}): step {N} — {name}` and `chore({phase}): step {N} output`.

Test evidence: 50 tests in `tests/test_execute.py` cover runner behavior (skippable-status skipping, blocked returning exit 2, pending steps creating a worktree and invoking `claude` with the preamble + acceptance-criteria guard, the 2-commit protocol, no commits on failure, push gated on `--push`, the new `TestMainDispatchDecision` class for the auto-classify contract, plus 10 state-machine tests for `update_step_status` (in-progress idempotency, duration rounding, reset semantics)). Plus 27 tests in `tests/test_dispatch_classifier.py` covering all 5 classifier rules, priority order, idempotency, reason format, and the `?`-marker false-positive regressions.

## Long-running session templates

When a build phase is expected to span more than one Claude Code session — typical signal: step count >= 5, or the user explicitly says "this is a multi-day effort" — `build` copies the four-file template bundle from `templates/` into the per-step worktree at `<worktree>/templates/` before the first step starts (idempotent — `cp -u` refreshes only stale files). The bundle is the cold-start fix: without it, every new session spends 30-60 min re-discovering "what did the last session do?".

| Template | Purpose |
|---|---|
| `templates/init.sh` | Bootstrap: verify env, read feature list, pick next failing feature, run baseline test. Idempotent — re-run every session open. |
| `templates/feature_list.json` | JSON array of `{id, description, status, depends_on, test_path}`. Single source of truth for "what's left". |
| `templates/progress.log.md` | Append-only per-session log (Goal / Work done / Tests status / Blockers / Next session should / Commits). |
| `templates/session_handoff.md` | Resume-from-cold-context checklist; read FIRST at session open, before any code change. |

Operational contract: each step's preamble (`step<N>.md`) must include a one-line reminder to append to `progress.log.md` before commit and to re-run `init.sh` at session open. Steps driven by `codex exec` honor the same contract — the runner copies the templates into the worktree before spawning the agent so the templates are part of the agent's working tree.

Failure mode: if `init.sh` exits 3 (`"no failing feature remaining"`) at the start of a step, the build has effectively finished — bail to `/dev-kit:review` instead of forcing another step.

Template behavior is validated by `tests/test_long_running_templates.py` (structure + behavioral execution in a tmpdir covering dry-run / missing-list / all-passing exit codes).

## Related

- `tdd-guard` and `stop-verify` — deterministic hook enforcement for test-first edits and completion evidence.
- `/dev-kit:review` and `/dev-kit:security`, then `/dev-kit:ship` — the next stages after `build` completes.
- `lib/execute.py` — the harness-runner engine this skill wraps.
- `lib/dispatch_classifier.py` — pure-Python classifier that decides parallel vs sequential per batch (5-rule priority order, default sequential; replaces the legacy `--parallel` flag).
- `tests/test_execute.py` — the 50 tests referenced above.
- `tests/test_dispatch_classifier.py` — the 27 classifier tests covering all 5 rules, priority order, idempotency, reason format.

---
*Source: [`skills/build/SKILL.md`](../../skills/build/SKILL.md)*
