# /dev-kit:ralph — Per-run progress log

This template is the per-run complement to `templates/progress.log.md`.
Each `/dev-kit:ralph <idea>` invocation appends one section per gate
transition plus one section per Skill invocation. It is the
operator-readable headline; the canonical state is
`.dev-kit/ralph/<session>.json`.

## Header (filled at init)

```
session: <session name>
idea: <original 1-line input>
started_at: <ISO-8601>
current_stage: <RESEARCH_GATE|PROPOSAL_GATE|PLAN_GATE|SHIP_CONFIRM_GATE|ATTENDED_RUN|DONE|RECOVERY_REQUIRED|USER_MERGE_REQUIRED>
attended_lock: <true|false>
```

## Gate transitions (one block per gate the user approved)

```
### [GATE <N>] <GATE_NAME> — <approved|edited|aborted>
- reviewer_signature: <sha256 of the user's Approve/Edit reply, or "n/a">
- idea_at_gate: <the running idea string, possibly amended by edits>
- artifact: <path to the rendered artifact reviewed at this gate>
- ambiguity_answers: <id → answer map applied during this gate>
- rewind_from: <if this gate was entered via rewind, the prior stage>
- timestamp: <ISO-8601>
- duration_seconds: <wall time spent at this gate>
```

## Skill invocations (one block per Skill call inside ATTENDED_RUN or otherwise)

```
### [SKILL] <skill-name> <args>
- invocation_id: <monotonic>
- parent_stage: <the gate or ATTENDED_RUN that triggered this call>
- started_at: <ISO-8601>
- ended_at: <ISO-8601>
- exit_code: <0 = success; non-zero captured for forensics>
- output_path: <path the Skill produced>
- next_action: <what the orchestrator plans to do next>
```

## Last action / next action (rolling summary)

```
last_action: <one-line description of the most recent Skill call>
next_action: <one-line description of what the orchestrator will do next>
blockers: <empty list, or list of unresolved blockers>
```

## Terminal verdict

```
### [TERMINAL] <DONE|RECOVERY_REQUIRED|USER_MERGE_REQUIRED>
- entered_at: <ISO-8601>
- reason: <one-line why this terminal>
- recovery_action: <if RECOVERY_REQUIRED, what the operator should try>
- user_action_required: <if USER_MERGE_REQUIRED, what the operator must do>
```

## Reviewer signature convention

For every gate the user crosses (Approve or Edit-then-approve), Ralph
records the SHA-256 of the user's reply text as `reviewer_signature`.
This is forensics-only — Ralph does NOT verify the signature; it just
records it. The on-disk state file (`.dev-kit/ralph/<session>.json`)
holds the canonical record.

## What this template is NOT

- It is NOT the canonical state. The canonical state is
  `.dev-kit/ralph/<session>.json` and `lib/ralph_state.py` is the
  authoritative mutator. This template is an operator-readable view.
- It is NOT a replacement for `templates/progress.log.md`. The cross-
  task log records per-session progress across many Ralph runs; this
  template records per-run detail for a single run.
- It is NOT a queue. Ralph does not run multiple ideas in parallel.

## Copy pattern (build step)

At the build step, `skills/ralph/scripts/ralph_drive.sh` copies this
template to the per-step worktree alongside the other long-running
templates:

```bash
mkdir -p .worktrees/<phase>-step<N>/templates
cp -u templates/init.sh templates/feature_list.json \
   templates/progress.log.md templates/session_handoff.md \
   templates/ralph_progress.md \
   .worktrees/<phase>-step<N>/templates/
```
