---
name: ralph
category: audit
alpha: state
description: End-to-end autonomous loop with 4 user gates + unattended build/babysit/ship.
when_to_use:
  - User types /dev-kit:ralph <idea>
  - User wants evidence → proposal → plan → build → babysit-pr → ship with intermediate approval gates
  - User wants scope, ambiguity, and gate contract spelled out in the proposal
  - User wants the long-running templates wired to a top-level orchestrator
allowed-tools: Read Write Glob Bash Skill
disallowed-tools: Agent Edit WebFetch
model: opus
disable-model-invocation: false
user-invocable: true
safety:
  safety_valve: 8
  convergence: terminal_state in {DONE, RECOVERY_REQUIRED, USER_MERGE_REQUIRED}
  dedup_metric: same-stage-repeat=2
  user_interrupt: gated
---
> [← Skills index](../../README.md)

# /dev-kit:ralph — End-to-end autonomous loop

Single slash command that takes a 1-line idea and walks it end-to-end:

```
RESEARCH_GATE → PROPOSAL_GATE → PLAN_GATE → SHIP_CONFIRM_GATE → ATTENDED_RUN
                                                              (no Ask)
                                                              → DONE | RECOVERY_REQUIRED | USER_MERGE_REQUIRED
```

After SHIP_CONFIRM_GATE exits Approve, the chain enters **ATTENDED_RUN**
with `attended_lock=True`. From that point forward, the orchestrator
refuses every `AskUserQuestion` invocation at the state-machine layer
(`skills/ralph/lib/ralph_state.py::RalphState.can_ask_question()`).
This is an invariant, not convention.

## The 4 gates + 1 attended execution

| # | Stage | AskUserQuestion? | Artifact | On Approve |
|---|---|---|---|---|
| 1 | RESEARCH_GATE | yes | `.dev-kit/hand-off/research/<session>.md` | transition to PROPOSAL_GATE |
| 2 | PROPOSAL_GATE | yes | `docs/proposals/<bucket>/<main>/<sub>.html` | transition to PLAN_GATE |
| 3 | PLAN_GATE | yes | `PRD.md` + `phases/<name>/{index.json, step<N>.md}` | transition to SHIP_CONFIRM_GATE |
| 4 | SHIP_CONFIRM_GATE | yes | `ship-confirm.md` (10-line summary) | SET attended_lock; enter ATTENDED_RUN |
| 5 | ATTENDED_RUN | **no** | (none) | chain runs `babysit-pr --operator-is-only-human --rationale "ralph-session=<id>"` then `ship` to terminal |

Every gate has the same three options: **Approve**, **Edit-then-approve**
(rewinds via `RalphState.rewind_to(stage)`), **Abort** (state preserved;
re-invocation resumes).

## Iteration loop (Edit-then-approve)

When the user replies with edits (e.g. `A2=no, use pytest parametrize`),
the orchestrator:

1. Parses referenced ambiguities by stable id (`A1`..`A6`)
2. Applies the answers to `state.ambiguity_answers`
3. Calls `state.rewind_to(<gate>)` — clears downstream state
4. Re-renders the affected layer(s)
5. Re-asks the gate

`rewind_to()` is bounded: refuses to rewind forward, refuses to rewind
once `attended_lock` is set (the one-way boundary is crossed).

## attended_lock (the hard wall)

`RalphState.attended_lock` defaults to `False`. The transition
`SHIP_CONFIRM_GATE → ATTENDED_RUN` is the only edge that sets it to
`True`. Once set:

- `can_ask_question()` returns `False`
- `assert_can_ask(state, ...)` raises `AttendedLockError`
- `can_enter(<gate>)` returns `False` for any non-terminal target
- `rewind_to()` raises `AttendedLockError`
- The forensic field `last_blocked_ask` is populated whenever a
  blocked Ask is attempted (for postmortem)

The test `tests/test_ralph_skill.py::test_attended_run_forbids_ask`
is the invariant guard.

## ATTENDED_RUN chain contract (mandatory)

After SHIP_CONFIRM_GATE approves and `attended_lock` flips to True,
the chain MUST walk the unattended execution phase without ever
calling `AskUserQuestion`. Two layers enforce this:

### Layer 1 — state machine invariant (`lib/ralph_state.py`)

`RalphState.can_ask_question()` returns False once `attended_lock=True`
or `current_stage=ATTENDED_RUN`. The orchestrator MUST call
`assert_can_ask(state, ...)` before every AskUserQuestion. If the
guard fires, the call raises `AttendedLockError` and the state
machine records `last_blocked_ask` for forensics.

