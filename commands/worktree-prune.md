---
description: Count worktrees, list them oldest-first, and remove the N oldest on demand.
argument-hint: '[N | --keep N | --dry-run | --yes]'
---

# /dev-kit:worktree-prune

Interactive worktree cleanup. Lists every registered worktree sorted
by branch-tip age, asks how many of the oldest to remove, and (after a
y/N gate) hands each one to `bin/worktree-remove-safe.sh` so the
per-worktree log archive (issue #689 Phase 2) runs first.

This is the mutating counterpart to the `worktree-janitor` agent
(read-only audit) — when the janitor reports a batch as
`safe-to-remove`, the operator can run this command to act on the
verdict in one shot.

## Args

| Position | Meaning | Default |
|---|---|---|
| `$1` (positional) | Number of oldest worktrees to remove | Prompted interactively |
| `-y, --yes` | Skip the final y/N gate | Off |
| `-n, --dry-run` | Show the would-be-removed list; never mutate | Off |
| `-k, --keep N` | Prune to N newest (equivalent to removing the `TOTAL-N` oldest) | Off |
| `-h, --help` | Print full usage and exit 0 | |

Positional N and `--keep` are mutually exclusive.

## Examples

```
/dev-kit:worktree-prune                # interactive: "how many?"
/dev-kit:worktree-prune 5              # remove the 5 oldest, then confirm
/dev-kit:worktree-prune -y 10          # remove 10 oldest, no prompt
/dev-kit:worktree-prune --keep 3       # prune to 3 newest
/dev-kit:worktree-prune -n 5           # preview the 5 oldest, no changes
```

## What it does

Forwards to `bin/worktree-prune.sh`, which:

1. Reads `git worktree list --porcelain` (one shot) and builds a
   `branch → committer-epoch` map via `git for-each-ref refs/heads/`
   (one shot). Excludes the main checkout and detached-HEAD worktrees
   (no branch → no stale-branch triage; the janitor agent handles
   those).
2. Sorts the candidates oldest-first and prints a numbered table with
   age in days, branch, and absolute path.
3. Resolves the target count from the positional arg, `--keep N`, or
   an interactive `read -p`.
4. Renders the would-be-removed slice in the same fixed-width format
   as the audit table, prompts `Proceed? [y/N]`, and on yes invokes
   `bin/worktree-remove-safe.sh <path>` for each row.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Removed (or nothing selected, or dry-run completed) |
| 1 | Invalid CLI / runtime error |
| 2 | User aborted at the y/N gate |
| 3 | At least one removal failed (partial success) |

## Related

- `bin/worktree-prune.sh` — the script the command forwards to
- `lib/worktree_prune.py` — the data half (parses porcelain, builds
  the epoch map, sorts oldest-first)
- `bin/worktree-remove-safe.sh` — per-worktree safe-removal wrapper
  used by the removal loop
- `tests/test_worktree_prune.py` — 14 hermetic tests covering Row
  age math, table rendering, end-to-end collect, and all three CLI
  modes (JSON / `--table` / `--count`)
- `dev-kit:worktree-janitor` agent — read-only counterpart that
  classifies candidates as `safe-to-remove` / `needs-human-check`
