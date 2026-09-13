# DEV-HARNESS CORE DIRECTIVE

## 1. Operating Principle

The harness is an autonomous end-to-end software engineering system. It works
through bounded loops from intent to verification while preserving the
repository's safety and evidence boundaries.

This document is declarative. It describes intent, authority, and routing; it
does not copy a hook algorithm, state machine, or child-skill safety valve.

## 2. Hard Invariants (Enforced via External Hooks)

- **Git Safety:** never commit directly to `main` or edit outside a designated
  worktree; [`rules/git-workflow.md`](../../rules/git-workflow.md) and the
  worktree guard enforce this boundary.
- **Verification:** never mark a task done without acceptance evidence; the
  verification hooks and child-skill gates enforce this boundary.
- **Context Hygiene:** do not dump whole files or broad search output into a
  handoff; use bounded artifact references and summaries.

## Authority boundary

The following sources are the enforcement plane and remain authoritative:

- [`iron-laws/index.md`](../../iron-laws/index.md) — non-negotiable safety,
  verification, and evidence invariants.
- [`hooks/index.md`](../../hooks/index.md) — hook ownership and active-hook
  state.
- [`rules/git-workflow.md`](../../rules/git-workflow.md) — worktree, branch,
  hand-off, commit, and PR boundaries.
- [`docs/scopes/modes.md`](../scopes/modes.md) — mode and scope resolution.

The prompt must not weaken or restate those enforcement rules. A route can
reference a guard decision, but it cannot authorize a tool call, bypass a
guard, approve missing evidence, change mode, or merge a PR.

## Declarative execution contract

`/dev-kit:do` is the single user-facing entrypoint for an end-to-end request.
It resolves one route and passes a versioned envelope to the existing owner.
The owner keeps its own workflow, loop, verification, and safety policy.
Ralph may sequence those owners, but does not replace their internal loops.

The route envelope is immutable for its `run_id` and carries:

```text
request_id, intent_ref, directive_ref, directive_version,
route_id, route_version, owner, scope, worktree, mode,
policy_version, team_toggle, context_budget
```

`mode` is exactly one of `full`, `lite`, or `undev`. `team_toggle` is a
separate `on` / `off` value resolved from the existing team configuration; it
is never a mode and must not be encoded into the mode value.

If a critical specification is absent or route ownership is ambiguous, the
envelope terminates as `HOLD` / `NEEDS_HUMAN`. The router does not infer a
route from incomplete intent.

## 3. Autonomous Execution Protocol

For a `/do` request, the harness follows this compact protocol:

1. Analyze and scope the request; for non-trivial work, route to the existing
   planning owner instead of inventing a second plan format.
2. Execute through the selected owner, which owns its edits, tests, and
   self-correction loop.
3. Verify and deliver the owner's evidence and status; do not relabel a hold,
   recovery, or missing-evidence result as completion.

## 4. Context Budget & Tool Usage Strategy

Prefer CLI/static configuration and lazy-loaded references when the selected
owner supports them. Keep the route envelope and handoff step-by-step,
bounded, and focused on the requested change.

### Context Diet

Skill handoffs carry references and bounded evidence, not prompt history. When
available, use [`lib/context_budget.py`](../../lib/context_budget.py)'s
`build_handoff()` and `validate_handoff()` rather than creating another
context limiter. Its allowed `context_mode` values are:

- `artifact_ref` — required artifacts are referenced by stable path or id.
- `summary_ref` — a compact redacted summary points to the source artifact.
- `minimal_inline` — only the smallest required inline fact is carried.

The first-rollout limits are 4 KiB per stdout/stderr excerpt, 2 KiB per child
summary, 32 artifact references per handoff, and 16 KiB per event payload.
Prompts and full transcripts are never persisted or re-injected into a child
or resumed attempt. A fingerprint is opaque join metadata only.

Provider usage is recorded when available. Missing usage is
`INSUFFICIENT_EVIDENCE`, not zero. Context metrics may describe a run, but
cannot turn incomplete telemetry into approval or success.

## Delivery contract

The router reports `HOLD`, `DELEGATED`, or the owner's terminal status. It
must preserve the owner's evidence references and real verification result;
it must not claim completion from a prose response alone.
