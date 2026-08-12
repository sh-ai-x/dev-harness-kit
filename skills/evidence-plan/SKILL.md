---
name: evidence-plan
category: design
description: Idea → cited research → HTML proposal (human confirms) → /dev-kit:plan hand-off. The proposal is rendered and reviewed BEFORE the expensive 5-gate PRD work runs, not after.
alpha: state
when_to_use: |
  - User types /dev-kit:evidence-plan <idea>
  - An idea needs cited evidence and a human-reviewable proposal before committing to a full PRD
allowed-tools: Read Write Glob Grep Skill
disallowed-tools: Edit WebFetch NotebookEdit Bash
model: opus
disable-model-invocation: false
user-invocable: true
safety:
  safety_valve: 3
  convergence: proposal.html rendered and user confirmation received before Skill("plan", ...) fires
  dedup_metric: same-phase-repeat=2
  user_interrupt: true
---
> [← Skills index](../../README.md)

## What it does

Three phases, in order. Each phase's output gates the next; the skill
never invokes `/dev-kit:build` and never writes source code.

1. **Research** — `Skill("research", <idea>)`. Produces cited evidence
   (every claim carries `url` + `fetched_at` + `source_type`, or is
   flagged `[UNCITED]`).
2. **Proposal** — writes `docs/proposals/<main>/idea-<slug>.yaml`
   (before/after + pros/cons/limitations, seeded from the research
   output), then `Skill("proposal", "<main>/idea-<slug>")` to render
   `docs/proposals/<main>/idea-<slug>.html`. **Stop here.** Tell the
   user to open the HTML and confirm before continuing — do not
   proceed to Phase 3 without an explicit go-ahead.
3. **Plan hand-off** — once confirmed, `Skill("plan", <idea>)`. Plan
   runs its own unmodified 5-gate loop and, at Gate 5/5, renders its
   own proposal under the phase-name slug (`<main>/<phase>`) — a
   separate, later "final design record" distinct from this skill's
   earlier "idea pitch" proposal at `<main>/idea-<slug>`. Slugs never
   collide because of the `idea-` prefix.

## Why this order

`/dev-kit:plan`'s Gate 4 (decompose) is the expensive step — full
phase + step-file generation. Reviewing a lightweight proposal before
that work runs, instead of after, means a rejected idea costs one
research pass and one proposal render, not a full PRD.

## Next step

`/dev-kit:plan` (invoked internally via `Skill("plan", ...)` once the
user confirms the Phase 2 proposal). This skill's job ends at the
Phase 3 hand-off.
