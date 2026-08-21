> [← Skills index](README.md) · [Project README](../../README.md)

# `sync-version`

**Category:** `config` · **Alpha:** `state` · **Invocation:** `/dev-kit:sync-version [vX.Y.Z]` (human-invoked)

Sync local `.claude-plugin/plugin.json:version` to `origin/main` (or an explicit target). Same operation the `pre-push` hook runs automatically; use this skill when you want the sync committed before you push, when `pre-push` is not installed, or when a CI run reports a stale branch.

## When to use it

- A PR is "behind" main by N bumps and the user wants to rebase without leaving the editor.
- A CI run failed `Version freshness` on `HEAD < BASE`.
- The user wants to verify the local branch's version matches `origin/main` without pushing.

## How it works

A 3-step deterministic sync:

1. **Read** — resolve the current `plugin.json:version` and the target (default = `origin/main` resolved via `git fetch origin main`).
2. **Compare** — if local already equals target, exit 0 with `nothing to sync`. If local is **ahead**, refuse (local is newer than origin — bump, don't sync). If local is **behind** by N>1, print `behind=N` and refuse (multiple-bump race; pull first).
3. **Patch + verify** — update only the `version` field, validate `plugin.json` is still parseable, and print `synced old → new`.

Backed by `bin/sync-version.sh`; the pre-push hook calls the same script with `local < origin/main` as the trigger. The hook refuses to push when `plugin.json` has uncommitted edits (avoids losing in-flight work).

## Usage

```bash
# Default: sync to origin/main
/dev-kit:sync-version

# Explicit target
/dev-kit:sync-version v0.3.301

# Verify only (no write)
/dev-kit:sync-version --check

# Force-write when local has uncommitted edits (rare)
/dev-kit:sync-version --force

# Raw script
bin/sync-version.sh
bin/sync-version.sh --check
```

## Related

- [`/dev-kit:bump`](bump.md) — the inverse operation (bump local ahead of `origin/main`, push a `chore/bump-vX.Y.Z` branch).
- [`hooks/HOOK-REFERENCE.md`](../hooks/HOOK-REFERENCE.md) § pre-push — the auto-SYNC half of the pre-push hook.
- [`bin/sync-version.sh`](../../bin/sync-version.sh) — the script the skill wraps.
- [`skills/sync-version/SKILL.md`](../../skills/sync-version/SKILL.md) — full algorithm body.