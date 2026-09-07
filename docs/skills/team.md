> [← Skills index](README.md) · [Project README](../../README.md)

# `team`

**Category:** `mode` · **Alpha:** `state` · **Invocation:** `/dev-kit:team [on|off]` (human-invoked)

`team` toggles whether `.dev-kit/` is tracked in git for the current project. The toggle is a separate env-var (`DEV_KIT_TEAM`) and is orthogonal to `DEV_KIT_MODE` — full+team, full+no-team, lite+team, and lite+no-team are all valid combinations. Default is OFF (silent).

## When to use it

- The user types `/dev-kit:team on` (or `off`) to set the team toggle at project scope.
- The user types `/dev-kit:team --show` to print the current value + source.
- The user types `/dev-kit:team` with no args and picks on/off from the picker.
- The user wants `.dev-kit/` (sanity reports, eval cache, hand-off notes) committed to git so the team can share them.

## How it works

`team` resolves `DEV_KIT_TEAM` from a 4-layer chain (mirroring `mode`):

1. `$DEV_KIT_TEAM` shell env var (per-session override)
2. `<proj>/.claude/settings.json` `env.DEV_KIT_TEAM` (team-committed)
3. `<proj>/.claude/settings.local.json` `env.DEV_KIT_TEAM` (gitignored personal override)
4. Not set → **OFF** (silent default — team toggle is opt-in)

The single concrete effect when team=ON is: `hooks/lib/team-resolve.sh:dev_kit_team_resolve` returns `on`, and `/dev-kit:bootstrap` reads that result to strip `^\.dev-kit` from the target `.gitignore` (sub-stage 8.5).

## Usage

```bash
/dev-kit:team                  # picker (on / off)
/dev-kit:team on               # project-scope write
/dev-kit:team off              # project-scope remove
/dev-kit:team on --scope local # personal override (gitignored)
/dev-kit:team --show           # print current value + source
DEV_KIT_TEAM=on claude         # one-session override (no file change)
```

| Argument | Effect |
|---|---|
| *(none)* | Opens the picker (AskUserQuestion: on / off). |
| `on` | Writes `DEV_KIT_TEAM=1` to project scope. |
| `off` | Removes `DEV_KIT_TEAM` from project scope (silent default takes over). |
| `--show` | Prints `team: <ON|OFF>  (set via <source>)` and exits 0. |
| `--scope local` | Writes to `settings.local.json` instead of `settings.json`. |

## Resolution matrix

| Mode | team OFF (default) | team ON |
|---|---|---|
| `full` | full dev-kit (30+ skills/hooks), `.dev-kit/` gitignored | full dev-kit, `.dev-kit/` tracked |
| `lite` | lite 7/7 subset, `.dev-kit/` gitignored | lite 7/7 subset, `.dev-kit/` tracked |
| `undev` | plugin off (team toggle is a no-op since plugin disabled) | plugin off (same) |

## What `team` does NOT do

- **Does not modify `DEV_KIT_MODE`.** Team toggle and mode are orthogonal env-vars.
- **Does not modify `enabledPlugins`.** Plugin enable/disable is separate.
- **Does not commit.** The write is staged in the working tree; the operator decides when to commit and push.
- **Does not auto-track hand-off or PR notes.** Those land in `.dev-kit/hand-off/` regardless of team state; team toggle only decides whether that directory is gitignored.

## Output

- **stdout**: the resolved value (one line for `resolve`, `team: <ON|OFF>  (set via <source>)` for `show`, `wrote DEV_KIT_TEAM=1 to <path>` or `removed DEV_KIT_TEAM from <path>` for `write`).
- **`.claude/settings.json`** or **`.claude/settings.local.json`**: the `env.DEV_KIT_TEAM` key is set or removed.

## Related

- [`mode`](../skills/mode.md) — the orthogonal mode toggle (full / lite / undev).
- [`docs/scopes/modes.md`](../scopes/modes.md) — full resolution-order reference and team-toggle matrix.
- [`bootstrap`](../skills/bootstrap.md) — when run, bootstrap reads `$DEV_KIT_TEAM` and acts on it (sub-stage 8.5).
- [`hooks/lib/team-resolve.sh`](../../hooks/lib/team-resolve.sh) — single source of truth for resolution logic.
- [`bin/dev_kit_team.py`](../../bin/dev_kit_team.py) — pure CLI used by this skill.
- [`tests/test_team_resolution.py`](../../tests/test_team_resolution.py) — resolver regression cases.
- [`tests/test_dev_kit_team_cli.py`](../../tests/test_dev_kit_team_cli.py) — CLI regression cases.
- [`tests/test_bootstrap_team_wiring.py`](../../tests/test_bootstrap_team_wiring.py) — gitignore-strip behavior tests.

---
*Source: [`skills/team/SKILL.md`](../../skills/team/SKILL.md)*
