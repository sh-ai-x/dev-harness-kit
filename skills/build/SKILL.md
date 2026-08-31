---
name: build
category: build
description: 0-arg. Per-step sub-agent delegation + self-fix loop (MUST-36~38). Uses harness-runner engine. TDD + verify + debug integrated.
alpha: state
when_to_use: |
  - User types /dev-kit:build
  - After plan+design (PRD.md + phases/<name>/ exist)
  - After /dev-kit:ci-setup has written .dev-kit/ci-config.json (REQUIRED — refuse if marker missing)
allowed-tools: Read Write Bash Glob Grep Agent Skill
disallowed-tools: WebFetch
model: opus
disable-model-invocation: false
---
> [← Skills index](../../README.md)

## What it does

Executes `phases/<name>/step{1..N}.md` end-to-end by spawning one non-interactive agent per step inside an isolated per-step git worktree, persisting real `step<N>-output.json` (subprocess exit code, stdout, stderr, measured duration), and emitting the 2-commit protocol on the per-step branch. Claude is the default; set `DEV_KIT_BUILD_AGENT=codex` to use `codex exec`. Every step has a bounded timeout from `DEV_KIT_AGENT_TIMEOUT_SECONDS` (default 1 hour, max 24 hours). Honors MUST-36 (one sub-agent per step), MUST-37 (3-cycle self-fix guard), MUST-38 (per-step worktree isolation).

## Session-scoped gate mode (workflow-fast-mode-lean)

Run `/dev-kit:harness-mode fast|full|custom` before (or during) a build to
control which optional local gates run this session — `full` (the default,
reset every session by a SessionStart hook) runs everything; `fast` skips
`tdd_scope_judge` and `slop_detector`; `custom` picks each gate individually.
Correctness gates (`stop_verify`, `secret_scan`, `intent_integrity`-high) never
turn off, regardless of mode — see `skills/harness-mode/SKILL.md`. Every step
preamble is appended with a one-line gate summary
(`lib/execute.py:_gate_summary_line`) so the per-step sub-agent knows what it
is and is not responsible for that step.

## Optional Linear preflight

At the start of a new build task, invoke `/dev-kit:linear` once when Linear is
enabled or available. Continue normally on `LINEAR_SKIP` or an implicit
`LINEAR_ERROR`; do not invoke it per step, retry, or sub-agent. See
`skills/linear/SKILL.md` for the reconciliation contract.

## Pre-flight gate

Refuses to start if `.dev-kit/ci-config.json` is absent. Run `/dev-kit:ci-setup` (or `/dev-kit:ci-setup --force` to refresh stale templates) first. No version comparison — presence of the marker is the only precondition; dev-kit does not gate consumer builds on a plugin-version floor.

## Pre-flight valuation gate (Phase 4, issue #373)

> **Removed in #463.** The build stage's hard auto-gate that read the
> valuation verdict and refused non-PROCEED verdicts was tied to the LCS
> substrate that backed the URI. The LCS substrate is gone; the
> auto-gate went with it. Operators run `/dev-kit:valuate` explicitly
> before invoking `/dev-kit:build`; a non-PROCEED verdict is the
> operator's signal to halt, not a hard block.

The verdict envelope (when it exists) is at
`.dev-kit/valuations/<plan-id>.json`. If `/dev-kit:valuate` was run,
the build proceeds and the verdict is operator context; if the verdict
is `kill` or unresolved `hold`, the operator should not have invoked
`build`. There is no auto-gate, no `--skip-valuation` flag, and no exit
code based on the verdict.

## Composition with /dev-kit:research and /dev-kit:build-debug

For net-new feature ideas that need cited evidence before planning,
run `/dev-kit:research` directly, then `/dev-kit:plan` — Gate 2.1
(evidence) reads the cited claims straight from that research output.
No binder skill sits between them; `/dev-kit:plan` is a single
`Skill` hop away from a research result.

For bug reports that need a proper plan instead of a quick self-fix,
invoke `/dev-kit:build-debug` standalone (outside any active build
step). It runs the same reproduce → isolate → root-cause phases it
already uses for the in-build self-fix loop, but its Phase 4 branches:
standalone, it calls `Skill("plan", <root cause>)` instead of patching
code inline. Like the research path, **it never invokes
`/dev-kit:build` — that stays a separate, explicit, user-typed
command**, reached only after `/dev-kit:plan`'s Gate 5/5 auto-renders
`proposal.html` and the user reviews it.

For single-session, non-bug work, `/dev-kit:build` runs the direct
`plan -> build` path — no binder. `/dev-kit:plan` emits the canonical
`phases/<name>/index.json` + `step<N>.md` artifacts either way; the
build runner reads those (NOT any binder-owned file).

See `skills/build-debug/SKILL.md` §"Two invocation contexts" for the
per-phase contract.

## Behavior

1. `lib/execute.py:main` reads `phases/<name>/index.json` and calls
   `lib/dispatch_classifier.classify(steps)` to decide parallel vs.
   sequential. The decision + reason are emitted as the first stderr
   line (`dispatch: <mode> — <reason>`) so the user can audit why
   parallelism was rejected.

   **Classifier priority order** (first match wins):
   1. **Dependency edge** between any pair (`depends_on` / `consumes`) → sequential.
   2. **Vague scope** (TODO/FIXME/TBD/maybe/perhaps/either in preamble or AC) → sequential.
   3. **Overlapping writes** between two steps → sequential.
   4. **N ≥ 4** AND clean worktree isolation (every step with non-empty
      `writes` has an explicit `partition`) → parallel.
   5. **Otherwise** → sequential.

   The previous `--parallel N` / `--allow-parallel-build` flags are
   removed. There is no user-facing toggle; the harness reasons about
   dispatch, the user audits the decision.

