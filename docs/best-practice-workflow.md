# Best-Practice Workflow — dev-kit Scope, Mode, and Session Overrides

Action-oriented guide for day-to-day dev-kit use. The companion reference is
[`docs/scopes/README.md`](scopes/README.md); this file turns that reference
into a setup checklist, a daily workflow, and a session-override cheat sheet.

> **Default rule.** Smallest scope that satisfies the need wins.
> A scratch repo you created five minutes ago does **not** need dev-kit enabled.

---

## TL;DR

| Decision                         | Where it lives                                      |
|---------------------------------|-----------------------------------------------------|
| "Is dev-kit enabled at all?"    | `enabledPlugins` in user / project / local settings |
| "Which mode?" (`full` / `lite` / `undev` / `team`) | `DEV_KIT_MODE` env var or `/dev-kit:mode`           |
| "Track `.dev-kit/` in git?"     | `DEV_KIT_TEAM` env var or `/dev-kit:team`           |
| "Pause the hard-block hooks for this session only?" | `/dev-kit:guard-mode off`                           |
| "Silence all optional local hooks for this session only?" | `/dev-kit:harness-mode fast`                       |

If you only remember one line: **never put `enabledPlugins.dev-kit@dev-kit: true`
in `~/.claude/settings.json`.** That single line leaks the plugin into every
project on the machine — scratchpads, recipes, README-only work, throwaway
spikes — and there is no per-project opt-out once it is on.

---

## The three-tier model

```
┌──────────────────────────────────────────────────────────────────┐
│  ~/.claude/settings.json          (machine-wide)                  │
│   ├─ extraKnownMarketplaces ✅   — slash commands resolve         │
│   ├─ enabledPlugins.dev-kit* ❌  — LEAKS, do not set               │
│   ├─ theme / statusline / model effort ✅                         │
│   └─ universal allow permissions ✅ (e.g. Edit(.worktrees/**))    │
├──────────────────────────────────────────────────────────────────┤
│  <proj>/.claude/settings.json     (committed, team-shared)        │
│   ├─ enabledPlugins.dev-kit@dev-kit: true  ✅  — per-project opt-in│
│   ├─ env.DEV_KIT_MODE = full|lite|undev|team  ✅                   │
│   └─ roles.* (team mode only)                                    │
├──────────────────────────────────────────────────────────────────┤
│  <proj>/.claude/settings.local.json  (gitignored, personal)       │
│   ├─ env.DEV_KIT_MODE override ✅                                 │
│   ├─ env.DEV_KIT_TEAM override ✅                                 │
│   └─ personal debug flags ✅                                     │
├──────────────────────────────────────────────────────────────────┤
│  Session overrides            (no file change, dies with session) │
│   ├─ /dev-kit:guard-mode off|on|show                              │
│   ├─ /dev-kit:harness-mode fast|full|custom|show                  │
│   ├─ DEV_KIT_MODE=... claude ...                                 │
│   └─ DEV_KIT_TEAM=on|off claude ...                              │
└──────────────────────────────────────────────────────────────────┘
```

Resolution order at every layer: **session override > local scope > project
scope > user scope > default**.

---

## Setup checklist (run once per machine)

```bash
# 1. User scope — register marketplace so slash commands resolve everywhere.
#    Do NOT enable the plugin here.
jq '.enabledPlugins // {}' ~/.claude/settings.json
# Expected: {}  (empty — no plugin auto-enabled)

jq '.extraKnownMarketplaces // {} | keys' ~/.claude/settings.json
# Expected: includes "dev-kit" or "dev-kit-lite"

# 2. In each project that actually needs dev-kit, opt in at PROJECT scope.
cd ~/projects/my-real-app
jq '.enabledPlugins' .claude/settings.json
# Expected: { "dev-kit@dev-kit": true }
```

If step 1 shows any `dev-kit*` entry under `enabledPlugins`, **remove it**.
That is the leak. The plugin still resolves slash commands; it just no longer
fires hooks in projects that have not opted in.

---

## Day-to-day workflow

### I am creating a new repo (scratch, docs-only, recipe, spike)

Do nothing. `enabledPlugins` defaults to empty, which is `undev` mode
(`DEV_KIT_MODE` falls back to `undev` when the plugin is not enabled — see
`modes.md` resolution order). The plugin is silent. No hooks fire.

If you later decide the project needs dev-kit:

```bash
/dev-kit:bootstrap        # writes minimal CLAUDE.md + AGENTS.md + active-hooks.json
/dev-kit:mode lite        # or full / team
```

