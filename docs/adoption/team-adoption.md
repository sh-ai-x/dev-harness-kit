# Personal vs Team adoption

dev-kit is one plugin, two adoption levels. Pick one based on whether you use Claude Code solo or in a team.

## Two levels

| Level | Lives in | Configured via | Who edits it |
|---|---|---|---|
| **Personal** | `~/.claude/plugins/dev-kit/` (User-level install) | `~/.claude/CLAUDE.md` | you, only you |
| **Team** | `<project>/.claude/plugins/dev-kit/` (Project-level install) | `<project>/CLAUDE.md` + `<project>/<subfolder>/CLAUDE.md` | the team, via PR |

Both installs use the same plugin artifact. Only the install location and config layer differ.

## Install

```bash
# Personal (User-level) — applies to every project you open
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit --scope user

# Team (Project-level) — applies only to this project, committed via PR
cd your-project
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit --scope project
git add .claude/ && git commit -m "chore: install dev-kit plugin"
```

## CLAUDE.md patterns

### Project root — `<project>/CLAUDE.md`

The single source of truth for the whole repo. Commit this. It includes:

- Iron Laws (no test no code, root-cause-first, …)
- Project-wide conventions (language, framework, test runner, formatter)
- Build / lint / test commands
- Stage gates and what each one blocks

### Sub-folder overrides — `<project>/<subfolder>/CLAUDE.md`

Loaded automatically when Claude Code works in that folder. Used for:

- Per-package conventions (e.g. `packages/api/CLAUDE.md` says "zod validation mandatory")
- Per-area guards (e.g. `migrations/CLAUDE.md` says "never edit a committed migration")
- Per-stack rules (e.g. `frontend/CLAUDE.md` says "vitest, no jest")

These override root rules only when narrower. They never contradict the root Iron Laws.

### Personal layer — `~/.claude/CLAUDE.md`

Loaded on top of project rules for every session. Used for:

- Your personal prefs (default model, preferred editor, key bindings)
- Cross-project shortcuts
- Things you never want to commit to a repo

## Team hooks (opt-in templates, NOT in the plugin)

The plugin ships a personal hook set wired into `hooks/hooks.json` and the
Codex mirror at `.codex-plugin/hooks/hooks.json`. The exact count drifts
as the plugin evolves (worktree-rule and provider-divergence hooks were
added on top of the original core; the provider-divergence hook was
[pruned 2026-09-30 in PR 941] as redundant with `bin/set-provider.sh`),
so it is NOT recorded here.
Inspect the live inventory with:

```bash
grep -E '"command"' hooks/hooks.json | wc -l   # canonical hook count
ls -1 docs/team-hooks/*.sh                     # team-hook templates
```

The 3 team hooks below are **NOT** registered in the plugin's
`hooks/hooks.json` — they live in `docs/team-hooks/` as **copy-paste
templates** that each project copies into its own `.claude/hooks/` and
wires up in `.claude/settings.json`.

| Hook | Stage | Mode | Where |
|---|---|---|---|
| `prettier-format` | PostToolUse (Edit/Write) | advisory | `docs/team-hooks/` (copy) |
| `block-dangerous-commands` | PreToolUse (Bash) | **hard-block** | `docs/team-hooks/` (copy) |
| `eslint-fix` | PostToolUse (Edit/Write) | advisory | `docs/team-hooks/` (copy) |

### Why team hooks are templates, not plugin content

Auto-formatting and hard-blocks are team policy, not personal preference. A solo dev who works in 5 repos shouldn't be forced into a 6th repo's formatter. And the plugin cannot enforce a team hook per-project: the plugin is **scoped** by Claude Code (one hooks.json per install), so a hook either fires on every project or none.

**Solution**: ship the scripts in `docs/team-hooks/` as copy-paste templates. Each team commits the copy that matches their policy.

### Enable a team hook in a project

```bash
# 1. Copy the templates into the project
mkdir -p .claude/hooks
cp docs/team-hooks/prettier-format.sh .claude/hooks/
cp docs/team-hooks/block-dangerous-commands.sh .claude/hooks/
cp docs/team-hooks/eslint-fix.sh .claude/hooks/
chmod +x .claude/hooks/*.sh

# 2. Wire them in .claude/settings.json
```

```json
{
  "permissions": { "allow": ["Edit", "Write", "Bash"] },
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{ "type": "command", "command": "bash .claude/hooks/block-dangerous-commands.sh" }]
    }],
    "PostToolUse": [{
      "matcher": "Write|Edit|MultiEdit",
      "hooks": [
        { "type": "command", "command": "bash .claude/hooks/prettier-format.sh" },
        { "type": "command", "command": "bash .claude/hooks/eslint-fix.sh" }
      ]
    }]
  }
}
```

The team shares this file via PR.

### Why hard-block only on `block-dangerous-commands` (not the others)

Auto-format is reversible (git checkout). A blocked `rm -rf` or `git push --force` is **not** — it can destroy work or rewrite shared history. Only `block-dangerous-commands` hard-blocks. The other two are advisory (exit 0, print warning).

### Coverage non-overlap with `bash-guard.sh`

| Concern | bash-guard | block-dangerous-commands |
|---|---|---|
| Destructive file ops | `rm -rf /`, `chmod 777` | `rm -rf` tokenized, curl\|sh, fork bomb |
| Destructive git | `git push --force.* main`, `git reset --hard`, `git clean -f` | `git push --force` (any), `git reset --hard`, `git clean -fd/-fdx` |
| DDL/data | `DROP TABLE`, `DROP DATABASE` | (out of scope) |
| Supply chain | `npm publish`, `eval $`, `>/etc/passwd` | (out of scope) |
| Mode | advisory → hard-block with `DEV_KIT_STRICT=1` | always hard-block |

No double-coverage. Either hook alone is safe; running both is also safe (the second hook sees a no-op and exits 0).

### Override per-folder

A sub-folder can override by adding its own `.claude/settings.json`. Example: `packages/legacy/` opts out of `prettier-format` because the legacy code is excluded from prettier for now.

## Why the split

- **Personal = your machine.** Always on, never committed, changes with your mood.
- **Team = the repo.** Always committed, changes via PR, reviewed by humans.
- **Folder = a sub-area.** Inherits project, overrides for one directory.

Three layers, one plugin, zero conflict.
