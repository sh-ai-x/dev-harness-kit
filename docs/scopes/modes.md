# Modes — `full` / `lite` / `undev` / `mod`

Set `DEV_KIT_MODE` in `<proj>/.claude/settings.json` `env` block, or via `/dev-kit:mode`, or as a per-session env var.

| Mode    | When                                          | Skills          | Hooks          | Iron Laws       |
|---------|-----------------------------------------------|-----------------|----------------|-----------------|
| `full`  | Multi-session, multi-agent, autonomous        | All 30+         | All 30+        | L1–L9           |
| `lite`  | 4-hour MVP sprint, 6-person team              | 7 lite subset   | 7 lite subset  | L1–L9 (subset of gates) |
| `undev` | Non-dev / scratchpad / docs-only / random     | none            | none           | none (silent)   |
| `mod`   | Multi-role team / dependency-aware plan       | All 30+ (gated by role) | All 30+ (mod subset) | L1–L9 |

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

## Why four modes, not three

| Need | Mode |
|---|---|
| Long-running autonomous work, GH-Actions babysit, full OWASP review | `full` |
| Greenfield MVP, 6-person team, 4-hour sprint, manual merges | `lite` |
| Random project that has nothing to do with dev-kit | `undev` (no plugin) |
| Multi-role team where you need a named persona per session and explicit step dependencies in your plan output | `mod` |

`mod` is the user-defined-role + dependency-aware-plan mode. **Roles are not shipped** — the operator declares role names + per-role skill subsets during `/dev-kit:mode mod`, mirroring the dev-harness-kit-lite pattern. The mode value alone is the gate; no pre-wired role subset is enforced.

The third mode is the one that makes the silent default intentional instead of accidental. Today, "undev" means "no plugin enabled" — which is what already happens for projects without `enabledPlugins`. The mode label just makes it explicit and reviewable.

## `mod` mode specifics

- Plan skill (Gate 4/5) prompts once per step for upstream `dependencies:` edges and writes them into `step<N>.md`. The existing `lib/intent_integrity.py` IC-3 check + `lib/dispatch_classifier.py:_has_dependency_edge` consume them for free.
- PRD §4 gets a "Dependency DAG" subsection listing each edge.
- `roles` block in `<proj>/.claude/settings.json` is honored (resolved by `lib/role_config.py`); the plugin ships no defaults.
- Schema:
  ```jsonc
  {
    "roles": {
      "active": "<role-name-set-by-operator>",
      "members": {
        "<role-name>":  {"skills": ["dev-kit:<skill>", "..."]}
      }
    }
  }
  ```
- Setting `DEV_KIT_MODE=full|lite|undev` in a project that carries a `roles:` block raises `RoleConfigError` from `lib/role_config.py` — the gate is fail-closed.

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
