---
name: codex-cache-update
category: shortcuts
description: Refresh the dev-kit Codex marketplace checkout and synchronize the versioned plugin cache. Use when Codex reports the marketplace is current but the installed cache may be stale, or after a dev-kit merge.
alpha: analysis
when_to_use: |
  - User types /dev-kit:codex-cache-update
  - User asks to update or refresh the Codex dev-kit plugin cache
  - `codex plugin marketplace upgrade dev-kit` reports up to date but files are stale
  - A new dev-kit version or commit was merged to main
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch Agent
model: haiku
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

## What it does

Refreshes the dev-kit Codex marketplace checkout and synchronizes the matching versioned cache directory so a new Codex session loads current plugin files.

Run the bundled updater from the repository root:

```bash
bash skills/codex-cache-update/scripts/update.sh
```

The updater first runs `codex plugin marketplace upgrade dev-kit`, then reads
the marketplace plugin version and synchronizes the marketplace checkout into
`$HOME/.codex/plugins/cache/dev-kit/dev-kit/<version>` with `rsync --delete`.
This second step is required because the marketplace command can report
“already up to date” while the versioned cache still contains stale files.

## Verification

The command prints the marketplace commit, cache directory, and a final
`cache synchronized` line. Confirm the source and cache manifests have the
same version before restarting Codex. Use `--dry-run` to inspect differences
without changing the cache.

## Configuration

Use these environment variables when the default Codex paths are different:

```bash
CODEX_MARKETPLACE_DIR="$HOME/.codex/.tmp/marketplaces/dev-kit" \
CODEX_CACHE_ROOT="$HOME/.codex/plugins/cache/dev-kit/dev-kit" \
bash skills/codex-cache-update/scripts/update.sh
```

The updater excludes Git metadata, worktrees, generated dev-kit state, Python
bytecode, and evaluation caches from the installed cache.

## Next step

Restart Codex so the refreshed plugin cache is loaded by the new session.