### Layer 2 — mechanical hook (`hooks/ralph-attended-lock.sh`)

`hooks/hooks.json` wires the AskUserQuestion matcher to
`hooks/ralph-attended-lock.sh`. The hook reads
`.dev-kit/ralph/<session>.json` via the canonical `ralph_state`
module and exits 2 with a deny JSON envelope whenever
`attended_lock=True` or `current_stage=ATTENDED_RUN`. Toolchain-missing
fails OPEN with a stderr WARN — the state-machine layer still enforces
the invariant, so the deny decision is correct on any host.

### babysit-pr flag recipe (mandatory during ATTENDED_RUN)

`skills/babysit-pr/SKILL.md:71-74` declares the default human-gate
path: with no flags, babysit-pr prints `REVIEW_REQUIRED -> human-gate`
and exits 0 without iterating. The unattended chain MUST pass BOTH
flags:

```text
/dev-kit:babysit-pr \
  --operator-is-only-human \
  --rationale "ralph-session=<session-id> unattended after SHIP_CONFIRM_GATE approve; operator=<handle>"
```

`RealDispatch.babysit()` in `lib/ralph_chain.py` emits exactly this
argv. The `--operator-is-only-human` flag is what flips babysit-pr into
its unattended repair loop; the `--rationale` is what the operator's
audit comment requires per `skills/babysit-pr/SKILL.md:494-588`.

### Exit-code mapping (`run_attended` in `lib/ralph_chain.py`)

| Dispatch outcome | Terminal landed | Operator action |
|---|---|---|
| `BUILD` exit 0 → `BABYSIT` exit 0 (`terminal="USER_MERGE_REQUIRED"`) | `USER_MERGE_REQUIRED` | `gh pr merge` |
| `BUILD` exit 0 → `BABYSIT` exit 0 (clean ship) → `SHIP` exit 0 | `DONE` | review final state |
| `BUILD` exit != 0 (build 3-cycle self-fix / state-machine reject / env error) | `RECOVERY_REQUIRED` | investigate + retry |
| `BABYSIT` exit != 0 (MAX_ITERS / watchdog / 3-consecutive-no-progress) | `RECOVERY_REQUIRED` | investigate + retry |
| Same sub_stage re-entered twice (same-stage-repeat=2 trip wire) | `RECOVERY_REQUIRED` | review loop log |
| Sub-skill crashed (any other Exception) | `RECOVERY_REQUIRED` | review crash + retry |
| `SHIP` pre-condition failed but build green + review approved | `USER_MERGE_REQUIRED` | `gh pr merge` |
| `AttendedLockError` raised mid-dispatch | (propagates) | forensic field already populated |

`USER_MERGE_REQUIRED` is the **expected landing** on a healthy single-
operator repo: build green, review approved, tag pushed, but the
human operator runs `gh pr merge` themselves per babysit-pr's
iron laws (`skills/babysit-pr/SKILL.md:476-483`).

### Recovery & re-entry

`RECOVERY_REQUIRED` is terminal but non-fatal. An operator who wakes
up to it inspects `.dev-kit/ralph/<session>.json` (canonical record)
+ the per-skill output captured in `state.build_state`,
`state.babysit_state`, `state.ship_state`. Re-invoking `/dev-kit:ralph`
with the same idea resumes from the persisted state — the on-disk
JSON is the recovery source-of-truth, not the assistant's memory.

### CLI surface

```bash
python3 -m skills.ralph.lib.ralph_chain \
  --project-root . --session default \
  run-attended --dispatch noop   # dry-run: BABYSIT exits 0 → USER_MERGE_REQUIRED
```

The `run-attended --dispatch noop` form uses a `RecordingDispatch`
that bypasses subprocess and returns a successful USER_MERGE_REQUIRED
landing; useful for state-machine smoke tests without spawning
babysit-pr. `tests/test_ralph_chain.py::test_cli_dry_run_*` pins this.

## State machine — `skills/ralph/lib/ralph_state.py`

Pure Python dataclass. No subprocess at import time. Persists to
`.dev-kit/ralph/<session>.json` on every transition via
`atomic_write_text` (tmp + rename).

