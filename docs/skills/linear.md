> [← Skills index](README.md) · [Project README](../../README.md)

# `linear`

**Category:** `config` · **Alpha:** `state` · **Invocation:** `/dev-kit:linear` (human-invoked)

`linear` is an **optional** Linear task tracker. It reconciles the current repository task with a canonical project and a non-duplicate issue. Auto-syncs on every `Edit` / `Write` / `MultiEdit` when configured. The skill itself describes the reconciliation contract; the actual sync lives in `tools/linear_sync.py` and is invoked through `hooks/linear-autosync.sh`. Linear failures never block normal work.

## When to use it

- The user types `/dev-kit:linear` (no args) to run one auto-sync round.
- The user types `/dev-kit:linear on | off | free-tier-cleanup on|off|status | status | setup | project-name <name>` to manage the per-worktree config.
- A workflow skill starts a new implementation, debugging, refactor, or plan task and wants the work tracked in Linear.
- Every `Edit | Write | MultiEdit` should fire the auto-sync hook (only when the worktree config is enabled).

## Subcommands

| Subcommand | Effect |
|---|---|
| `/dev-kit:linear` (no args) | Run one auto-sync round (re-evaluates the current task and creates/updates the matching Linear issue). |
| `/dev-kit:linear on` | Enable auto-sync in this worktree. Writes `enabled: true` to `<worktree>/.dev-kit/linear-config.json`. |
| `/dev-kit:linear off` | Disable auto-sync in this worktree. Project name and team id are preserved. |
| `/dev-kit:linear setup` | Print the one-time setup checklist + the current state (whether `LINEAR_API_KEY` is set, the resolved project name, whether the worktree config exists). |
| `/dev-kit:linear project-name <name>` | Override the auto-detected project name for this worktree. Without an argument, prints the resolved name. |
| `/dev-kit:linear free-tier-cleanup on\|off\|status` | Optional recovery mode. On a confirmed free issue-limit error, archive up to 10 oldest non-terminal issues in the active project and retry once. Off by default. |
| `/dev-kit:linear status` | Print a JSON snapshot of the resolved state (worktree path, slug, config, env-var presence, resolved project + team). |

Each subcommand delegates to `tools/linear_sync.py`, which is the authoritative implementation. The skill exists so the user does not have to remember the script path; the script exists so the hook, the skill, and any future caller share one code path.

## Auto-sync trigger

When Linear is configured (`LINEAR_API_KEY` env var OR per-worktree `.dev-kit/linear-config.json:enabled` OR legacy `.dev-kit/.enabled.json:mcp.linear` ∈ {`auto`, `on`}), `hooks/linear-autosync.sh` runs `tools/linear_sync.py` before every `Edit` / `Write` / `MultiEdit`. The optional `free_tier_cleanup` config flag is disabled by default and only applies to the exact free issue-limit error; it never turns general API failures into archive operations. The script:

1. Resolves the task description in priority order: the active hand-off's `prompt` field → the latest commit subject on the current branch → the branch name. The hand-off is keyed by worktree slug, so two parallel sessions in two worktrees never share or overwrite each other's state.
2. Skips read-only / non-task prompts (`/`, `#`, `!`, `ls `, `cat `, `grep `, `git status`, and prompts that lack a work verb).
3. Finds or creates the project named after the configured project-name (per-worktree override) or the repository basename.
4. Searches for an open issue whose `description` starts with `<!-- scope:<branch>::<prompt-head> -->`.
5. Updates the existing issue OR creates a new one with the same scope marker.
6. Writes the updated handoff at `.dev-kit/hand-off/linear/<worktree-slug>.json` so the next edit reuses the same issue.

The script always returns exit code 0. Transport errors, missing tokens, and GraphQL failures are logged to stderr and never block the edit. Users without Linear configured are unaffected — the hook fast-paths on missing env var and config.

## Setup

The API key is **never** read from or written to disk; set it once in your shell environment (e.g. `export LINEAR_API_KEY=...` in `~/.zshrc`). Two equivalent entry points:

```bash
# Option A — shell env (recommended for shared machines)
export LINEAR_API_KEY=<your-linear-api-token>     # https://linear.app/settings/api
cd .worktrees/<your-worktree>
python3 tools/linear_sync.py on
python3 tools/linear_sync.py project-name "<name>"    # optional

# Option B — per-worktree env file (recommended for solo dev)
```

The CLI writes the config at `<repo>/.dev-kit/linear-config.json` (untracked) and the handoff at `<repo>/.dev-kit/hand-off/linear/<worktree-slug>.json`.

## Failure policy

- If this is an implicit workflow call and Linear is disabled or unavailable, return `LINEAR_SKIP` and let the caller continue.
- If this is an explicit `/dev-kit:linear` call and Linear is unavailable, report the missing connection/setup clearly; do not pretend the task was registered.
- Do not invoke Linear for read-only work such as `inspect`, `review`, `security`, or `code-viz` unless the user explicitly requests registration.

## Related

- [`skills/linear/SKILL.md`](../../skills/linear/SKILL.md) — the skill definition.
- [`tools/linear_sync.py`](../../tools/linear_sync.py) — authoritative implementation.
- [`hooks/linear-autosync.sh`](../../hooks/linear-autosync.sh) — the auto-sync hook entry point.
