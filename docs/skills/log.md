> [← Skills index](README.md) · [Project README](../../README.md)

# `log`

**Category:** `shortcuts` · **Alpha:** `state` · **Invocation:** `/dev-kit:log setup|on|off|status` (human-invoked)

Wraps the standalone `~/dev/loghooks` repo (Claude Code `Stop` + `SessionEnd`
transcript capture, plus Codex equivalents) as a one-command on/off toggle
per project. Every hook entry it installs is tagged with a sentinel
(`_loghooks_managed=true`), so `off` is precise — it never touches hooks you
already had configured yourself.

## When to use it

- You type `/dev-kit:log setup` to scaffold transcript capture in a new project.
- You type `/dev-kit:log on` / `off` to toggle capture.
- You type `/dev-kit:log status` to see how many hooks are installed and how
  many transcripts have been captured.

## How it works

Default subcommand when none is given: `status`. The skill body is pure
documentation — the actual behavior lives in `scripts/log-<sub>.sh`, each of
which does its own arg parsing + `jq` + atomic write:

| Subcommand | What it does | Idempotent |
|---|---|---|
| `setup` | Copies `tools/save_log.py` + scaffolds `logs/` in the target | Yes — refreshes by default; `--force` to no-op even when the sha matches |
| `on` | Merges hooks from `~/dev/loghooks` into the target's `.claude/settings.json` + `.codex/hooks.json` | Yes — replace-by-command, not duplicate |
| `off` | Strips only `_loghooks_managed=true` entries from the target's settings | Yes |
| `status` | Shows installed-entry count + captured-transcript count per target | Read-only |

`jq` is required (the worktree rule-hooks already depend on it).

**Resolution knobs:**

| Knob | Default | Env override |
|---|---|---|
| Source repo | `~/dev/loghooks` | `LOGHOOKS_DIR` |
| Target project | `$PWD` | `TARGET_DIR` |
| Global target (`--global`) | `$HOME/.claude/` | `HOME` |

Captured transcripts are grouped by `gitBranch` — one subdirectory per branch
(`main`, `feature-x`, …), with `detached-<sha>` for SHA checkouts and
`no-git` for a non-git cwd. `/dev-kit:token-analyzer` reads the `gitBranch`
wire field to render its "Cost by Branch" panel. `off` deliberately leaves
`tools/save_log.py` and `logs/` in place — they cost nothing, and a future
`on` skips the setup step.

## Usage

```bash
/dev-kit:log setup [--global] [--force]
/dev-kit:log on    [--global] [--claude-only] [--codex-only]
/dev-kit:log off   [--global] [--claude-only] [--codex-only]
/dev-kit:log status
```

| Flag | Subcommands | Effect |
|---|---|---|
| `--target DIR` | all | Override target project |
| `--global` | setup, on, off | Install to `$HOME/.claude/` so ONE install captures every project/worktree on the machine. Mutually exclusive with `--target` and `--all-worktrees`. |
| `--all-worktrees` | setup | Runs `setup` + `on` recursively for every `.worktrees/*/` dir under `--target`; backfills sibling worktrees. Mutually exclusive with `--global`. |
| `--force` | setup | Overwrites `tools/save_log.py` even when the local sha matches |
| `--claude-only` | on, off | Touches only `.claude/settings.json` |
| `--codex-only` | on, off | Touches only `.codex/hooks.json` (no-op if the source has no Codex config) |

### Typical flow

```
1. /dev-kit:log setup     # creates tools/save_log.py + logs/{claude-code,codex}/
2. /dev-kit:log on        # merges Stop + SessionEnd hooks into .claude/settings.json
3.  ... run Claude Code ... transcripts appear in logs/claude-code/<branch>/<sid>.jsonl
4. /dev-kit:log status    # managed=N captured=N (recursive, per-branch)
5. /dev-kit:log off       # strips managed entries; scaffold left in place
```

### Global install (recommended)

