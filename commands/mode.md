---
description: Read or write the active DEV_KIT_MODE (full | lite | undev) for the current project.
allowed-tools: Read Write Bash AskUserQuestion
argument-hint: "[full|lite|undev] [--show] [--scope=project|local]"
model: sonnet
---

# /dev-kit:mode — read or write the active mode

Forward to `skills/mode/SKILL.md`. Implementation lives in `bin/dev_kit_mode.py`.

Arguments:
- `--show` — print current mode + exit 0 (no picker)
- `--mode <full|lite|undev>` — non-interactive write (skips picker)
- `--scope <project|local>` — where to write (default `project`)

When no args are given, open the picker (AskUserQuestion: full / lite / undev).

See `docs/scopes/modes.md` for the resolution order and per-scope semantics.
