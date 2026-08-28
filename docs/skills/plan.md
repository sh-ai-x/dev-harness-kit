> [← Skills index](README.md) · [Project README](../../README.md)

# `plan`

**Category:** `plan` · **Alpha:** `state` · **Invocation:** `/dev-kit:plan` (human-invoked)

`plan` converts a 1-line idea into `PRD.md` plus `phases/<name>/{index.json, step<N>.md}` through 5 gates run inside a single Ralph loop. It replaces what used to be an 8-gate structure (frame → evidence → diff → non-goals → socratic → phase-decompose → seed-convergence → prd-writer): several of those gates asked overlapping "is this worth building?" questions, so they were collapsed into one quantified `validate` gate. The former `plan-ralph` sub-skill was also absorbed (issue #58) — `plan` is fully self-contained, with no delegated sub-skill invocation. The skill produces planning artifacts only: no code, build, or deploy output, even if the user says "just write the code."

## When to use it

- The user types `/dev-kit:plan` with an idea.
- The user wants the PRD regenerated.
- Resuming from `.dev-kit/decision-log.md` after a HOLD pause (the cap on gate 2's ambiguity loop was hit without convergence).

## How it works

### Worktree precondition (fail-closed)

Before Gate 1 is asked, the skill reads `./.git` to detect whether the cwd is a worktree or the main checkout (it uses `Read` instead of `Bash` because `plan`'s `disallowed-tools` includes `Bash`). If `./.git` is a file starting with `gitdir:`, the cwd is a worktree and Gate 1 proceeds. If `./.git` is a directory (the main checkout), the skill stops before asking Gate 1 and tells the parent agent to cut a worktree first — proceeding anyway would let `hooks/worktree-guard.sh` block every `Write`, so gate answers would be captured but `PRD.md` would never be emitted, and the failure would only surface later when `/dev-kit:build` fails on a missing phase.

### 5 gates, 1 Ralph loop

```
[1/5] frame        — goal + target user + 1-line situation
       ↓
[2/5] validate     — evidence (>=3 sources) + value_score + ambiguity loop
       ↓
[3/5] non-goals    — 3+ non-goals with rationale + breach-response
       ↓
[4/5] decompose    — phases/<name>/index.json + step<N>.md (per-step status)
       ↓
[5/5] emit         — PRD.md 6-section DoD pass + hand-off
```

**Gate 1/5 — frame.** Asks, in one message: goal (one sentence, what ships and what changes for the user), target user (one named persona, not "everyone"), situation (one sentence, where the user is today). Empty fields are asked once more, then filled with `"<unspecified>"`. All 3 fields are written to `.dev-kit/decision-log.md` under `# frame`.

**Gate 2/5 — validate.** Three numeric inputs feed one composite convergence test:
- *Evidence*: ask once for ≥3 independent signals (`{source, claim, date}`) that the target user wants this. Fewer than 3 fails the gate — no "sharpen once" retry, count gates this input.
- *Value score*: computed, not asked — `value_score = (LTV_per_user × reachable_users_year1) / total_cost`. Threshold `value_score >= 3.0`; below that, the gate fails and the skill names the single biggest lever to close the gap (cheaper / more reachable / higher LTV — one, not a list).
- *Ambiguity loop (0-10)*: starts at 10. Each iteration asks exactly one question targeting the highest-leverage unknown (knobs: user, pain, scope, metric, kill — each worth -1 to -3 on the score), then re-scores. The re-score must be lower than the previous iteration (`narrowed_delta`); two identical scores in a row (`dedup_metric: identical-ambiguity-cycle=2`) breaks the loop early as "best effort."
- *Convergence test*: PASS iff `evidence_count >= 3 AND value_score >= 3.0 AND ambiguity_score <= 3`. On FAIL, loop on the failing dimension, capped at `safety_valve=8` iterations. On cap with no pass, write `"status": "held"` to `loop-log.json`, surface the remaining gap, and do not auto-emit `PRD.md`.

**Gate 3/5 — non-goals.** Ask once for 3 things the PRD will NOT do, each with a rationale and a breach-response (what happens if a reviewer asks to add it back). Fewer than 3 → the skill generates candidates from the decision log and asks the user to confirm or replace. Written to PRD.md §3.

**Gate 4/5 — decompose.** Emits `phases/<name>/index.json` (one step = one shippable, dependency-ordered layer). The top-level `worktree` field carries the branch base each per-step worktree derives from (`<branch-base>-step<N>`), conventionally `<prefix>-<phase>` where `<prefix>` follows the worktree-cut convention (e.g. `plan/plugin-harness-v3`); if absent, the build runner defaults to `feat/<phase>` as a defense-in-depth fallback, not the contract. For each step: `lib/execute.py:register_step()` creates the index.json entry as `status="unimplemented"`, then the skill writes `phases/<name>/step<N>.md` from the pinned template (Status / Read first / Task / Acceptance Criteria / Verification & Status Update / Don't) — plan only writes `Status: pending`; the runner and the executing sub-agent own the rest of the lifecycle. Per-step status values are the SSOT `lib/execute.py:VALID_STATUSES`; plan only ever writes `unimplemented` and `pending` — runtime states (`in_progress`, `completed`, `error`, `blocked`) belong to the harness-runner.

The step file's `Verification & Status Update` section ends with two mandatory HTML-comment markers (`<!-- status: completed|error|blocked -->` and a matching `<!-- summary/error_message/blocked_reason: ... -->`) — this is the plan↔build SSOT the build runner's parser reads; if missing or malformed, the runner falls back to the index.json status.

**Machine-executable verification (`lib/verify_harness.py`).** A step declares the commands the harness-runner will run to decide `completed` vs. `error` — never the free-text AC prose alone. As of the initial `lib/verify_harness.py` PR this field is parsed and executable but **not yet wired into `lib/execute.py`** — the runner still decides `completed` on subprocess exit-0 alone until the follow-up PR lands the `_verify_and_retry` gate (see `docs/proposals/verification-harness/harness-design.yaml`). Declaring `verification` today has no runtime effect beyond making the step's intent machine-parseable. Precedence: an `index.json` step field `verification` (`str | list[str]`, e.g. `"verification": "pytest tests/test_foo.py -q"` or `"verification": ["pytest tests/test_foo.py -q", "ruff check lib/foo.py"]`) wins over a fenced code block placed under the step.md `Verification & Status Update` heading (each non-comment, non-blank line in the fence is one command). A step with neither source declares `[]` and the gate is a no-op — existing phases predating this field keep their current behavior unchanged. `lib/verify_harness.py:parse_verification()` resolves the precedence; `run_verification()` executes each command with `shlex.split` (no `shell=True`) in the step's worktree and captures exit code + pytest `N passed`/`M failed` counts as evidence.

**Gate 5/5 — emit.** Writes `PRD.md` with 6 sections, gated on 5 DoD conditions: §1 Frame verbatim from Gate 1; §2 Validate showing `value_score >= 3.0` and `ambiguity_score <= 3` (or `status: held`, in which case the skill stops and asks the user); §3 Non-goals with ≥3 rationale+breach-response entries; §4 Phase plan pointing at `phases/<name>/index.json` listing every step title; §5 AC list (1-5 items) mapping 1:1 to step AC commands; §6 Hand-off naming `/dev-kit:build` as next. The skill then appends the final cycle to `.dev-kit/loop-log.json`, writes `.dev-kit/hand-off/plan→build.md`, and auto-renders the design proposal (see below).

### Proposal auto-invoke

Gate 5/5's final step calls the `proposal` skill so the design record is materialized before `/dev-kit:build` runs, making the chain **plan → proposal → build**. The topic slug is `<main>/<sub>`: `<main>` is the umbrella (hardcoded to `harness-architecture` for this project); `<sub>` is the phase directory name from Gate 4/5 — one name shared by the phase directory, the proposal sub-topic, and the worktree branch base's `<phase>` segment. The skill writes `docs/proposals/<bucket>/<main>/<sub>.yaml` (each PRD § becomes one proposal section; frontmatter status is `design-discussion`, auto-routes to the `review/` bucket) and invokes `Skill("proposal", topic="<main>/<sub>")` — `plan`'s `disallowed-tools: Bash` does not block this since `Skill` is a separate tool. If the sub-topic slug is malformed, or the file already exists with different content, the proposal skill refuses and Gate 5/5 surfaces the conflict for the user to resolve.

## Usage

```bash
/dev-kit:plan
```

0-arg — the idea is supplied as the user's prompt text, not a flag.

## Output

- `PRD.md` — the 6-section plan.
- `phases/<name>/index.json` — the phase state machine.
- `phases/<name>/step<N>.md` — one per step.
- `.dev-kit/decision-log.md` — accumulated Q&A and score deltas (cumulative across iterations).
- `.dev-kit/loop-log.json` — narrowing per cycle (MUST-16).
- `.dev-kit/hand-off/plan→build.md`.
- `docs/proposals/<bucket>/<main>/<sub>.{yaml,html}` — the auto-rendered design record (bucket auto-routed from YAML `status:`).

## Related

- [proposal](proposal.md) — auto-invoked at Gate 5/5 to render the design record; see its "Authoring a proposal" section for the YAML shape this skill emits.
- `/dev-kit:build` — the named next stage; consumes `phases/<name>/step<N>.md` via the harness-runner.
- `lib/execute.py` — owns `register_step`, `VALID_STATUSES`, `update_step_status`, and `parse_status_marker()`.
- `rules/git-workflow.md` — the worktree-cut convention referenced by Gate 4/5's `worktree` field.

---
*Source: [`skills/plan/SKILL.md`](../../skills/plan/SKILL.md)*
