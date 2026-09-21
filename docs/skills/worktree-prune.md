# `/dev-kit:worktree-prune`

Interactive stale-worktree cleanup. Lists every registered worktree
sorted by branch-tip age, then removes the N oldest (or keeps the N
newest) after a y/N gate.

## Slash

```
/dev-kit:worktree-prune                # interactive — ask how many
/dev-kit:worktree-prune 5              # remove the 5 oldest
/dev-kit:worktree-prune --keep 3       # prune to 3 newest
/dev-kit:worktree-prune --except-self -y   # nuke every worktree except this one
```

## When to use

- After a flurry of feature branches to reclaim disk space and tidy
  `.worktrees/`, `.claude/worktrees/`, and `.codex/worktrees/` together.
- When the `worktree-janitor` agent reports a batch as `safe-to-remove`
  and the operator wants to act on the verdict in one shot.
- When `git worktree list` shows >100 entries and the audit table is
  the only sensible way to triage.

## Companion reads

- `bin/worktree-prune.sh` — the script the slash forwards to. Owns CLI
  parsing, interactive prompts, and the per-row `bin/worktree-remove-safe.sh`
  dispatch.
- `lib/worktree_prune.py` — deterministic half (porcelain parse, epoch
  map, table sort, `--exclude` filtering).
- `bin/worktree-remove-safe.sh` — per-row safe-removal wrapper that
  archives each worktree's `logs/` to `logs/.archive/<branch>/<ts>/`
  before invoking `git worktree remove`.
- `bin/worktree-session-cleanup.sh` — single-worktree KEEP/REMOVE decision
  used by the completion-time cleanup advisory.
- `hooks/worktree-session-cleanup.sh` — Stop-stage advisory; it never removes
  a worktree without an explicit user decision.
- `tests/test_worktree_prune.py` — 21 hermetic tests covering Row age
  math, table rendering, end-to-end `collect`, and all CLI modes
  (JSON / `--table` / `--count` / `--exclude`).

## Completion-time cleanup

Task auto-cut intentionally has no active-worktree generation cap and never
reuses an existing worktree. After a completion-shaped response in a clean
task worktree, the Stop hook asks whether to keep the worktree or remove it.
Only an explicit `--decision remove` archives `logs/` and removes that one
worktree; the local branch remains. Dirty, detached, main-checkout, and
`babysit-pr` retained worktrees are kept.

## Known quirks (macOS bash 3.2)

The script targets `/usr/bin/env bash` (macOS ships 3.2.57, which has
neither `mapfile` nor safe empty-array expansion under `set -u`). Both
edges were hit and fixed during the original rollout; see the commit
message on `feat/worktree-prune-skill` for the post-mortem.

## Read-only counterpart

The `worktree-janitor` agent (separate) classifies each candidate as
`safe-to-remove` vs `needs-human-check` without mutating state. Run
the agent first if the user wants per-row judgement; run this slash
when the operator has reviewed the audit table and is ready to act.
