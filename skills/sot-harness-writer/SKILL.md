---
name: sot-harness-writer
category: design
description: "Interview-based Single Source of Truth harness document writer (5 rounds × 2-3 evidence-backed recommendations, full traceability, hands off to /dev-kit:plan)."
alpha: state
when_to_use:
  - User types /dev-kit:sot-harness-writer <1-line-idea>
  - User wants a research-backed harness design for a new project
  - User wants to retro-document an existing harness with traceable decisions
  - Reviewer wants the audit trail of "why this pattern, not that one"
allowed-tools: Read Write Glob AskUserQuestion
disallowed-tools: Bash Edit NotebookEdit WebFetch
model: opus
disable-model-invocation: true
user-invocable: true
---
> [← Skills index](../../README.md)

Self-contained. Runs 5 interview rounds (one per SOT dimension: project
context, verification, context, safety, lifecycle). Each round surfaces
2-3 evidence-based recommendations from the agent-harness-playbook
research; the user accepts/rejects/customizes. Output is a SOT
harness document at `.dev-kit/hand-off/sot-harness-<session>.md` that
flows into `/dev-kit:plan` (PRD emission) and then `/dev-kit:build`.

The 5 dimensions are derived from Martin Fowler / Böckeler's
"5-subsystem decomposition" of an agent harness
[src:https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html;ts:2026-08-04;type:primary]
and Anthropic's "Effective harnesses for long-running agents"
[src:https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents;ts:2026-08-04;type:primary].

## Iron Laws (Phase 6, MUST-19)

1. **5 rounds are mandatory.** Every interview must complete
   `project_context`, `verification`, `context`, `safety`, and
   `lifecycle`. Skipping a round = `status: held`.
2. **Each round surfaces at least 2 evidence-backed recommendations.**
   Bare questions without source citations are forbidden. Each
   recommendation cites the source URL + a one-sentence thesis.
3. **`safety_valve: 5` round cap.** At most 5 user turns per
   dimension. Cap reached without pass = `status: held`.
4. **`accept | reject | customize` only.** No "free-form answer that
   doesn't pick one of the surfaced options." Customization must name
   the rejected option and explain why.
5. **Every accepted recommendation is traceable.** The final SOT doc
   carries `(source: <url>)` next to every decision. No trace = the
   doc is invalid.

## Conversational loop (5 dimensions × 5 turns max)

```text
[1/5] project_context  — "What is your project's primary agent-harness category?"
[2/5] verification     — "How will you verify the agent's work?"
[3/5] context          — "How will you manage the context window?"
[4/5] safety           — "What safety perimeter?"
[5/5] lifecycle        — "What session lifecycle?"

For each round:
  - Show 2-3 recommendations from the playbook
  - Each recommendation: 1-line thesis + source URL
  - User picks: accept (A) | reject (R) | customize (C)
  - For C, ask "what would you change and why" (max 1 follow-up)
  - Lock the decision
```

## Acceptance rubric (must all clear)

| # | Question | Pass condition |
|---|---|---|
| A1 | All 5 dimensions have a locked decision? | Yes for all 5 |
| A2 | Every accepted recommendation cites a source? | 100% |
| A3 | Rejected recommendations have a reason? | 100% |
| A4 | "Open questions" section is non-empty? | Yes (or explicit "none") |
| A5 | Implementation phases are sequenced by dependency? | Yes |

If any criterion fails, the skill surfaces `status: held` and points
the user to the failing dimension.

## Inputs / outputs

- **Input**: 1-line idea (from `<idea>` if given, else from the user
  prompt) + session id (default = "default").
- **Output**:
  - `.dev-kit/hand-off/sot-harness-<session>.md` — the SOT document
    with all 5 dimensions, traceability, implementation phases. The
    file ALWAYS carries a YAML frontmatter (see "Output frontmatter"
    below) so the plan-skill hand-off consume gate can route it via
    `--from-sot` (issue #898).
  - `.dev-kit/decision-log-sot-harness/<session>.md` — every Q+A in
    order, including rejected recommendations and the user's reason.

### Output frontmatter

The SOT hand-off frontmatter is the typed discriminator
`lib.hand_off_consume.SOT_HANDOFF_KIND = "sot"`. The plan skill
MUST NOT consume a `sot-harness-*.md` file through the generic
interview path; it only accepts one via `--from-sot <path>`.

```yaml
---
handoff_kind: sot
status: locked          # locked = all 5 rounds accepted; held = validation failed
session_id: <sanitized session id>
generated_by: sot-harness-writer
---
```

` `status: held` is emitted when the decision set fails
`validate_sot_decision_set()` (e.g. a round was skipped, a `reject`
came without a reason, or a `customize` came without
`customize_text`). Re-run `/dev-kit:sot-harness-writer` to complete
the missing rounds.

## Post-interview handoff

After the SOT document is written, the skill suggests:

```bash
SOT doc written. To convert to a build-ready plan:
  /dev-kit:plan --from-sot .dev-kit/hand-off/sot-harness-<session>.md

To go straight to implementation:
  /dev-kit:build --from-sot .dev-kit/hand-off/sot-harness-<session>.md
```

The plan skill reads the SOT doc and emits PRD.md with phases. The
build skill reads the PRD and runs the per-step sub-agent delegation.

## Why this design

- **Interview + recommendations, not free-form**: every decision is
  anchored to a published pattern, so the SOT doc is auditable.
- **5 dimensions**: matches the canonical harness-engineering
  decomposition (instructions / state / verification / scope / lifecycle
  per walkinglabs and Fowler/Böckeler).
- **`accept | reject | customize`**: forces the user to engage with
  the tradeoff instead of accepting defaults blindly.
- **Source citation per decision**: enables the reviewer to verify
  the recommendation in 30 seconds.

## Next step

Hand the SOT document off to `/dev-kit:plan --from-sot` for PRD
emission, then `/dev-kit:build` for per-step implementation. See also:
`/dev-kit:interview` (5-field safety contract, precedes this skill),
`/dev-kit:plan`, `/dev-kit:build`, `lib/sot_harness_engine.py`, and
`agent-harness-playbook` (the source of all recommendations).
