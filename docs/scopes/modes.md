# Modes — `full` / `lite` / `undev`

Set `DEV_KIT_MODE` in `<proj>/.claude/settings.json` `env` block, or via `/dev-kit:mode`, or as a per-session env var.

| Mode    | When                                          | Skills          | Hooks          | Iron Laws       |
|---------|-----------------------------------------------|-----------------|----------------|-----------------|
| `full`  | Multi-session, multi-agent, autonomous        | All 30+         | All 30+        | L1–L9           |
| `lite`  | 4-hour MVP sprint, 6-person team              | 7 lite subset   | 7 lite subset  | L1–L9 (subset of gates) |
| `undev` | Non-dev / scratchpad / docs-only / random     | none            | none           | none (silent)   |

## Resolution order (highest wins)

1. `$DEV_KIT_MODE` env var in the shell — per-session override
2. `env.DEV_KIT_MODE` in `<proj>/.claude/settings.json` — committed project choice
3. Default = `full`

## Switching modes

```bash
# Project scope (committed)
/dev-kit:mode lite                  # writes to .claude/settings.json

# Per-session override (no file change)
DEV_KIT_MODE=undev claude --plugin-dir <dev-harness-kit-repo>

# Personal override (gitignored)
/dev-kit:mode lite                  # with --local flag writes to settings.local.json
```

## Why three modes, not two

| Need | Mode |
|---|---|
| Long-running autonomous work, GH-Actions babysit, full OWASP review | `full` |
| Greenfield MVP, 6-person team, 4-hour sprint, manual merges | `lite` |
| Random project that has nothing to do with dev-kit | `undev` (no plugin) |

The third mode is the one that makes the silent default intentional instead of accidental. Today, "undev" means "no plugin enabled" — which is what already happens for projects without `enabledPlugins`. The mode label just makes it explicit and reviewable.

## Mode + scope interaction

| Where mode is set | Precedence |
|---|---|
| `$DEV_KIT_MODE` shell env var | wins over everything |
| `<proj>/.claude/settings.json` env | wins over default |
| `<proj>/.claude/settings.local.json` env | wins over default (this checkout only) |
| not set | default = `full` |

Local-scope mode is useful when you want to test `lite` behavior in a `full` project without committing it.

## Mode + plugin-enable interaction

`undev` means the plugin is **off**, not "the plugin is on but with lite behavior". If `enabledPlugins.dev-kit@dev-kit: true` and `DEV_KIT_MODE=undev`, the plugin is on (full hooks fire) but the mode label is misleading. To actually be undev:

```jsonc
// .claude/settings.json
{
  "enabledPlugins": {},                  // ← empty
  "env": { "DEV_KIT_MODE": "undev" }     // ← explicit label
}
```
