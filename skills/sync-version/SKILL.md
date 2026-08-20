---
name: sync-version
category: workflow
description: Sync local plugin.json:version to origin/main (or an explicit target). Same operation the pre-push hook runs automatically; use this skill when you want the sync committed before you push, when pre-push is not installed, or when a CI run reports a stale branch.
alpha: state
when_to_use: |
  - User types /dev-kit:sync-version [vX.Y.Z]
  - A PR is "behind" main by N bumps and the user wants to rebase without leaving the editor
  - A CI run failed Version freshness on HEAD < BASE
  - The user wants to verify the local branch's version matches origin/main without pushing
allowed-tools: Read Write Bash
disallowed-tools: WebFetch Agent
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:sync-version — Catch local plugin.json up to origin/main

## What it does

Advances `.claude-plugin/plugin.json:version` AND `.codex-plugin/plugin.json:version` to a target version. The default target is read from `origin/main`, so the local branch is brought up to whatever the trunk workflow has already published. With no args the skill is the user-triggered equivalent of what `.githooks/pre-push` runs automatically when it detects local < origin/main.

This is **sync (==)**, not **bump (+1)**. The trunk `.github/workflows/version-bump.yml` is still the single source of truth for the next version number; this skill only catches the branch up to a version origin/main already published. Bumping from this skill would re-introduce the parallel-PR cascade (#439's pre-fix pathology).

## Iron Law

1. Never increment locally. Refuse `--target` values that are LESS than the local version (use `/dev-kit:bump` for a real advance).
2. Refuse to run if `.claude-plugin/plugin.json` has uncommitted edits — the sync would clobber the user's work. The user must commit, stash, or discard first.
3. Sync both manifests in one pass. The freshness gate on the next push requires `HEAD:plugin.json:version` to match across runtime trees; partial sync would re-trip the check.
4. No local tagging. No `git push` from this skill. It stages and (with `--commit`) creates a `chore(sync):` commit; the user pushes when ready.
5. Idempotent. If local already equals the target, exit 0 with no changes (no commit, no amend).

## Pre-flight

```bash
set -euo pipefail
# 1. jq is required (same dependency as pre-push hook and version-bump workflow)
if ! command -v jq >/dev/null 2>&1; then
  echo "::error::jq is required; brew install jq | apt install jq"
  exit 1
fi
# 2. Working-tree guard: refuse to clobber an uncommitted plugin.json edit
if ! git diff --quiet -- .claude-plugin/plugin.json 2>/dev/null \
   || ! git diff --cached --quiet -- .claude-plugin/plugin.json 2>/dev/null; then
  echo "::error::.claude-plugin/plugin.json has uncommitted changes; commit, stash, or discard first"
  exit 1
fi
# 3. Resolve target (default = origin/main)
SYNC_SCRIPT="$(git rev-parse --show-toplevel)/bin/sync-version.sh"
if [ ! -x "$SYNC_SCRIPT" ]; then
  echo "::error::$SYNC_SCRIPT not found or not executable"
  exit 1
fi
```

## Behavior

```bash
# 1. Parse arg: optional explicit target version (with or without leading 'v')
TARGET="${1:-}"
case "$TARGET" in
  v*) TARGET="${TARGET#v}" ;;  # strip leading v
  "") ;;                        # default = origin/main's version (resolved inside the script)
  *)
    if ! [[ "$TARGET" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
      echo "::error::target must be MAJOR.MINOR.PATCH (e.g. 0.3.294 or v0.3.294); got '$TARGET'"
      exit 1
    fi
    ;;
esac

# 2. Read current version for the commit message
OLD_VERSION="$(jq -r .version .claude-plugin/plugin.json)"

# 3. Delegate to the script (single source of truth for sync semantics)
if [ -n "$TARGET" ]; then
  "$SYNC_SCRIPT" --target "$TARGET"
else
  "$SYNC_SCRIPT"
fi

# 4. If a real change landed, commit it (chore(sync): — not chore(release): bump)
NEW_VERSION="$(jq -r .version .claude-plugin/plugin.json)"
if [ "$NEW_VERSION" != "$OLD_VERSION" ]; then
  git add .claude-plugin/plugin.json .codex-plugin/plugin.json
  git commit -m "chore(sync): advance plugin.json from v${OLD_VERSION} to v${NEW_VERSION} (origin/main drift)"
  echo "synced + committed: v${OLD_VERSION} -> v${NEW_VERSION}"
else
  echo "no-op: local already at v${OLD_VERSION}"
fi
```

## Rules (no exceptions)

- Default target = `origin/main`. `bin/sync-version.sh` reads it with `git show origin/main:.claude-plugin/plugin.json`, no manual `git fetch` required.
- Pass an explicit target only when you need to align with a non-main ref (e.g. `origin/feature/foo` mid-rebase). The CI freshness check still compares HEAD against the PR base, not the target you synced to.
- `chore(sync): ...` is the ONLY commit shape this skill produces. Never `chore(release): bump dev-kit to v...` — that would feed the version-bump workflow's idempotency skip path on the wrong PR.
- No `--no-verify`. The pre-push hook will re-validate the sync on the next push.

## Hook integration

| Hook | Mode | Why |
|---|---|---|
| `stop-verify` | ON | MUST-L3: "done" must quote `git log -1 --format=%H` of the new commit |
| `bash-guard` | ON | Guards `git push --force` patterns (we don't push at all, but defense in depth) |
| `git-guard` | ON | Hard-blocks `gh pr merge` and direct main push |
| `secret-scan` | ON | Globally on |
| `tdd-guard` | OFF | Workflow tool, not test authoring |
| `slop-detector` | OFF | Single-line commit, no prose to scan |

## Output

- **stdout**: pre-flight probe (jq / working-tree / script-exists) + old→new version + commit hash.
- **git history**: one new commit `chore(sync): advance plugin.json from v${OLD} to v${NEW} (origin/main drift)`.

## Red flags

- The skill prints `local 0.3.X is AHEAD of target 0.3.Y; refusing to roll back` — you tried to sync backwards. Use `/dev-kit:bump` if you meant to advance, or pick a target >= your local.
- The skill prints `local 0.3.X < target 0.3.Y (sync needed)` from `--check` — origin/main has advanced and you have not yet pulled. Run `/dev-kit:sync-version` (no args) to fix.
- The pre-push hook (if installed) STILL re-runs the sync at push time. Running this skill before pushing is purely a convenience; it is not required and the two paths never conflict.

## Next step

After the sync commit:

- `git push -u origin HEAD` — push the synced branch. The pre-push hook will pass without re-running the sync (it sees `local == origin/main` after the sync commit).
- `/dev-kit:bump` — for an actual version ADVANCE, not a sync. Use only when preparing a release.
- `/dev-kit:babysit-pr` — for babysitting the PR that this sync commit was added to.
