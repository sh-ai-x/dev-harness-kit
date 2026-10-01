---
name: archify
category: design
description: 0-arg diagram chain. Dispatches to the global `archify` skill for visualization; mirrors harness-lite's flow-feature pattern.
alpha: state
when_to_use:
  - User types /dev-kit:archify <type> <subject>
  - User wants an archify visualization in front of an autonomous loop
  - User wants a chain skill that calls into the global `archify` skill (NOT a re-implementation)
allowed-tools: Read Bash Skill
disallowed-tools: Write Edit
model: sonnet
disable-model-invocation: false
user-invocable: true
---

> [← Skills index](../../README.md)

# /dev-kit:archify

`/dev-kit:archify` is a thin dispatch wrapper. It calls into the already-installed global [`archify`](https://github.com/tt-a1i/archify) skill at `~/.agents/skills/archify/SKILL.md` and surfaces the rendered HTML path back to the parent workflow. **It does not re-implement diagram generation** — `finalize` (validate → deliver → check → browser-check) is owned by the global skill, and writing inside `.archify/` is its exclusive territory.

This skill exists so that workflow-shaped skills (`/dev-kit:ralph`, `/dev-kit:build`) can request a visualization at a decision moment without importing archify's prompt body or hand-rolling its `bin/archify.mjs finalize` invocation. The chain shape mirrors `harness-lite/skills/flow-feature/SKILL.md`.

## What it does

1. `Skill("archify", "<type> <subject>")` — dispatch to the global `archify` skill. `<type>` is one of `architecture` / `workflow` / `sequence` / `dataflow` / `lifecycle` per archify SKILL.md; `<subject>` is the prose or pasted Mermaid source. The global skill picks the `.archify/<type>-<slug>-<YYYYMMDD-HHMMSS>/` folder, writes the typed `candidate.json`, and runs:
   ```bash
   node bin/archify.mjs finalize <type> <candidate.json> <output.html> --quality showcase --json
   ```
2. Read the `finalize` receipt; surface the compact stdout summary back to the caller. On non-zero exit, hand off to archify's repair path (archify SKILL.md) — do not re-implement repair here.
3. Print the absolute HTML path so the parent workflow (e.g. `/dev-kit:ralph`) can surface it as the visualization that gates the next user approval.

## What it does not do

- Does not call `bin/archify.mjs` directly. The chain dispatches to the global `archify` skill; this skill never edits `.archify/*` itself.
- Does not render to a non-archify output (no Mermaid emit, no PNG direct, no SVG inline). One entrypoint, one receipt shape.
- Does not skip the `finalize` gate (validate + deliver + check + browser-check) — if `finalize` fails, hand back to archify's repair path; do not publish a half-rendered diagram.

## When to invoke

```text
/dev-kit:archify <type> <subject>
```

Reach it directly when the visualization is the goal. Reach it via `/dev-kit:ralph` (gate visualization at RESEARCH_GATE / PROPOSAL_GATE / PLAN_GATE / SHIP_CONFIRM_GATE + mid-stream decision change) or via `/dev-kit:build` (phase-decision visualization) when called as a chain step. See [`docs/ai-suggestion-workflow.md`](../../docs/ai-suggestion-workflow.md) for the slot-in pattern.

## Acceptance criteria

- [ ] The skill called `Skill("archify", ...)` exactly once; no direct `bin/archify.mjs` invocation.
- [ ] `finalize` exited 0 before the chain reported done; `finalize-summary.json` is the source of truth.
- [ ] The absolute HTML path was printed and handed back to the parent workflow.
- [ ] No file under `.archify/` was written by this skill itself — only by the dispatched `archify` skill.

## References

- `~/.agents/skills/archify/SKILL.md` — the global archify skill this chain dispatches into.
- [painhardcore/pstack](https://github.com/painhardcore/pstack) — design-time architecture sketch (`pstack:architect`) and rigorous implementation (`pstack:poteto-mode`), referenced by [`docs/ai-suggestion-workflow.md`](../../docs/ai-suggestion-workflow.md).
- `skills/ralph/SKILL.md` — primary consumer as a gate visualization in the autonomous loop.
- `harness-lite/skills/flow-feature/SKILL.md` — the chain-skill pattern mirrored here.
- [`docs/ai-suggestion-workflow.md`](../../docs/ai-suggestion-workflow.md) — the workflow doc that motivates this skill.
- [`docs/architecture/visualization.md`](../../docs/architecture/visualization.md) — sibling doc on dev-kit's own diagram conventions (for `/dev-kit:code-viz` et al.); archify is a different engine.

## Next step

After `/dev-kit:archify` renders, the next skill is whatever the parent workflow dictates — `/dev-kit:ralph` re-enters at SHIP_CONFIRM_GATE, `/dev-kit:build` proceeds to the next `step<N>.md`.
