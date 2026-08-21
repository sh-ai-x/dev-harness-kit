---
name: log
category: shortcuts
description: Toggle /log setup|on|off|status — install/remove loghooks from ~/dev/loghooks into the current project's Claude/Codex settings.
alpha: state
when_to_use: |
  - User types /dev-kit:log on
  - User types /dev-kit:log off
  - User types /dev-kit:log setup
  - User types /dev-kit:log status
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch Agent
model: haiku
disable-model-invocation: false
---
> [← Skills index](../../README.md)

# /dev-kit:log — Toggle loghooks on/off

Wraps the standalone `~/dev/loghooks` repo (Stop + SessionEnd transcripts)
as a one-command on/off per project. Hooks are tagged with a sentinel
so off is precise — never touches the user's pre-existing hooks.

## Iron Law

**Hooks merge, not replace. Off is sentinel-scoped, not "rm -rf".**
Every entry this skill installs carries `_loghooks_managed=true`; off
removes only those entries. Existing user hooks are always preserved.

## Subcommands

| Subcommand | What it does | Idempotent |
|---|---|---|
| `setup` | Copy `tools/save_log.py` + scaffold `logs/` in target | Yes (refresh by default; `--force` to no-op even when sha matches) |
| `on` | Merge hooks from `~/dev/loghooks` into target's `.claude/settings.json` + `.codex/hooks.json` | Yes (replace-by-command, not duplicate) |
| `off` | Strip only `_loghooks_managed=true` entries from target's settings | Yes |
| `status` | Show installed-entry count + captured transcript count per target | Read-only |

Default subcommand when none given: `status`.

## Resolution

| Knob | Default | Env |
|---|---|---|
| Source repo | `~/dev/loghooks` | `LOGHOOKS_DIR` |
| Target project | `$PWD` | `TARGET_DIR` |
| Global target (`--global`) | `$HOME/.claude/` | `HOME` |

`jq` is required (the worktree rule-hooks already depend on it).

## Behavior (delegated to scripts/)

1. **Detect subcommand** from `$ARGUMENTS`. If empty → `status`.
2. **Source `scripts/lib.sh`** (sentinel + JSON merge/remove/count helpers).
3. **Dispatch** to the matching `scripts/log-<sub>.sh`.
4. Each script does its own arg parsing + jq + atomic write.
5. SKILL.md body is documentation; the scripts are the contract.

## Flags

| Flag | Subcommands | Effect |
|---|---|---|
| `--target DIR` | all | Override target project |
| `--global` | setup, on, off | Install to `$HOME/.claude/` (settings + save_log.py) so a SINGLE install captures every project / worktree on the machine. Mutually exclusive with `--target` and `--all-worktrees`. See [Global install](#global-install-recommended) below. |
| `--all-worktrees` | setup | Run `setup` + `on` recursively for every `.worktrees/*/` dir under `--target`. Use after `--target` on the main checkout to backfill sibling worktrees. Mutually exclusive with `--global`. |
| `--force` | setup | Overwrite `tools/save_log.py` even when local sha matches |
| `--claude-only` | on, off | Touch only `.claude/settings.json` |
| `--codex-only` | on, off | Touch only `.codex/hooks.json` (no-op if source has no codex config) |

## Setup → On → Off → Status flow

```
1. /dev-kit:log setup     # creates tools/save_log.py + logs/{claude-code,codex}/
2. /dev-kit:log on        # merges Stop + SessionEnd hooks into .claude/settings.json
3.  ... run Claude Code ... transcripts appear in logs/claude-code/<branch>/<sid>.jsonl
4. /dev-kit:log status    # managed=N captured=N (recursive, per-branch)
5. /dev-kit:log off       # strips managed entries; scaffold left in place
```

Transcripts are grouped by `gitBranch` — one subdir per branch (`main`,
`feature-x`, …), with `detached-<sha>` for commits checked out by SHA and
`no-git` for non-git cwd. The token analyzer reads branch from the
`gitBranch` wire field and renders a "Cost by Branch" panel.

### Cleanup-safe external retention

Set `AGENT_LOG_ROOT` before enabling hooks to store canonical telemetry outside
the repository and every worktree:

```bash
export AGENT_LOG_ROOT="$HOME/.agent_logs"
/dev-kit:log on --global
```

The path is `AGENT_LOG_ROOT/<repository>/<tool>/<branch>/`. Each capture is
written atomically and receives a `.meta.json` sidecar containing repository,
branch, worktree, session, tool, and timestamp metadata. Common credentials
(API keys, bearer tokens, passwords, and GitHub/OpenAI token-shaped values) are
redacted before persistence. Without the variable, the existing main-checkout
`logs/` path and worktree attribution mirror remain unchanged.
`/dev-kit:token-analyzer` automatically scans the external repository bucket
and reads the sidecar's `worktree` field, so Cost by Worktree attribution still
works after the source worktree is removed.

`off` deliberately leaves `tools/save_log.py` + `logs/` in place — they
cost nothing and a future `on` skips the setup step. Remove them
manually if you really want a clean slate.

## Global install (recommended)

```
1. /dev-kit:log setup --global   # writes $HOME/.claude/save_log.py
2. /dev-kit:log on   --global    # writes $HOME/.claude/settings.json hooks
3.  ... run Claude Code ANYWHERE ... transcripts land in <main_repo>/logs/...
4. /dev-kit:log off  --global    # strips managed entries from $HOME/.claude/settings.json
```

**Why `--global` exists.** Per-project install places hooks at
`<project>/.claude/settings.json` — which only fires for sessions that
start inside that one checkout. A developer with 30 sibling worktrees
sees 1 worktree capture per `git worktree add`. `--global` writes to
`$HOME/.claude/settings.json` instead, so Claude Code picks up the
hooks regardless of cwd. The hook command is rewritten at install time
to `${HOME}/.claude/save_log.py` so the install is self-contained — no
per-project copy required. `save_log.py` (already unified via
`find_main_repo_root` → `git rev-parse --git-common-dir`) routes every
captured transcript to the main repo's `logs/` regardless of which
worktree the session ran in.

**When to use which.**
- `--global`: recommended default. One install covers every project /
  worktree / machine login. Sessions are captured the first time you
  start Claude Code, no `git worktree add` ritual.
- `--target <dir>` + `--all-worktrees`: legacy per-project install,
  useful when you want one project explicitly opted in and others
  untouched (e.g. shared CI machines, restricted sandboxes).

**Coexistence.** `--global` and per-project installs are independent
hooks trees. `log-off --global` strips only the global managed entries;
per-project `log-off --target <dir>` is unaffected.

## Hand-off

No hand-off to another stage. Pure utility.
