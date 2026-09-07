---
name: team
category: mode
description: Read or write the team toggle (DEV_KIT_TEAM on|off). Default OFF, independent of DEV_KIT_MODE. When ON, .dev-kit/ stays tracked in git.
alpha: state
user-invocable: true
when_to_use: |
  - User types `/dev-kit:team` and wants to toggle
  - User types `/dev-kit:team --show` to see current state
  - User types `/dev-kit:team on` or `/dev-kit:team off` to switch
  - User wants `.dev-kit/` tracked in git (project-scope team)
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch
model: sonnet
disable-model-invocation: false
---
> [← Skills index](../../README.md)

# /dev-kit:team — toggle team mode (.dev-kit/ tracking)

## What it does

The team toggle is the single switch that decides whether `.dev-kit/`
is tracked in the project's git. Two legal values:

- **`on`** — `.dev-kit/` is kept in git (the team-committed default).
- **`off`** — `.dev-kit/` is gitignored (the silent default).

This is **orthogonal to `DEV_KIT_MODE`** (full / lite / undev). The
four valid combinations are full+team, full+no-team, lite+team,
lite+no-team.

The resolution order lives in [`docs/scopes/modes.md`](../../docs/scopes/modes.md):

| Source | Effective value |
|---|---|
| `$DEV_KIT_TEAM` shell env | wins over everything (per-session override) |
| `<proj>/.claude/settings.json` `env.DEV_KIT_TEAM` | wins over default (team-committed) |
| `<proj>/.claude/settings.local.json` `env.DEV_KIT_TEAM` | kicks in when project scope is unset (this checkout only) |
| not set | `off` (silent default — team toggle is opt-in) |

## Iron Law (no exceptions)

**0-arg default opens the picker.** Hidden flags: `--show` (print
current team value + exit 0), `--on` / `--off` (non-interactive;
bypasses the picker), `--scope=project|local` (where to write; default
`project`; `--scope=local` writes to gitignored `settings.local.json`
instead of team-committed `settings.json`), `--target DIR` (operate on
`<DIR>` instead of `$PWD`).

## 4-Step Orchestration

```
[1] resolve current    -> bin/dev_kit_team.py resolve (pure helper)
       | (auto, deterministic; reads $DEV_KIT_TEAM then settings layers)
[2] show               -> print "team: <ON|OFF>  (set via <source>)" + exit 0
       | (auto, when --show or first run)
[3] pick (interactive) -> AskUserQuestion (on / off)
       | (auto, when no --on/--off arg)
[4] write              -> bin/dev_kit_team.py write --on|--off --scope <project|local>
       | (auto)
```

## When to use which scope

- **`--scope=project`** (default) — writes to `.claude/settings.json`.
  Team-committed; everyone in the repo gets this team setting.
- **`--scope=local`** — writes to `.claude/settings.local.json`.
  Gitignored; only your checkout is affected. Use this when you want
  team=on for yourself in a repo where the team hasn't opted in.
- **Shell env var** — `DEV_KIT_TEAM=on claude` for a one-session
  override that doesn't touch any file.

## Examples

```bash
# Picker (default)
/dev-kit:team

# Non-interactive
/dev-kit:team on
/dev-kit:team off
/dev-kit:team --on
/dev-kit:team --off

# Show current
/dev-kit:team --show

# Personal override (gitignored)
/dev-kit:team on --scope local

# One-session override (no file change)
DEV_KIT_TEAM=on claude
```

## What this skill does NOT do

- **Does not modify `enabledPlugins`.** Team mode is purely about
  `.dev-kit/` git tracking; plugin enable/disable is a separate concern.
- **Does not modify `DEV_KIT_MODE`.** The two env-vars are orthogonal;
  toggling team does not change the mode skill/hook subset.
- **Does not commit.** When writing to `--scope=project`, the change is
  staged in the working tree but not committed. The operator decides
  when to commit and push.

## Cross-references

- [`docs/scopes/modes.md`](../../docs/scopes/modes.md) — team toggle
  table (orthogonal to mode)
- [`docs/skills/team.md`](../../docs/skills/team.md) — operator-facing
  reference
- [`hooks/lib/team-resolve.sh`](../../hooks/lib/team-resolve.sh) —
  single source of truth for resolution logic
- [`bin/dev_kit_team.py`](../../bin/dev_kit_team.py) — pure CLI used
  by this skill (subcommands: `resolve`, `show`, `write`)
- [`tests/test_team_resolution.py`](../../tests/test_team_resolution.py)
  — resolver regression cases
- [`tests/test_dev_kit_team_cli.py`](../../tests/test_dev_kit_team_cli.py)
  — CLI regression cases
- `/dev-kit:bootstrap` — when bootstrap runs, it reads
  `DEV_KIT_TEAM` via `hooks/lib/team-resolve.sh` and strips
  `^\.dev-kit` from the target `.gitignore` (sub-stage 8.5).
