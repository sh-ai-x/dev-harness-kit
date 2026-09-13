# `/dev-kit:do` — unified entrypoint

`/dev-kit:do` (also written `/do`) is the compact front door for a software
request. It resolves one owner, emits an auditable route envelope, and passes
the owner a bounded handoff. Existing skills, static hooks, Iron Laws, and the
git/worktree rules remain the enforcement plane.

## Usage

```text
/dev-kit:do <goal> — <scope> — verify with <acceptance signal>
```

Examples:

```text
/dev-kit:do add retry metrics to the Ralph report — skills/ralph — verify with pytest
/dev-kit:do diagnose the failing PR checks — PR 123 — verify with a green check run
/dev-kit:do review the staged change — current worktree — verify with a written review
```

The request must state the outcome, target scope, and a concrete verification
signal. Missing or ambiguous critical specifications produce `HOLD` /
`NEEDS_HUMAN`; `/do` does not infer a route or begin an interview from an
underspecified request.

## Route envelope

Each request gets one immutable, versioned envelope for its `run_id`:

| Field | Meaning |
|---|---|
| `request_id`, `intent_ref` | Stable request identity and opaque intent reference. |
| `directive_ref`, `directive_version` | Core Directive source and version. |
| `route_id`, `route_version`, `owner` | One registered owner and its route contract. |
| `scope`, `worktree` | Explicit target and resolved worktree reference. |
| `mode` | `full`, `lite`, or `undev`; no `team` value is valid here. |
| `team_toggle` | Independent `on` / `off` collaboration setting. |
| `policy_version` | Version of the referenced static SSOT boundary. |
| `context_budget` | Context schema, mode, and bounded payload limits. |

`undev` is fail-closed for `/do`: it returns `HOLD` with
`reason=mode_disabled`. `/dev-kit:mode` and `/dev-kit:team` remain the owners
of their respective configuration; routing never changes either setting.

## Ownership map

`/do` selects an existing owner and does not duplicate its algorithm:

| Request shape | Owner |
|---|---|
| cited evidence | `/dev-kit:research` |
| PRD or implementation plan | `/dev-kit:plan` |
| defect reproduction/root cause | `/dev-kit:build-debug` |
| implementation with an existing plan | `/dev-kit:build` |
| code/diff review | `/dev-kit:review` |
| security review | `/dev-kit:security` |
| open-PR repair loop | `/dev-kit:babysit-pr` |
| release/tag gate | `/dev-kit:ship` |
| explicitly end-to-end loop | `/dev-kit:ralph` using `route_id=ralph-attended` |

No match or multiple matches is a hold. A child owner's precondition or gate
still controls the request after delegation.

## Context Diet handoff

The handoff contains only an intent reference, checkpoint, latest failure
signature, required artifact references, and a compact redacted summary. Use
`lib/context_budget.py` when available; it defines the bounded handoff shape
and the only allowed modes:

- `artifact_ref`
- `summary_ref`
- `minimal_inline`

The initial caps are 4 KiB per stdout/stderr excerpt, 2 KiB per child summary,
32 artifact references per handoff, and 16 KiB per event payload. Full prompts,
transcripts, and message arrays are neither persisted nor re-injected. A
fingerprint is opaque metadata, not prompt content.

Token usage is measured when provider data exists. Missing usage is
`INSUFFICIENT_EVIDENCE`, not zero. Context metrics are diagnostic only: they
cannot approve a route, bypass a guard, or authorize merge.

## Result states

`/do` returns the envelope and one of:

- `HOLD` / `NEEDS_HUMAN` — critical input, route, mode, or evidence is not
  sufficient.
- `DELEGATED` — the owner accepted the bounded handoff; the owner's output is
  authoritative.
- the owner's terminal status — including recovery, guard-blocked, or human
  merge states, without relabeling them as success.

For a complex request, `/do` routes to the planning or Ralph owner that owns
the required artifacts. It does not create a parallel `index.json` planner,
copy child policies, or replay the full conversation.

## Next step

Provide any missing specification and invoke `/dev-kit:do` again after `HOLD`.
After `DELEGATED`, follow the selected owner's documented next step and use
its verification evidence for completion.
