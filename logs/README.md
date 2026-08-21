# logs/

Conversation transcripts captured by the `/dev-kit:log` hooks.

## What's in here

| Path | Source | Contents |
|---|---|---|
| `claude-code/<branch>/` | Claude Code `Stop` + `SessionEnd` hooks | One `.jsonl` per session, grouped by `gitBranch` |
| `codex/<branch>/` | Codex `Stop` + `SessionEnd` hooks | One `.jsonl` per session, grouped by `gitBranch` |

Captured files are gitignored (`logs/.gitignore` ignores `*.jsonl`). Only the empty subdirs are tracked, via `.gitkeep`. The transcripts stay local — they are NOT shipped, NOT pushed, NOT indexed by the eval harness.

For cleanup-safe retention, set `AGENT_LOG_ROOT` before enabling the hooks. The
canonical path becomes `$AGENT_LOG_ROOT/<repository>/...`, outside the main
checkout and all worktrees. Each capture is written atomically and gets a
`<session>.meta.json` sidecar containing repository, branch, worktree, session,
tool, and timestamp metadata. The default remains the main-checkout `logs/`
path for backward compatibility.
When `AGENT_LOG_ROOT` is set, `/dev-kit:token-analyzer` also discovers the
external repository bucket and uses the sidecar `worktree` value for Cost by
Worktree attribution, even after the originating worktree is removed.

The `<branch>` bucket is one of:

- `<branch-name>` — attached HEAD on a real branch (e.g. `main`, `feature-x`).
- `detached-<short-sha>` — `git checkout <commit>` / CI checkout of a tag.
- `no-git` — non-git cwd OR `git` binary missing on PATH.

The tokenizer reads the branch from each JSONL line's top-level `gitBranch` field (Claude Code sets this on every record), with a path-based fallback for legacy flat files. `/dev-kit:token-analyzer` filters with `--branch <name>` and renders a "Cost by Branch" panel.

## Why this exists

`/dev-kit:log` wraps the standalone [`~/dev/loghooks`](https://github.com/sh-ai-x/loghooks) repo as a one-command on/off per project. Hooks are tagged with a sentinel (`_loghooks_managed=true`) so:

- `on`  merges `Stop` + `SessionEnd` entries into `.claude/settings.json` + `.codex/hooks.json` without touching your pre-existing hooks.
- `off` strips only the sentinel-tagged entries. Your hooks stay.

This folder + `tools/save_log.py` are the runtime artifacts. Both are scaffolded once via `/dev-kit:log setup`; a future `on` skips the setup step.

## Quick start

```bash
# Once per project
/dev-kit:log setup     # copies tools/save_log.py + creates logs/{claude-code,codex}/
/dev-kit:log on        # merges hooks into .claude/settings.json + .codex/hooks.json

# ... use Claude Code / Codex — transcripts land in logs/<tool>/<branch>/<sid>.jsonl ...

/dev-kit:log status    # managed=N captured=N (read-only)
/dev-kit:log off       # strips sentinel-tagged entries; scaffold left in place
```

## Source

The hook payloads come from `${HOME}/dev/loghooks` (override with `LOGHOOKS_DIR`). The skill scripts live at `skills/log/scripts/{log-setup,log-on,log-off,log-status}.sh`. See `skills/log/SKILL.md` for the full contract.

## Cleanup

`off` deliberately leaves `tools/save_log.py` + `logs/` in place — they cost nothing and a future `on` is a no-op setup. To remove everything:

```bash
git rm -rf tools/save_log.py logs/
```

## Per-skill usage telemetry

`tools/skill_usage.py` and the `--skill-usage` flag on `tools/session_monitor.py`
turn the two distinct signals in every captured JSONL into a standing
data feed, so future cut/merge calls don't have to re-aggregate by hand.

### Two signals

| Signal | Source field | What it counts |
|---|---|---|
| `turns` | top-level `attributionSkill` on each assistant message | Depth / work done by the skill — one `/dev-kit:foo` kick that orchestrates 14 sub-agents adds 14 turns. |
| `invocations` | `tool_use` block with `name == "Skill"`, `input.skill == "<name>"` | Distinct human (or skill-driven) kicks — the user explicitly asked for the skill to run. |

Both are tracked separately. The same skill can have many turns and few
invocations (a babysitter / maintenance loop) or many of both (a heavy
hitter). `last_seen` is the maximum `timestamp` observed per skill.

### CLI

```bash
# Top 20 skills, 30-day window, default logs dir
python3 tools/skill_usage.py

# Just self-dev (cwd-prefix scoped to this repo)
python3 tools/skill_usage.py --cwd /Users/.../dev-harness-kit

# One target project (separates target-project usage from self-dev)
python3 tools/skill_usage.py --cwd /Users/.../my-target

# All-time, JSON for piping into other tools
python3 tools/skill_usage.py --days 0 --json

# Per-cwd breakdown (every distinct cwd that touched each skill)
python3 tools/skill_usage.py --json --per-cwd
```

Output is sorted by `turns` desc, ties broken by `invocations` desc.
The default window is 30 days; pass `--days 0` to disable.

### session_monitor integration

`tools/session_monitor.py --list --skill-usage` adds a per-worktree
`TOP SKILLS:` line (3 skills) and a global top-10 panel at the bottom:

```text
  ▸ feat/p5  [live]  (2 sessions)  last: "feat: p5 telemetry"
    TOP SKILLS: dev-kit:feat-fix:14 inv:3  dev-kit:inspect:5 inv:1
```

The `--skill-usage` flag aggregates once per `--skill-days N` window
(default 30); pass `--skill-days 0` to disable the window.

### Interpretation

* **High turns + low invocations** — babysitter or maintenance loop.
  The skill does a lot of work per kick. Often worth keeping (e.g.
  `dev-kit:babysit-pr`).
* **Both low** — prune candidate. Few turns *and* few distinct kicks.
  Remove without much regret.
* **High turns + high invocations** — heavy hitter. Verify it's actually
  shipping value per invocation, not just absorbing cycles.
* **High turns, zero invocations** — likely auto-attributed by a
  pre-tool hook (e.g. an agent that names the skill in its output). Not
  a kick but worth tracking.

The `dont-cut-heavily-used-skills` rule (see `MEMORY.md`) gates removal
on the `turns` number, not the `invocations` count — a babysitter with
zero kicks but thousands of turns is still load-bearing.
