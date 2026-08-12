> [← Skills index](README.md) · [Project README](../../README.md)

# `evidence-plan`

**Category:** `design` · **Alpha:** `state` · **Invocation:** `/dev-kit:evidence-plan <idea>` (human-invoked)

`evidence-plan` moves the human review checkpoint earlier. `/dev-kit:plan`'s Gate 5/5 already auto-renders a proposal, but only after the expensive Gate 4 (decompose — full phase + step-file generation) has already run. `evidence-plan` gathers cited research and renders a lightweight proposal FIRST, stops for human confirmation, and only then hands off to `/dev-kit:plan`'s unmodified 5-gate loop — so a rejected idea costs one research pass and one proposal render, not a full PRD.

## When to use it

- The user types `/dev-kit:evidence-plan <idea>`.
- An idea needs cited evidence and a human-reviewable proposal before committing to the full PRD process.

## How it works

Three phases, in order. Each phase's output gates the next; the skill never invokes `/dev-kit:build` and never writes source code (`allowed-tools: Read Write Glob Grep Skill`; `Edit`/`WebFetch`/`NotebookEdit`/`Bash` disallowed).

1. **Research** — `Skill("research", <idea>)`. Produces cited evidence (every claim carries `url` + `fetched_at` + `source_type`, or is flagged `[UNCITED]`).
2. **Proposal** — writes `docs/proposals/<main>/idea-<slug>.yaml` (before/after + pros/cons/limitations, seeded from the research output), then `Skill("proposal", "<main>/idea-<slug>")` to render `docs/proposals/<main>/idea-<slug>.html`. `evidence-plan` itself disallows `Bash`, but `/dev-kit:proposal` renders via its own `Bash` grant in its own frontmatter — a called skill's tool grants are evaluated against its own frontmatter, not the caller's. **The skill stops here** and tells the user to open the HTML and confirm before continuing.
3. **Plan hand-off** — once confirmed, `Skill("plan", <idea>)`. Plan runs its own unmodified 5-gate loop and, at Gate 5/5, renders its own proposal under the phase-name slug (`<main>/<phase>`) — a separate, later "final design record" distinct from this skill's earlier "idea pitch" proposal at `<main>/idea-<slug>`. Collision is prevented by construction: `/dev-kit:proposal` refuses to overwrite an existing file with different content (it has no overwrite flag), so a phase later named `idea-<slug>` would surface a conflict for the user to resolve rather than silently overwriting the earlier proposal.

## Output

- `docs/proposals/<main>/idea-<slug>.yaml` + `.html` (Phase 2, this skill's own artifact).
- `PRD.md` + `phases/<name>/` + `docs/proposals/<main>/<phase>.html` (Phase 3, owned by `/dev-kit:plan`, produced after hand-off).

## Related

- [research](research.md) — Phase 0-3 escalation engine invoked in Phase 1.
- [proposal](proposal.md) — HTML renderer invoked in Phase 2; its refuse-to-overwrite contract is what makes Phase 2/3 slug collision impossible by construction.
- [plan](plan.md) — owns everything past the Phase 3 hand-off, including its own Gate 5/5 proposal render.

---
*Source: [`skills/evidence-plan/SKILL.md`](../../skills/evidence-plan/SKILL.md)*
