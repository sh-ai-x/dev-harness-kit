# User Scope — `~/.claude/settings.json`

| Property | Value |
|---|---|
| **File** | `~/.claude/settings.json` |
| **Git** | not tracked |
| **Lifetime** | until you change it |
| **Owner** | you, only |
| **Applies to** | every Claude Code session on this machine |

## What belongs here

- **Personal preferences** — theme, statusline, model effort, language
- **Universal `allow` permissions** — your workflow helpers that work everywhere
- **Personal keybindings** — `~/.claude/keybindings.json` if used
- **`extraKnownMarketplaces`** — so slash commands resolve, even if you don't auto-enable the plugin

## What does NOT belong here

- ❌ `enabledPlugins.dev-kit@dev-kit: true` — **fires in every project on this machine**
- ❌ `enabledPlugins.dev-kit-lite@dev-kit-lite: true` — same leak
- ❌ Project-specific hooks
- ❌ Anything that should be team-shared
- ❌ Personal theme/preferences that only matter inside one project

## Canonical contents

See [`templates/settings.user.json`](templates/settings.user.json). Key points:

```jsonc
{
  "enabledPlugins": {},                  // ← deliberately empty
  "extraKnownMarketplaces": {
    "dev-kit-lite": { ... }              // ← slash commands resolve, but plugin is OFF
  },
  "permissions": {
    "allow": ["Edit(.worktrees/**)"]     // ← universal helpers only
  }
}
```

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Enable `dev-kit@dev-kit: true` at user scope | Hooks fire in random repos, scratchpads, README-only work | Move to project scope; remove from user scope |
| Add `dev-kit-lite` to `extraKnownMarketplaces` and never disable | lite hooks fire from cache even without `enabledPlugins` | Either install explicitly (project scope) or remove from `extraKnownMarketplaces` |
| Put personal skill overrides here | Settings are inconsistent across projects | Move to local scope (`<proj>/.claude/settings.local.json`) |
| Add a `.env` file with API keys | Accidental commit risk | Use a `.zshrc` export or a credential manager |

## Audit

```bash
# What does my user scope enable?
jq '.enabledPlugins // {}' ~/.claude/settings.json

# Expected: {} or only truly universal plugins

# Which marketplaces does my user scope register?
jq '.extraKnownMarketplaces // {} | keys' ~/.claude/settings.json
```

If `enabledPlugins` lists `dev-kit*`, the plugin leaks to every project. Move it to project scope or remove it.
