---
name: worktree-prune
category: shortcuts
description: 0-arg interactive prune of stale worktrees. Counts registered worktrees, lists them oldest-first by branch-tip age, asks how many to remove, then dispatches `bin/worktree-remove-safe.sh` per row after a y/N gate.
alpha: state
when_to_use:
  - User types /dev-kit:worktree-prune
  - User types "clean up old worktrees" / "prune stale worktrees"
  - After `worktree-janitor` agent reports a batch as `safe-to-remove`
  - For single-named cleanup that needs survivor analysis, use `/dev-kit:prune --target <feature>`
allowed-tools: Read Bash
disallowed-tools: Edit Write WebFetch Agent
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:worktree-prune — interactive stale-worktree cleanup

Interactive shell wrapper over `bin/worktree-prune.sh`. The skill owns a
small interactive state machine (count → audit table → N selection →
preview → y/N gate → safe-remove loop → report); the script is the
contract. Excludes the main checkout and detached-HEAD worktrees —
those need human judgment, not bulk removal.

## What it does

Forwards to `bin/worktree-prune.sh`, which:

1. Reads `git worktree list --porcelain` and `git for-each-ref refs/heads/`
   in two git calls (regression for the N-spawns perf issue).
2. Sorts removable candidates oldest-first by branch-tip committer epoch.
3. Renders a fixed-width table (no ANSI) with `# / AGE(d) / BRANCH / PATH`.
4. Resolves the target count from positional N, `--keep N`, or an
   interactive prompt.
5. Renders the would-be-removed slice in the same format as the audit
   table, prompts `Proceed? [y/N]`, then per row invokes
   `bin/worktree-remove-safe.sh <path>` (per-worktree log archive,
   issue #689 Phase 2).

## Flags

| Flag | Effect |
|---|
| positional `N` | Remove the N oldest worktrees, then confirm |
| `-y, --yes` | Skip the final y/N gate (CI / batch mode) |
| `-n, --dry-run` | Print what would be removed; never mutate |
| `-k, --keep N` | Prune to N newest (equivalent to removing `TOTAL-N` oldest) |
| `--except-self` | Exclude the worktree the script is currently running from; combine with `-y` to nuke every other worktree |
| `-h, --help` | Print full usage and exit 0 |

Positional N and `--keep` are mutually exclusive. `--except-self` is the
preferred way to do bulk cleanup when the operator's running session
must stay alive (the typical "cleanup all my old worktrees" pattern is
`/dev-kit:worktree-prune --except-self -y`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Removed (or nothing selected, or dry-run completed) |
| 1 | Invalid CLI / runtime error |
| 2 | User aborted at the y/N gate |
| 3 | At least one removal failed (partial success — remaining worktrees printed) |

## Examples

```
/dev-kit:worktree-prune                # interactive: "how many?"
/dev-kit:worktree-prune 5              # remove the 5 oldest, then confirm
/dev-kit:worktree-prune -y 10          # remove 10 oldest, no prompt
/dev-kit:worktree-prune --keep 3       # prune to 3 newest
/dev-kit:worktree-prune -n 5           # preview the 5 oldest, no changes
/dev-kit:worktree-prune --except-self -y   # nuke every worktree except this one
```

## Hand-off

No hand-off. Pure utility — invoked when the operator judges the audit
table warrants action. For read-only classification (`safe-to-remove` vs
`needs-human-check`), use the `worktree-janitor` agent instead.

## Related

- [`commands/worktree-prune.md`](../../commands/worktree-prune.md) — slash dispatch + user-facing argument hint
- `bin/worktree-prune.sh` — CLI parsing, interactive prompts, dispatch contract
- `lib/worktree_prune.py` — deterministic half (porcelain parse, epoch map, table sort)
- `bin/worktree-remove-safe.sh` — per-worktree safe-removal wrapper (log archive runs first)
- `tests/test_worktree_prune.py` — 14 hermetic tests covering Row age math, table rendering, end-to-end `collect`, and all three CLI modes (JSON / `--table` / `--count`)