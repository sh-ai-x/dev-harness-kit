> [← Skills index](README.md) · [Project README](../../README.md)

# `ralph`

**Category:** `audit` · **Alpha:** `state` · **Invocation:** `/dev-kit:ralph <idea>` (human-invoked)

`ralph` is the end-to-end autonomous orchestrator: a single slash that takes
a 1-line idea and walks it through **4 explicit user-approval gates** plus
one **unattended execution phase** that runs build / babysit-pr / ship with
**no further AskUserQuestion** until terminal state. Linear is OUT OF SCOPE
— `/dev-kit:linear` remains a separate opt-in skill.

The contract: after SHIP_CONFIRM_GATE exits Approve, the chain enters
`ATTENDED_RUN` with `attended_lock=True` and the state machine refuses every
`AskUserQuestion` invocation. The lock is an invariant (not convention) —
every Ask during `ATTENDED_RUN` is rejected with `AttendedLockError` and a
forensic `last_blocked_ask` field.

Two layers enforce the Ask-refusal invariant during `ATTENDED_RUN`:

1. **State machine** — `lib/ralph_state.py::RalphState.can_ask_question()`
   returns `False` once `attended_lock=True`; the orchestrator raises
   `AttendedLockError` if any sub-skill tries to call
   `assert_can_ask` mid-chain.
2. **Mechanical hook** — `hooks/ralph-attended-lock.sh` is wired as a
   `PreToolUse` matcher on `AskUserQuestion` in `hooks/hooks.json` and
   `.codex-plugin/hooks/hooks.json`. The hook reads the canonical
   ralph_state from disk and exits 2 with a deny JSON envelope so even a
   misbehaving sub-skill cannot surface an Ask. The hook fails OPEN on
   toolchain-missing (the state machine remains the source of truth).

The unattended chain runs via `lib/ralph_chain.py::run_attended()`,
which walks `BUILD → BABYSIT → SHIP` with injectable dispatch shims.
`RealDispatch.babysit()` always invokes
`babysit-pr --operator-is-only-human --rationale "ralph-session=<id>..."`
— without both flags, `babysit-pr` defaults to the human-gate (issue #324,
see `skills/babysit-pr/SKILL.md:71-74`). The bash glue
`scripts/ralph_drive.sh run-attended` is the operator-facing entrypoint.

## When to use it

- The user types `/dev-kit:ralph <idea>` with a 1-line idea they want taken
  end-to-end.
- The user has approved a prior proposal and wants the chain driven through
  to a green PR without further interrupts.

## How it works

The chain is a 5-stage state machine (4 gates + 1 attended execution):

```
RESEARCH_GATE → PROPOSAL_GATE → PLAN_GATE → SHIP_CONFIRM_GATE → ATTENDED_RUN
                                                                     (no Ask)
                                                                     → DONE | RECOVERY_REQUIRED | USER_MERGE_REQUIRED
```

Each interactive gate (1-4) emits an `AskUserQuestion` with three options:

```
Approve           → transition to next stage
Edit-then-approve → rewind_to(<gate>) + re-render + re-ask
Abort             → persist state, exit 0
```

**Stage 1 — RESEARCH_GATE.** `Skill("research", <idea>)` writes cited
evidence to `.dev-kit/hand-off/research/<session>.md`. Reviewer approves
the evidence; on Approve, transition to PROPOSAL_GATE.

**Stage 2 — PROPOSAL_GATE.** Writes `docs/proposals/<bucket>/<main>/<sub>.yaml`
then `Skill("proposal", ...)` renders `main.html`. The proposal YAML
includes `ambiguity:` (stable `A1`..`A6` ids), `scope.in_scope` /
`scope.out_of_scope`, and a `gate_contract:` table so the reviewer can see
ambiguities and boundaries up front.

**Stage 3 — PLAN_GATE.** `Skill("plan", <idea>, --best-effort)` writes
`PRD.md` + `phases/<name>/{index.json, step<N>.md}`. `interview` runs in
best-effort mode. Auto-renders plan HTML.