2. Read `phases/<name>/index.json` (must contain `worktree: "<branch-base>"`; emitted by `/dev-kit:plan` as `<prefix>-<phase>`, e.g. `plan/plugin-harness-v3-0-mvp`); derive per-step branch = `<branch-base>-step<N>` and worktree path = `<root>/.worktrees/<phase>-step<N>`. Falls back to `feat/<phase>` when the field is absent (defense-in-depth, not the contract).
3. Skip entries where `status` ∈ `SKIPPABLE_STATUSES` (`completed`, `unimplemented`).
4. Bail with exit 2 if any step has `status == "blocked"` (no implicit resume).
   Override: `--skip-blocked` lets the runner continue past `blocked` steps, running only `pending | error | in_progress`. Skipped blocked steps are listed in `.dev-kit/hand-off/build→review.md` after the run.
5. For each RESUMABLE step:
   - `git worktree add -B <branch> <wt> origin/main` (MUST-38).
   - Read `step<N>.md` as preamble; append AC guard + `3-cycle self-fix max`.
   - `update_step_status(... status="in_progress")` (stamps `started_at`).
   - Spawn the selected agent command (`claude -p` or `codex exec`) with a bounded timeout (MUST-36).
   - Write `phases/<name>/step<N>-output.json` with REAL `exit_code`, `stdout`, `stderr`, `duration_seconds` (no fake `0.01` or `stub completed`).
   - On non-zero exit: `status="error"`, stash `error_message`, return non-zero — no commits.
   - On success: 2 commits on the per-step branch — `feat({phase}): step {N}[ — <name>]` then `chore({phase}): step {N} output`. Push the per-step branch to `origin` if `--push`.

## Status state machine (lib/execute.py)

SSOT: `lib/execute.py:VALID_STATUSES` (+ `SKIPPABLE_STATUSES`, `RESUMABLE_STATUSES`,
`--skip-blocked` override). The plan/build contract: plan emits `pending`,
build drives `in_progress`/`completed`/`error`/`blocked` per the source
constants.

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

## Long-running session templates (>1 session tasks)

When a build phase is expected to span more than one Claude Code session
(typical signal: step count >= 5, or the user explicitly says "this is a
multi-day effort"), `build` is meant to copy the four-template artifact
bundle from `templates/` into the working tree of the build's per-step
worktree before the first step starts. The templates implement Pattern 2
from `docs/proposals/playbook-application/02-reanalysis.yaml` — the
cold-start recovery cost is the dominant per-session waste, and shipping a
fixed file layout removes the "what did the last session do?" discovery
loop.

| Template | Purpose |
|---|---|
| `templates/init.sh` | Bootstrap: verify env, read feature list, pick next failing feature, run baseline test. Idempotent — re-run every session open. |
| `templates/feature_list.json` | JSON array of `{id, description, status, depends_on, test_path}`. The single source of truth for "what's left". |
| `templates/progress.log.md` | Append-only per-session log (Goal / Work done / Tests status / Blockers / Next session should / Commits). |
| `templates/session_handoff.md` | Resume-from-cold-context checklist; read FIRST at session open, before any code change.

Intended wiring rule: copy the four files into the per-step worktree at
`<worktree>/templates/` on the first step (idempotent — `cp -u` refreshes
only stale files). Each step's preamble (`step<N>.md`) must include a
one-line reminder to append to `progress.log.md` before commit and to
re-run `init.sh` at session open. Steps driven by `codex exec` honor the
same contract; the runner would copy the templates into the worktree
before spawning the agent so the agent sees them as part of its working
tree.

Status: as of this PR the runner does NOT yet perform the copy step
(`lib/execute.py` has no reference to `templates/`). Operators running a
multi-session build must run the copy manually before the first step:

```bash
mkdir -p .worktrees/<phase>-step<N>/templates
cp -u templates/init.sh templates/feature_list.json \
   templates/progress.log.md templates/session_handoff.md \
   .worktrees/<phase>-step<N>/templates/
```

Follow-up: add the copy step to the per-step worktree creation site in
`lib/execute.py` (after `cut_worktree` returns) so the manual step above
is no longer needed. Until then the SKILL.md is normative for the bundle
shape, not for the runner behavior.

Failure mode: if `init.sh` exits 3 ("no failing feature remaining") at
the start of a step, the build has effectively finished — bail to
`/dev-kit:review` instead of forcing another step.

## Test evidence

50 tests in `tests/test_execute.py` covering runner behavior (skippable status skips runner, blocked returns 2, pending step creates worktree + invokes claude with preamble+AC, 2-commit protocol per step, no commits on failure, push gated on `--push`, the new `TestMainDispatchDecision` class for the auto-classify contract, plus 10 state-machine tests for `update_step_status` (in_progress idempotency, duration rounding, reset semantics)). Plus 27 tests in `tests/test_dispatch_classifier.py` covering all 5 classifier rules, priority order, idempotency, and reason format.

## Next step

`/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) then `/dev-kit:ship`.

## Iron Law

L5 (one answer, no option lists unless asked): dispatch is auto-classified;
no `--parallel` user toggle exists. The harness reasons from step metadata;
the user audits the `dispatch: <mode> — <reason>` line.
