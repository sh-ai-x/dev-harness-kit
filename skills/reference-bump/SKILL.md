---
name: reference-bump
category: ship
description: Explicit gated upgrade of the pinned skills-library reference. Diffs breaking changes against the current version and gates the `claude plugin update` call on user approval.
alpha: state
when_to_use: |
  - User types /dev-kit:reference-bump
  - User wants explicit upgrade of the pinned skills-library reference before PR (e.g. cutting a release candidate)
  - Cherry-pick queue contains an item that needs a newer upstream pattern
allowed-tools: Read Write Bash Glob Grep
disallowed-tools: WebFetch Agent Edit
model: sonnet
disable-model-invocation: false
user-invocable: true
---

> [← Skills index](../../README.md)

# /dev-kit:reference-bump — Gated skills-library upgrade

## What it does

Upgrades the pinned skills-library reference from the current version
(e.g. v6.2.0) to the latest upstream tag. **Always gated on user
approval** — never auto-runs. Behavior:

1. Resolve currently installed version
   (`claude plugin list | grep skills-library`).
2. Query upstream latest release tag via `git ls-remote` against the
   marketplace source.
3. Diff the candidate version vs. installed version across:
   - skill files added / removed / renamed
   - hook definitions changed
   - manifest changes (the project's multi-manifest version policy)
   - any new Iron-Law phrasing in `using-superpowers`-style
     SessionStart-injected files
4. **Display the diff and stop.** Require the user to type
   `approve` before any upgrade.
5. On `approve`: `claude plugin update <marketplace-key> --force`.
6. Re-verify with `claude plugin list | grep skills-library`.

## Iron Law

1. Never auto-update. The skills-library marketplace entry has
   `"autoUpdate": false` per `rules/reference-coexistence.md`.
2. Never bypass the diff-display step. The user must read what changed
   before approving.
3. Refuses to bump if the cherry-pick backlog (`docs/cherry-picks/BACKLOG.md`)
   contains items that depend on the *current* pinned version. Update
   the cherry-pick memos first, or move them to a defer queue.
4. Refuses to bump if a worktree in this repo has uncommitted changes
   to files matching `rules/reference-coexistence.md` paths (per the
   frontmatter `paths:` filter) — user must commit or stash first.
5. Quarterly cap interaction: after a successful bump, the
   cherry-pick quarterly counter is not auto-incremented; existing
   `BACKLOG.md` items must be re-validated against the new upstream.

## Pre-flight

```bash
set -euo pipefail
# 1. Linear sync OK
gh auth status >/dev/null || { echo "gh auth failed"; exit 1; }

# 2. Resolve installed version
INSTALLED="$(claude plugin list | awk '/skills-library/ {print $2}' | head -1)"
[ -n "$INSTALLED" ] || { echo "skills-library not installed"; exit 1; }

# 3. Resolve upstream
LATEST="$(git ls-remote --tags --sort=-v:refname <upstream-url> | head -1 | awk -F/ '{print $NF}')"
[ -n "$LATEST" ] || { echo "could not resolve upstream"; exit 1; }

# 4. Diff (display only — do not apply)
echo "Installed: $INSTALLED"
echo "Latest:    $LATEST"
echo "---"
# (skill diff, hook diff, manifest diff, Iron-Law phrasing diff)
```

## Post-flight

```bash
# Re-verify
claude plugin list | grep skills-library
# Update BACKLOG.md if needed
# Run /dev-kit:valuate --harness-quality
# Run /codex:review on the post-bump state
```

## Hand-off

Writes `.dev-kit/hand-off/reference-bump→cherry-pick.md` if the bump
enables a previously-deferred cherry-pick. Reads BACKLOG.md to find
matches.
