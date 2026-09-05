# Local Scope — `<project>/.claude/settings.local.json`

| Property | Value |
|---|---|
| **File** | `<project>/.claude/settings.local.json` |
| **Git** | **gitignored** (you only) |
| **Lifetime** | until you delete the file or change branches |
| **Owner** | you, in this checkout |
| **Applies to** | your checkout of this project, only |

## What belongs here

- **Your personal debug flags** / env vars
- **Your temporary hotfix mode** — `setup-guard off` for spike work
- **Extra caution permissions** — `ask: ["Bash(rm -rf:*)"]` beyond team default

## What does NOT belong here

- ❌ Anything your teammates should have (project scope)
- ❌ Project-wide hooks (project scope)
- ❌ Universal preferences (user scope)

## Required `.gitignore` line

```gitignore
.claude/settings.local.json
```

See [`templates/.gitignore-snippet`](templates/.gitignore-snippet).

If this line is missing and the file is committed, your personal settings are now in the team's repo. Add the line, then `git rm --cached .claude/settings.local.json`.

## Canonical contents

See [`templates/settings.local.json`](templates/settings.local.json). Common fields:

```jsonc
{
  "env": {
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"   // ← personal debug env
  }
}
```

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Forgot `.gitignore` line | Personal file is committed; `git log` shows your overrides | Add `.gitignore` line; `git rm --cached` the file |
| Put team-shared rules here | Teammates don't get your settings | Move to project scope |
| Used this to "disable" the kit globally | Other projects on your machine still have the leak | Edit user-scope `enabledPlugins: {}` instead |
| File contains API keys | Risk of accidental commit despite gitignore | Use a credential manager, not this file |

## Audit

```bash
# Is this file properly gitignored?
git check-ignore .claude/settings.local.json && echo "OK — gitignored"

# What personal overrides do I have?
cat .claude/settings.local.json | jq .
```
