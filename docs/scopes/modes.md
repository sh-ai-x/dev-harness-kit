# Modes — `full` / `lite` / `undev`

Set `DEV_KIT_MODE` in `<proj>/.claude/settings.json` `env` block, or via `/dev-kit:mode`, or as a per-session env var.

| Mode    | When                                          | Skills          | Hooks          | Iron Laws       |
|---------|-----------------------------------------------|-----------------|----------------|-----------------|
| `full`  | Multi-session, multi-agent, autonomous        | All 30+         | All 30+        | L1–L9           |
| `lite`  | 4-hour MVP sprint                              | 7 lite subset   | 7 lite subset  | L1–L9 (subset of gates) |
| `undev` | Non-dev / scratchpad / docs-only / random     | none            | none           | none (silent)   |

## Resolution order (highest wins)

| Source | Effective value | Notes |
|---|---|---|
| `$DEV_KIT_MODE` shell env var | wins over everything | per-session override |
| `<proj>/.claude/settings.json` `env.DEV_KIT_MODE` | wins over default | committed project choice |
| `<proj>/.claude/settings.local.json` `env.DEV_KIT_MODE` | wins over default | this checkout only |
| not set | `full` **only when** `enabledPlugins.dev-kit@dev-kit: true`; otherwise `undev` (silent — plugin not loaded) | the conditional default |

## Switching modes

```bash
# Project scope (committed)
/dev-kit:mode lite                  # writes to .claude/settings.json

# Per-session override (no file change)
DEV_KIT_MODE=undev claude --plugin-dir <dev-harness-kit-repo>

# Personal override (gitignored)
/dev-kit:mode lite                  # with --local flag writes to settings.local.json
```

## Why three modes, not four

| Need | Mode |
|---|---|
| Long-running autonomous work, GH-Actions babysit, full OWASP review | `full` |
| Greenfield MVP, 4-hour sprint, manual merges | `lite` |
| Random project that has nothing to do with dev-kit | `undev` (no plugin) |
The third mode is the one that makes the silent default intentional instead of accidental. Today, "undev" means "no plugin enabled" — which is what already happens for projects without `enabledPlugins`. The mode label just makes it explicit and reviewable.

### Team behavior when the toggle is on

`DEV_KIT_TEAM` is a separate capability toggle. When it resolves to
`on`, team collaboration behavior is enabled in the selected `full` or
`lite` mode: the plan skill prompts once per step for upstream
`dependencies:` edges, and `roles` blocks are honored by
`lib/role_config.py`. Bootstrap also keeps `.dev-kit/` tracked. When it
resolves to `off`, those optional behaviors are disabled and `.dev-kit/`
remains gitignored.

- The existing `lib/intent_integrity.py` IC-3 check and
  `lib/dispatch_classifier.py:_has_dependency_edge` consume dependency
  edges without changing the normal non-team plan shape.
- PRD §4 gets a "Dependency DAG" subsection when team collaboration is on.
- The plugin ships no default roles; the operator declares role names
  and per-role skill subsets in the project settings.
- Schema:
  ```jsonc
  {
    "env": {
      "DEV_KIT_MODE": "full",
      "DEV_KIT_TEAM": "on"
    },
    "roles": {
      "active": "<role-name-set-by-operator>",
      "members": {
        "<role-name>":  {"skills": ["dev-kit:<skill>", "..."]}
      }
    }
  }
  ```

Setting `DEV_KIT_MODE=team` is invalid and is ignored by the mode
resolver. A project that carries `roles:` must resolve
`DEV_KIT_TEAM=on`; otherwise `lib/role_config.py` raises
`RoleConfigError` and fails closed.

## Mode + plugin-enable interaction

`undev` means the plugin is **off**, not "the plugin is on but with lite behavior". If `enabledPlugins.dev-kit@dev-kit: true` and `DEV_KIT_MODE=undev`, the plugin is on (full hooks fire) but the mode label is misleading. To actually be undev:

```jsonc
// .claude/settings.json
{
  "enabledPlugins": {},                  // ← empty
  "env": { "DEV_KIT_MODE": "undev" }     // ← explicit label
}
```

The resolution-order table above already encodes this: the default-conditional-on-plugin-enabled row is the same "silent undev" rule the conditional default implements.

## Team toggle (`DEV_KIT_TEAM`) — orthogonal to mode

`DEV_KIT_MODE` and `DEV_KIT_TEAM` are independent env-vars. The mode
decides **which skills/hooks fire**; the team toggle decides **whether
team collaboration behavior is enabled** and whether `.dev-kit/` is
tracked in git. Both can be set independently and combine freely.

| Mode | team OFF (default) | team ON |
|---|---|---|
| `full` | full dev-kit (30+ skills/hooks), `.dev-kit/` gitignored | full dev-kit, team roles/dependencies enabled, `.dev-kit/` tracked |
| `lite` | lite 7/7 subset, `.dev-kit/` gitignored | lite 7/7 subset, team roles/dependencies enabled, `.dev-kit/` tracked |
| `undev` | plugin off (team toggle is a no-op since plugin disabled) | plugin off (same — team behavior is inactive) |

### Resolution order (highest wins)

| Source | Effective value | Notes |
|---|---|---|
| `$DEV_KIT_TEAM` shell env var | wins over everything | per-session override |
| `<proj>/.claude/settings.json` `env.DEV_KIT_TEAM` | wins over default | committed project choice |
| `<proj>/.claude/settings.local.json` `env.DEV_KIT_TEAM` | wins over default | this checkout only |
| not set | `off` (silent default — team toggle is opt-in) | unconditional default |

### Switching team toggle

```bash
# Project scope (committed)
/dev-kit:team on                   # writes DEV_KIT_TEAM=1 to .claude/settings.json

# Personal override (gitignored)
/dev-kit:team on --scope local      # writes to .claude/settings.local.json

# Per-session override (no file change)
DEV_KIT_TEAM=on claude --plugin-dir <dev-harness-kit-repo>

# Inspect
/dev-kit:team --show
```

See [`skills/team/SKILL.md`](../../skills/team/SKILL.md) and
[`hooks/lib/team-resolve.sh`](../../hooks/lib/team-resolve.sh) for the
implementation.
