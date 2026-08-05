# Cherry-pick memo template

> One-page memo per cherry-pick candidate. ≤ 30 lines. Verdict gates the merge.

**Slug**: `<NNN-kebab-slug>` (NNN = rank in BACKLOG.md)

## Pattern

What the reference project provides. Describe the pattern, not the vendor.

## Why dev-harness-kit cares

Concrete capability dev-kit would gain. Cite the closest existing dev-kit
surface (skill name, hook, file) that overlaps or is missing.

## Trade-offs

| What dev-kit gains | What dev-kit loses |
|---|---|
| ... | ... |

## Thin-harness gates

- **Gate 1 — Pros/cons**: this memo (filled).
- **Gate 2 — Net-negative-weight**: bytes added ≤ bytes removed.
  - Estimated bytes added: ___
  - Estimated bytes removed: ___
  - Net delta: ___
- **Gate 3 — tmux + long-running safety**: [pure-prose | hook | subagent | persistent-loop]
  - Verification command: ___

## Verdict

`adopt` | `defer` | `reject`

If `defer`: what's the unblock condition?
If `reject`: what's the next-best alternative within dev-kit?

## Re-validation at merge

The diff against the actual implementation must be re-checked at merge
time. If the diff invalidates any gate, the cherry-pick does not land.
