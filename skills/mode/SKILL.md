---
name: mode
category: mode
description: Read or write the active DEV_KIT_MODE (full | lite | undev) for the current project. Picker by default; --show to display current mode; --scope=local to write to .claude/settings.local.json instead of .claude/settings.json.
alpha: state
user-invocable: true
when_to_use: |
  - User types `/dev-kit:mode` and wants to pick a mode
  - User types `/dev-kit:mode --show` to see current mode
  - User types `/dev-kit:mode lite` to switch to 4-hour MVP mode
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch
model: sonnet
disable-model-invocation: false
---
> [← Skills index](../../README.md)

# /dev-kit:mode — pick / show / write the active plugin mode

## What it does

The mode selector is the single switch that gates which dev-kit hooks
and skills run. Three legal values:

- **`full`** — current dev-kit (30+ skills, 30+ hooks, multi-session/multi-agent). Default if plugin is enabled.
- **`lite`** — 7-hook / 7-skill subset for 4-hour MVP sprints with a 6-person team.
- **`undev`** — plugin not enabled; silent default.

The resolution order lives in [`docs/scopes/modes.md`](../../docs/scopes/modes.md):

| Source | Effective value |
|---|---|
| `$DEV_KIT_MODE` shell env | wins over everything (per-session override) |
| `<proj>/.claude/settings.json` `env.DEV_KIT_MODE` | wins over default (team-committed) |
| `<proj>/.claude/settings.local.json` `env.DEV_KIT_MODE` | kicks in when project scope is unset (this checkout only) |
| not set | `full` only when `enabledPlugins.dev-kit@dev-kit: true`; otherwise `undev` |

## Iron Law (no exceptions)

**0-arg default opens the picker.** Hidden flags: `--show` (print current mode + exit 0), `--scope=project|local` (where to write; default `project`; `--scope=local` writes to gitignored `settings.local.json` instead of team-committed `settings.json`), `--mode full|lite|undev` (non-interactive; bypasses the picker), `--target DIR` (operate on `<DIR>` instead of `$PWD`).

## 4-Step Orchestration

```
[1] resolve current    -> bin/dev_kit_mode.py resolve (pure helper)
       | (auto, deterministic; reads $DEV_KIT_MODE then settings layers)
[2] show               -> print "current mode: <X>  (set via <source>)" + exit 0
       | (auto, when --show or first run)
[3] pick (interactive) -> AskUserQuestion (full / lite / undev)
       | (auto, when no --mode arg)
[4] write              -> bin/dev_kit_mode.py write --scope <project|local> --mode <X>
       | (auto)
```

## When to use which scope

- **`--scope=project`** (default) — writes to `.claude/settings.json`. Team-committed; everyone in the repo gets this mode.
- **`--scope=local`** — writes to `.claude/settings.local.json`. Gitignored; only your checkout is affected. Use this when you want to test `lite` behavior in a `full` project without committing it.
- **Shell env var** — `DEV_KIT_MODE=lite claude` for a one-session override that doesn't touch any file.

## Examples

```bash
# Picker (default)
/dev-kit:mode

# Non-interactive
/dev-kit:mode lite
/dev-kit:mode --mode undev

# Show current
/dev-kit:mode --show

# Personal override (gitignored)
/dev-kit:mode --mode lite --scope local

# One-session override (no file change)
DEV_KIT_MODE=undev claude
```

## What this skill does NOT do

- **Does not modify `enabledPlugins`.** Mode = "undev" with the plugin enabled is a misleading label; to actually go undev, run `/dev-kit:bootstrap` and choose "no kit", or edit `.claude/settings.json` to set `"enabledPlugins": {}`.
- **Does not commit.** When writing to `--scope=project`, the change is staged in the working tree but not committed. The operator decides when to commit and push.
- **Does not archive `dev-harness-kit-lite`.** That's a separate concern; see `/dev-kit:docs` or the proposal at `docs/proposals/scope-consolidation/00-index.yaml`.

## Cross-references

- [`docs/scopes/modes.md`](../../docs/scopes/modes.md) — full mode reference
- [`hooks/lib/mode-resolve.sh`](../../hooks/lib/mode-resolve.sh) — single source of truth for resolution logic
- [`tests/test_mode_resolution.py`](../../tests/test_mode_resolution.py) — 15 pinned resolution cases
- Hook integration (per-hook `dev_kit_mode_require`) — see follow-up PR; current PR ships the resolver + skill only
