# AI Suggestion Workflow

`/dev-kit:ralph` (`skills/ralph/SKILL.md`) is dev-kit's end-to-end autonomous loop: 4 user gates (`RESEARCH_GATE` → `PROPOSAL_GATE` → `PLAN_GATE` → `SHIP_CONFIRM_GATE`) plus an attended run, all driven by a state machine in `lib/ralph_chain.py`. The complaint that motivates this doc: humans lose the plan mid-stream — Ralph works unattended, decisions get buried in the AI log, and only the final PR surfaces back to a human. The fix is not a new state machine, a new gate, or a new ask quota. The fix is keeping a visualization in front of the operator at every moment a decision might change.

The **AI Suggestion Workflow** is the pattern that does that. The visualization source is the global [`archify`](https://github.com/tt-a1i/archify) skill at `~/.agents/skills/archify/SKILL.md`. dev-kit's contribution to the chain is a thin dispatch wrapper, [`/dev-kit:archify`](skills/archify/SKILL.md), that other dev-kit workflows call without importing archify's prompt body. Two intervention surfaces — mid-stream decision changes, and the final PR review — get a concrete image; everything else stays in prose.

## The chain at a glance

```mermaid
flowchart TD
    R[/dev-kit:ralph &lt;idea&gt;/]
    R --&gt; G1[RESEARCH_GATE<br/>visualize: archify workflow research]
    G1 --&gt; G2[PROPOSAL_GATE<br/>visualize: archify workflow proposal]
    G2 --&gt; G3[PLAN_GATE<br/>visualize: archify workflow plan]
    G3 --&gt; G4[SHIP_CONFIRM_GATE<br/>visualize: archify workflow ship-confirm]
    G4 --&gt; A[ATTENDED_RUN<br/>visualize on mid-stream decision change]
    A --&gt;|mid-stream decision| DV[/dev-kit:archify &lt;type&gt; &lt;subject&gt;/]
    DV --&gt; A
    A --&gt; T[terminal: USER_MERGE_REQUIRED]
    T --&gt; PR[final PR review<br/>visualize: archify architecture &lt;feature&gt;]
```

The `DV` self-loop on `ATTENDED_RUN` is the mid-stream intervention surface — the only place the chain pulls the operator in for human eyes without breaking the `attended_lock` invariant (an archify emit is not an `AskUserQuestion`).

## Where the chain slots in

| Ralph gate / state                | Visualization trigger                          | What the operator sees                                |
|-----------------------------------|------------------------------------------------|-------------------------------------------------------|
| `RESEARCH_GATE`                   | `/dev-kit:archify workflow research`           | A diagram of where the idea lands in the codebase     |
| `PROPOSAL_GATE`                  | `/dev-kit:archify workflow proposal`           | The before/after shape of the proposed change        |
| `PLAN_GATE`                      | `/dev-kit:archify workflow plan`               | The full plan topology, including all phase edges    |
| `SHIP_CONFIRM_GATE`              | `/dev-kit:archify workflow ship-confirm`       | The chain summary: gates hit, decisions raised       |
| `ATTENDED_RUN` (mid-stream)      | `/dev-kit:archify <type> <subject>`            | The single decision moment that needs human eyes     |
| `USER_MERGE_REQUIRED` (terminal) | `/dev-kit:archify architecture <feature>`       | The final change as a system diagram                  |

Each row maps to one explicit `/dev-kit:archify` invocation. The skill does not decide on its own when to fire — the parent workflow owns the gate logic.

## Tool references

The chain references six external / cross-plugin tools. Each is consumed in the diagram order above; none is re-implemented by `/dev-kit:archify`.

- **`/dev-kit:ralph`** — [`skills/ralph/SKILL.md`](skills/ralph/SKILL.md) — the autonomous orchestrator that owns the gates and `attended_lock`.
- **`/dev-kit:archify`** — [`skills/archify/SKILL.md`](skills/archify/SKILL.md) — the new dev-kit dispatcher (this PR's other deliverable).
- **`archify` (global)** — `~/.agents/skills/archify/SKILL.md` — the actual diagram engine; runs `validate → deliver → check → browser-check` via `node bin/archify.mjs finalize`. The dev-kit skill calls into this one and never re-implements it.
- **[painhardcore/pstack](https://github.com/painhardcore/pstack)** — design-time rigor. `pstack:architect` sketches types / signatures / module structure before code; `pstack:poteto-mode` applies rigorous engineering discipline to the implementation. Both are useful upstream of the visualization emit, so the operator can review a topology that already has a defended design behind it.
- **`ouroboros`** — `~/.agents/skills/ouroboros/` (local install). `ouroboros:interview` crystallizes vague requirements before `RESEARCH_GATE`; `ouroboros:run` is the alternative entry point for the autonomous run if ralph is not the chosen orchestrator.
- **`mattpocock-skills`** — `~/.agents/skills/mattpocock-skills/` (local install). `mattpocock-skills:tdd` enforces red-before-green so the visualized plan has tests behind it; `mattpocock-skills:code-review` runs standards + spec-fidelity review at `SHIP_CONFIRM_GATE`.

## When NOT to add a visualization

The ponytail rule applies. Do not emit an archify diagram when:

- The PR is a one-line YAML change with no topology implications.
- The operator's next decision is purely textual (e.g. "which name do we pick?").
- A previous emit in the same gate still reflects the current state (re-emit only after a real change).
- The visualization would re-render mid-decision and freeze the operator's context.

The rule of thumb: **emit when the operator's next decision depends on seeing the topology**, not when emitting is easy. `docs/workflow/WORKFLOW-SCENARIOS.md` covers the broader "what to do when the flow doesn't go straight through" cases.

## See also

- [`skills/ralph/SKILL.md`](skills/ralph/SKILL.md) — the autonomous orchestrator this pattern wraps around.
- [`skills/archify/SKILL.md`](skills/archify/SKILL.md) — the dev-kit dispatcher this pattern invokes.
- `~/.agents/skills/archify/SKILL.md` — the global archify skill that owns the actual rendering.
- [`docs/architecture/visualization.md`](architecture/visualization.md) — sibling doc on dev-kit's own diagram conventions (`/dev-kit:code-viz`, per-skill Mermaid); archify is a different engine.
- [`docs/workflow/WORKFLOW-SCENARIOS.md`](workflow/WORKFLOW-SCENARIOS.md) — companion doc for "flow doesn't go straight through" cases.