```
1. /dev-kit:log setup --global   # writes $HOME/.claude/save_log.py
2. /dev-kit:log on   --global    # writes $HOME/.claude/settings.json hooks
3.  ... run Claude Code ANYWHERE ... transcripts land in <main_repo>/logs/...
4. /dev-kit:log off  --global    # strips managed entries from $HOME/.claude/settings.json
```

**Why `--global` exists.** A per-project install places hooks at
`<project>/.claude/settings.json`, which only fires for sessions started
inside that one checkout — a developer with 30 sibling worktrees would see
only 1 worktree captured per `git worktree add`. `--global` writes to
`$HOME/.claude/settings.json` instead, so Claude Code picks up the hooks
regardless of cwd; the hook command is rewritten at install time to
`${HOME}/.claude/save_log.py` so the install is self-contained. `save_log.py`
(unified via `find_main_repo_root` → `git rev-parse --git-common-dir`) routes
every captured transcript to the main repo's `logs/` regardless of which
worktree the session ran in.

Use `--global` as the recommended default; reach for `--target <dir>` +
`--all-worktrees` only when you want one project explicitly opted in while
others stay untouched (shared CI machines, restricted sandboxes).
`--global` and per-project installs are independent hooks trees — `log off
--global` strips only the global managed entries, and a per-project
`log off --target <dir>` is unaffected.

### Cleanup-safe external retention

Set `AGENT_LOG_ROOT` in the hook environment to move canonical captures
outside the repository and every worktree:

```bash
export AGENT_LOG_ROOT="$HOME/.agent_logs"
/dev-kit:log on --global
```

Captures are stored under `AGENT_LOG_ROOT/<repository>/<tool>/<branch>/`.
Writes use a same-directory temporary file plus `fsync`/`os.replace`, and each
session has a `.meta.json` sidecar with repository, branch, worktree, session,
tool, and timestamp metadata. Common API keys, bearer tokens, passwords, and
GitHub/OpenAI token-shaped values are redacted before persistence. The default
main-checkout `logs/` path remains unchanged for compatibility.
When `AGENT_LOG_ROOT` is set, `/dev-kit:token-analyzer` automatically discovers
the repository's external log bucket and uses the sidecar's `worktree` field for
Cost by Worktree attribution, including after the source worktree is removed.

### Cleanup-safe worktree removal

The `bin/worktree-remove-safe.sh` wrapper complements `AGENT_LOG_ROOT` by
archiving a worktree's `logs/` tree into the durable store **before** `git
worktree remove` deletes the directory. Default archive target mirrors the
Stop-hook write path:

| `AGENT_LOG_ROOT` | Archive target |
|---|---|
| unset | `<main>/logs/.archive/<branch>/<ts>/` |
| set | `<AGENT_LOG_ROOT>/<repo>/.archive/<branch>/<ts>/` |

```bash
# Default: warn on archival error, do not block removal
bin/worktree-remove-safe.sh /path/to/repo.worktrees/feat-x

# Forward args to git worktree remove after --
bin/worktree-remove-safe.sh /path/to/repo.worktrees/feat-x -- --force

# Strict mode: block removal if archival fails (CI / automation)
DEV_KIT_WORKTREE_REMOVE_STRICT=1 bin/worktree-remove-safe.sh /path/to/repo.worktrees/feat-x
```

The wrapper is idempotent (copy, not move) and fail-safe (returns a JSON
status dict, never raises). See `tools/worktree_cleanup.py` for the
programmatic contract and `tests/test_worktree_cleanup.py` for the
contract test. The wrapper closes the second half of issue #689.

## Output

No file artifact of its own beyond the scaffolded `logs/{claude-code,codex}/`
directories and the merged hook entries in `.claude/settings.json` /
`.codex/hooks.json`.

## Related

- [`token-analyzer`](token-analyzer.md) — consumes the transcripts this skill captures.
- [`cost-gate`](cost-gate.md) — also reads the captured transcripts.
- No hand-off to another stage — pure utility.

---
*Source: [`skills/log/SKILL.md`](../../skills/log/SKILL.md)*
