---
description: Read or write the active DEV_KIT_MODE (full | lite | undev | mod) for the current project.
allowed-tools: Read Write Bash AskUserQuestion
argument-hint: "[full|lite|undev|mod] [--show] [--scope=project|local]"
model: sonnet
---

# /dev-kit:mode — read or write the active mode

Forward to `skills/mode/SKILL.md`. Implementation lives in `bin/dev_kit_mode.py`.

Arguments:
- `--show` — print current mode + exit 0 (no picker)
- `--mode <full|lite|undev|mod>` — non-interactive write (skips picker). `mod` triggers the user-defined-role picker loop.
- `--scope <project|local>` — where to write (default `project`)

When no args are given, open the picker (AskUserQuestion: full / lite / undev / mod).

`mod` mode requires `roles` block in `.claude/settings.json`. The picker prompts for active role + member role names + per-role skill subsets (no shipped defaults).

See `docs/scopes/modes.md` for the resolution order and per-scope semantics.
