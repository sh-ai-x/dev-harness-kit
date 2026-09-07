---
description: Read or write the team toggle (DEV_KIT_TEAM on|off) for the current project. Default OFF, independent of DEV_KIT_MODE.
allowed-tools: Read Write Bash AskUserQuestion
argument-hint: "[on|off] [--show] [--scope=project|local]"
model: sonnet
---

# /dev-kit:team — toggle team mode (.dev-kit/ tracking)

Forward to `skills/team/SKILL.md`. Implementation lives in `bin/dev_kit_team.py`.

Arguments:
- `--show` — print current team value + source + exit 0 (no picker)
- `--on` / `--off` — non-interactive write (skips picker)
- `--scope <project|local>` — where to write (default `project`)

When no args are given, open the picker (AskUserQuestion: on / off).

The team toggle is orthogonal to `DEV_KIT_MODE` (full / lite / undev). The four valid combinations are full+team, full+no-team, lite+team, lite+no-team.

See `docs/scopes/modes.md` for the resolution order and per-scope semantics.