### I am joining an existing dev-kit project

```bash
git clone <repo>
cd <repo>
# The committed .claude/settings.json already enables dev-kit. No action needed.
```

### I want a quick exception in a project that has dev-kit enabled

| Want                                   | Command                          |
|----------------------------------------|----------------------------------|
| Disable `tdd-guard` + `worktree-guard` for this session only | `/dev-kit:guard-mode off`        |
| Silence all optional local hooks for this session only      | `/dev-kit:harness-mode fast`     |
| Run one command in undev-mode without changing settings      | `DEV_KIT_MODE=undev claude ...`  |
| Track `.dev-kit/` in this checkout only                       | `/dev-kit:team on --scope local` |

None of these touch any file. They die when the session ends.

### I want a durable change for one checkout

```bash
/dev-kit:mode lite --local           # writes to settings.local.json (gitignored)
/dev-kit:team on --scope local
```

`settings.local.json` is the right home for personal debug flags that would
confuse your teammates.

---

## Common pitfalls

| Symptom                                                              | Cause                                                           | Fix                                                            |
|----------------------------------------------------------------------|-----------------------------------------------------------------|----------------------------------------------------------------|
| Hooks fire in scratch / README-only / recipe repos                    | `enabledPlugins.dev-kit@dev-kit: true` at user scope            | Remove from `~/.claude/settings.json`                          |
| Teammate sees different hooks in the same project                    | Mixed user / project / local scope overrides                    | Audit all three: `jq '.enabledPlugins' ...` for each layer     |
| `undev` is set but hooks still fire                                  | `enabledPlugins.dev-kit@dev-kit: true` overrides the mode       | Set `enabledPlugins` empty AND `DEV_KIT_MODE=undev`             |
| Plugin updates are not picked up                                     | Plugin cache is stale                                           | `claude plugin marketplace upgrade dev-kit` (or restart — SessionStart auto-refreshes) |
| New dev-kit skill does not show up after upgrade                     | Cache refreshed but plugin not force-installed                  | `claude plugin install dev-kit --force`                         |
| `team` mode but no role gating feels active                          | `DEV_KIT_MODE=team` requires a `roles:` block in project scope  | Run `/dev-kit:mode team`; declare role names + skill subsets   |

---

## Audit commands (paste-ready)

```bash
# 1. Is dev-kit leaking from user scope?
jq '.enabledPlugins // {}' ~/.claude/settings.json
# Expected: {} or only truly universal plugins.

# 2. Which marketplaces does user scope register?
jq '.extraKnownMarketplaces // {} | keys' ~/.claude/settings.json

# 3. Is dev-kit enabled in this project?
jq '.enabledPlugins // {}' .claude/settings.json

# 4. What mode is this project in?
jq '.env.DEV_KIT_MODE // "<unset>"' .claude/settings.json

# 5. Is the team toggle on?
jq '.env.DEV_KIT_TEAM // "<unset>"' .claude/settings.json

# 6. Personal overrides?
jq '.env // {}' .claude/settings.local.json 2>/dev/null || echo "(no local file)"
```

If line 1 lists any `dev-kit*` entry, fix that first. Everything else follows.

---

## When you do NOT need this workflow

- **You are the dev-harness-kit maintainer and editing this repo itself.**
  Use `claude --plugin-dir <dev-harness-kit-repo>` per
  [`scopes/decision-tree.md`](scopes/decision-tree.md). No settings file
  needed; the source code *is* the plugin definition.
- **You are running a one-off experiment.** Use a session env-var override,
  not a settings file:
  `DEV_KIT_MODE=undev claude --plugin-dir <dev-harness-kit-repo>`.

---

## Related

- [`scopes/README.md`](scopes/README.md) — landing page + 3×3 matrix
- [`scopes/decision-tree.md`](scopes/decision-tree.md) — flowchart
- [`scopes/user-scope.md`](scopes/user-scope.md) — `~/.claude/settings.json`
- [`scopes/project-scope.md`](scopes/project-scope.md) — `<proj>/.claude/settings.json`
- [`scopes/local-scope.md`](scopes/local-scope.md) — `<proj>/.claude/settings.local.json`
- [`scopes/modes.md`](scopes/modes.md) — `DEV_KIT_MODE` + `DEV_KIT_TEAM` resolution
- [`scopes/troubleshooting.md`](scopes/troubleshooting.md) — leakage FAQ