```python
# Stages
RESEARCH_GATE, PROPOSAL_GATE, PLAN_GATE, SHIP_CONFIRM_GATE  # gates (Ask allowed)
ATTENDED_RUN                                                # locked (Ask forbidden)
DONE, RECOVERY_REQUIRED, USER_MERGE_REQUIRED                # terminals

# Chain
GATE_ORDER = [RESEARCH_GATE, PROPOSAL_GATE, PLAN_GATE,
              SHIP_CONFIRM_GATE, ATTENDED_RUN]
```

CLI:

```bash
python3 -m skills.ralph.lib.ralph_state --project-root . init "add a hello-world skill"
python3 -m skills.ralph.lib.ralph_state --project-root . show
python3 -m skills.ralph.lib.ralph_state --project-root . transition PLAN_GATE --action "user approved proposal"
python3 -m skills.ralph.lib.ralph_state --project-root . rewind PROPOSAL_GATE --reason "user edits ambiguity A2"
python3 -m skills.ralph.lib.ralph_state --project-root . can-ask  # exits 0 if Ask allowed, 1 if locked
```

## Linear is OUT OF SCOPE

Per the user's explicit request, this skill does NOT call
`/dev-kit:linear` or create/sync/close Linear issues. Linear stays a
separate opt-in skill (`/dev-kit:linear`) outside the Ralph chain.
If a future PR wants Linear back, it lands as a separate proposal
under `accepted/`, not as a Ralph amendment.

## Bash glue — `skills/ralph/scripts/ralph_drive.sh`

Linear bash script that chains the underlying Skill invocations.
Reads/writes `.dev-kit/ralph/<session>.json` between hops. Re-uses
the long-running templates (`templates/init.sh`, `feature_list.json`,
`progress.log.md`, `session_handoff.md`, `ralph_progress.md`) at the
build step via the same `cp -u` pattern `skills/build/SKILL.md`
already documents.

## Long-running templates wiring

At Phase 6 (build) the orchestrator runs:

```bash
mkdir -p .worktrees/<phase>-step<N>/templates
cp -u templates/init.sh templates/feature_list.json \
   templates/progress.log.md templates/session_handoff.md \
   templates/ralph_progress.md \
   .worktrees/<phase>-step<N>/templates/
```

`templates/ralph_progress.md` is a per-run complement (not a
replacement) of the cross-task `templates/progress.log.md`. It tracks:
gate transitions with reviewer signature, last action, next action,
blockers, operator-readable one-liner.

## Ralph-loop safety valves

| Valve | Source | What it does |
|---|---|---|
| 3-cycle self-fix | `lib/execute.py` MUST-37 | build's per-step sub-agent fails 3× → RECOVERY_REQUIRED |
| 3-consecutive-no-progress | `skills/babysit-pr/SKILL.md` | babysit-pr polls 3× with no change → RECOVERY_REQUIRED |
| MAX_ITERS=1000 | `skills/babysit-pr/SKILL.md` | babysit-pr hits watchdog cap → RECOVERY_REQUIRED |
| same-stage-repeat=2 | ralph itself | same sub-stage re-enters twice → RECOVERY_REQUIRED |

## What this skill does NOT do

- ❌ Auto-merge to `main` (MUST-NO-SKIP from babysit-pr)
- ❌ Force-push to `main`
- ❌ Skip `gh pr` review checks
- ❌ Bypass the human-merge boundary (USER_MERGE_REQUIRED is terminal)
- ❌ Run unattended across Linear's `LINEAR_ERROR` cases (out of scope)
- ❌ Spawn sub-agents (`disallowed-tools: Agent` — Ralph orchestrates,
      never edits source)

## When to use this skill

- The user types `/dev-kit:ralph <idea>` with a 1-line idea they
  want taken end-to-end.
- The user has approved a prior proposal and wants the chain driven
  through to a green PR without further interrupts.

## When NOT to use this skill

- The user wants to review each phase of an existing project
  (use `/dev-kit:plan`, `/dev-kit:build`, `/dev-kit:babysit-pr` directly).
- The user wants Linear to track the work (out of scope; use
  `/dev-kit:linear` separately).
- The user wants to amend an in-flight build (use `/dev-kit:adapt`).
- The user wants to refactor an existing codebase (use `/dev-kit:refactor`).

## What "Ralph" means

Named after the Ralph Wiggum pattern (Geoffrey Huntley,
[ghuntley.com/ralph](https://ghuntley.com/ralph/)). The discipline is:
*make the loop durable, make the contract explicit at the front,
make the failure surface at the end*. dev-harness-kit's chain
already had 6 of 7 components; Ralph wires them together with 4
explicit gates and one unattended execution phase.