**Stage 4 — SHIP_CONFIRM_GATE.** Ralph synthesizes a 10-line summary
(`ship-confirm.md`): phases green, plan locked, build about to start
unattended. This is the LAST review surface. On Approve, the chain sets
`attended_lock=True` and enters ATTENDED_RUN.

**Stage 5 — ATTENDED_RUN.** The lock is set. `Skill("build", "--push")` →
`Skill("babysit-pr")` → `Skill("ship")` run unattended. The
`MAX_ITERS=1000` watchdog + 3-consecutive-no-progress guard from
babysit-pr cap runtime at ~45 min before surfacing `RECOVERY_REQUIRED`.

## State machine

Pure Python dataclass — `skills/ralph/lib/ralph_state.py`. No subprocess,
no `gh` at import time. Persists to `.dev-kit/ralph/<session>.json` on
every transition via `atomic_write_text` (tmp + rename).

```python
RESEARCH_GATE     = "RESEARCH_GATE"
PROPOSAL_GATE     = "PROPOSAL_GATE"
PLAN_GATE         = "PLAN_GATE"
SHIP_CONFIRM_GATE = "SHIP_CONFIRM_GATE"
ATTENDED_RUN      = "ATTENDED_RUN"
DONE              = "DONE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
USER_MERGE_REQUIRED = "USER_MERGE_REQUIRED"
```

The `attended_lock` field defaults to `False`. The transition
`SHIP_CONFIRM_GATE → ATTENDED_RUN` is the only edge that sets it to
`True`. Once set, `can_ask_question()` returns `False` and `rewind_to()`
raises `AttendedLockError`. The lock is reset only by an explicit
`rewind_to()` to a prior gate (which clears the lock AND downstream
state).

## Bash glue

`skills/ralph/scripts/ralph_drive.sh` exposes a 5-subcommand CLI that
calls into the state machine:

```
ralph_drive.sh init <idea>            # create state at RESEARCH_GATE
ralph_drive.sh status                 # print state JSON
ralph_drive.sh can-ask                # exit 0/1 based on attended_lock
ralph_drive.sh advance <stage>        # transition (state-machine validated)
ralph_drive.sh rewind <gate> --reason # Edit-then-approve handler
```

## Iteration loop

Edit-then-approve is bounded and tested:

- `rewind_to(<gate>)` clears downstream state (`plan_hand_off`,
  `build_state`, `babysit_state`, `ship_state`, `ambiguity_answers`).
- Refuses to rewind forward (`rewind_to(PLAN_GATE)` from
  `PROPOSAL_GATE` is rejected).
- Refuses to rewind once `attended_lock` is set (one-way boundary).
- Bounded to one target per Edit-then-approve reply (no double-rewinds).

## Ralph-loop safety valves

| Valve | Source | When it fires |
|---|---|---|
| 3-cycle self-fix | `lib/execute.py` MUST-37 | build's per-step sub-agent fails 3× |
| 3-consecutive-no-progress | `skills/babysit-pr/SKILL.md` | babysit-pr polls 3× no change |
| MAX_ITERS=1000 | `skills/babysit-pr/SKILL.md` | babysit-pr watchdog cap |
| same-stage-repeat=2 | `lib/ralph_state.py` | sub-stage re-enters twice |

All four write `RECOVERY_REQUIRED` to the state and exit; re-invoking
`/dev-kit:ralph` resumes from the last persisted checkpoint.

## What this skill does NOT do

- ❌ Auto-merge to `main` (MUST-NO-SKIP from babysit-pr)
- ❌ Force-push to `main`
- ❌ Skip `gh pr` review
- ❌ Run Linear integration (out of scope; use `/dev-kit:linear` separately)
- ❌ Span multiple repos
- ❌ Spawn sub-agents (`disallowed-tools: Agent` — Ralph orchestrates)

## Reference

The proposal that this skill implements lives at
[`docs/proposals/review/ralph-autonomy/main.yaml`](../../proposals/review/ralph-autonomy/main.yaml).
Read it for the full design rationale, ambiguity defaults, scope
boundaries, and verification plan.
