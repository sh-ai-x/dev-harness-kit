# Project Scope — `<project>/.claude/settings.json`

| Property | Value |
|---|---|
| **File** | `<project>/.claude/settings.json` |
| **Git** | **committed** (team-shared) |
| **Lifetime** | as long as the project exists |
| **Owner** | the team |
| **Applies to** | when Claude Code's cwd is in this project |

## What belongs here

- **This project's plugin choice** — `enabledPlugins` reflects the team's choice
- **Mode** — `env.DEV_KIT_MODE` is `full`, `lite`, or `undev`
- **Hooks the team agreed on** — SessionStart, PreToolUse, etc.
- **Team-shared `allow` permissions** — `.worktrees/**`, etc.
- **`.gitignore` line**: `.claude/settings.local.json`

## What does NOT belong here

- ❌ Your personal theme/preferences
- ❌ Debug flags you'll forget about
- ❌ Skill overrides specific to your taste (those go in local scope)
- ❌ Plugin enables that affect other projects on your machine

## Templates

| Project type | Template |
|---|---|
| Multi-session dev / autonomous work | [`templates/settings.project.full.json`](templates/settings.project.full.json) |
| 4-hour MVP sprint / 6-person team | [`templates/settings.project.lite.json`](templates/settings.project.lite.json) |
| Non-dev repo (silent default) | [`templates/settings.project.undev.json`](templates/settings.project.undev.json) |

## Created by

- `/dev-kit:bootstrap` writes this file with the appropriate template based on your mode choice.
- `/dev-kit:ci-setup` does NOT write this file; it writes `.github/workflows/` instead. Run bootstrap first if the file is missing.

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Forgot `.gitignore` line for `.claude/settings.local.json` | Local settings file accidentally committed | Add the line from [`templates/.gitignore-snippet`](templates/.gitignore-snippet) |
| Mode set to `full` on a 4-hour sprint | Too much gate overhead, slow iteration | Switch to `lite` or use `DEV_KIT_MODE=lite claude` per session |
| Mode unset on a consumer project | Defaults to `full`; works but you may want `lite` | Add `"env": { "DEV_KIT_MODE": "lite" }` |
| Multiple `enabledPlugins` for the same kit at different scopes | Duplicate hook firing | Pick one scope; user-scope should be `{}` |

## Audit

```bash
# What does this project enable?
jq '.enabledPlugins // {}' .claude/settings.json

# What's the mode?
jq -r '.env.DEV_KIT_MODE // "full (default)"' .claude/settings.json

# Is the local settings file gitignored?
git check-ignore .claude/settings.local.json && echo "OK"
```
