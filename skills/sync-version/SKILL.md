---
name: sync-version
category: config
description: DEPRECATED. The GitHub Merge Queue now owns version sync at merge time; this skill is a no-op wrapper around bin/sync-version.sh that preserves the CLI surface for callers that haven't migrated yet. See docs/proposals/release/plugin-version-bump-via-merge-queue.yaml.
alpha: state
when_to_use: |
  - A pre-merge-queue hook (`.githooks/pre-push`) or an external CI script still references this skill name and the operator wants to verify what it does today
  - A contributor ran an older doc that mentions `/dev-kit:sync-version` and wants to know the current behavior
allowed-tools: Read
disallowed-tools: Bash Write Edit
model: sonnet
disable-model-invocation: true
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:sync-version — DEPRECATED no-op (merge queue owns the sync)

## What it does

**Nothing, by design.** As of 2026-08-30, the GitHub Merge Queue
rebases every PR onto the latest main (which already contains the
`version-bump.yml` bump from the previously-merged PR) immediately
before merge. The per-PR `plugin.json:version` conflict this skill
existed to resolve cannot happen anymore under merge queue, so:

- `bin/sync-version.sh` is now a no-op compat shim. It preserves the
  old CLI surface (`--check`, `--target`, `--from`, `--help`) for any
  caller that still references it, but does not mutate the working
  tree. The deprecation notice points at the merge queue.
- `.githooks/pre-push` no longer auto-syncs. It emits a NOTICE when
  local != origin/main (so developers see drift) but lets the push
  through; the queue handles the actual sync.

The full migration rationale, evidence chain, and options-considered
table are in:

```
docs/proposals/release/plugin-version-bump-via-merge-queue.yaml
```

## Iron Law

1. **Never** mutate `.claude-plugin/plugin.json` or
   `.codex-plugin/plugin.json` from this skill. Both manifests are
   owned by the trunk `version-bump.yml` workflow (now firing on
   `merge_group`). Manual edits re-introduce the parallel-PR cascade
   that the merge queue was set up to eliminate.

## How to invoke

```bash
# Confirm the no-op behavior is in place
bash bin/sync-version.sh --help

# Verify your local branch matches origin/main (read-only)
bash bin/sync-version.sh --check
# exit 0 = in sync, exit 1 = drift (rebase onto origin/main)

# If drift is reported, fix it the new way:
git fetch origin main
git rebase origin/main
```

## What to use instead

| Old | New |
|---|---|
| `/dev-kit:sync-version` (mutates working tree) | `git rebase origin/main` (the queue would do this for you; doing it locally surfaces merge conflicts before CI) |
| `bin/sync-version.sh --target v0.3.346` | `git rebase origin/main` |
| `bin/sync-version.sh` (no args) | `git rebase origin/main` |
| `chore(sync): advance plugin.json ...` commit | (no longer needed -- the queue rebases onto the bumped main before merge) |

## Hook integration

| Hook | Mode | Why |
|---|---|---|
| `stop-verify` | ON | MUST-L3: any future "done" claim must quote exit codes |
| `bash-guard` | ON | Defends against any accidental `git push --force` from a stale wrapper script |
| `git-guard` | ON | No-op skill doesn't touch git; guard stays on as defense in depth |
| `secret-scan` | ON | Globally on |
| `tdd-guard` | OFF | This skill has no production-code edits |
| `slop-detector` | OFF | No prose to scan |

## Red flags

- You typed `/dev-kit:sync-version` expecting it to commit a
  `chore(sync): ...` change. It does not. Use `git rebase origin/main`
  instead.
- The pre-push hook prints `[pre-push] NOTICE: local plugin.json=X !=
  origin/main=Y` and lets the push through. That's the new contract;
  the queue rebases on merge. Do NOT panic and run `/dev-kit:sync-version`
  to "fix" it.
- `bin/sync-version.sh --check` exits 1 (drift). The fix is a rebase,
  not a script invocation.

## Next step

- `git fetch origin main && git rebase origin/main` — the only
  legitimate user action that follows a drift notice.
- `/dev-kit:bump` — if you actually meant to ADVANCE the version (not
  catch up). This still works as before and posts a `chore(release):`
  PR; the merge queue then merges it via `merge_group`.
- `/dev-kit:babysit-pr` — for babysitting the PR that needed the
  rebase.
