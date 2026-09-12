# Linear auto-trigger hooks

> SessionStart / PostToolUse:Bash / UserPromptSubmit hooks that fire
> `tools/linear_sync.py auto-sync` from the right cwd so a worktree
> creation, a fresh session, or a plan/task shift is reflected in
> Linear immediately, without the user typing anything. All three
> are owner-gated; non-owners bail silently.

## Why three hooks, not one

`hooks/linear-autosync.sh` already fires on every `Edit` / `Write` /
`MultiEdit`. That covers the steady-state case but not three real
workflow events where there is no edit yet:

| Event | Edit hook? | Why it needs a separate trigger |
|---|---|---|
| New worktree | first Edit there is minutes away | the worktree may sit idle before any file is touched; the user expects the issue to exist immediately |
| New session in a worktree | the user often starts with a question, not a save | the first Edit can be many turns in; a session-start sync removes the latency |
| Plan / task change mid-session | the user often says "actually, let me switch to X" before saving | the next Edit can be 5+ minutes away if the user is still planning |

Each hook is a thin shell wrapper that calls `auto-sync` from the
correct cwd. They share the same owner gate (see
[`is_repo_owner`](#owner-gate) below) so a contributor who clones
the repo never has their work silently registered in the owner's
Linear workspace.

## Triggers

### `linear-session-start.sh` — SessionStart

Fires once at every session start inside a Linear-configured
worktree. Triggers one auto-sync round so a fresh session in a
worktree (one cut minutes ago by `worktree-auto-cut.sh`, or opened
manually) is reflected in Linear immediately.

Discriminator (via `hooks/lib/worktree-detect.sh`):

- `WORKTREE_DETECT=worktree` → fire (sync into the worktree's handoff).
- `WORKTREE_DETECT=main` → silent (no per-worktree task yet; the
  worktree-create hook covers the cut case, and the Edit|Write hook
  covers in-place work).
- `WORKTREE_DETECT=outside` → silent (not a git working tree).
- `WORKTREE_DETECT=""` → silent (jq missing — fail open, no-op).

### `linear-worktree-create.sh` — PostToolUse:Bash

Catches a `git worktree add` after the Bash tool returns. The
`worktree-auto-cut.sh` UserPromptSubmit hook covers the auto-cut
case; this hook covers the manual case (e.g. the user runs
`git worktree add -b feat/foo .worktrees/foo origin/main`
themselves) so the new worktree's handoff gets registered before
its first Edit|Write (or first SessionStart, if the user reopens
Claude Code in the new path).

Path resolution: prefer the path parsed out of the bash command
(the only authoritative signal of "which worktree was just
created"). Falls back to the most recent entry in
`git worktree list --porcelain` if the parse fails (e.g. multi-line
bash command, or an exotic flag the parser doesn't know).

## Owner gate

The auto-sync path (`auto_sync` in `tools/linear_sync.py`)
applies a **repo-owner gate** that the manual CLI path
(`/dev-kit:linear` → `sync()`) does NOT. The
gate's resolution order, first match wins:

1. `LINEAR_REPO_OWNER_AUTO_SYNC=1|true` — explicit opt-in
   (escape hatch for forks where `gh auth` points at the
   contributor, not the upstream).
2. `LINEAR_REPO_OWNER_AUTO_SYNC=0|false` — explicit opt-out.
3. Detection: `gh api user --jq .login` must equal the OWNER
   segment of `git remote get-url origin` (case-insensitive).
4. Anything fails (gh missing, no auth, no remote) → False
   (safe default: stay silent, never auto-sync anyway).

The CLI path stays ungated so a contributor who has configured
Linear can still register work explicitly via `/dev-kit:linear`.
The gate is about implicit hook-driven updates, not about
refusing to ever touch the API.

## Wiring

Both `hooks/hooks.json` (Claude Code) and
`.codex-plugin/hooks/hooks.json` (Codex) register the three hooks
in the same event matchers, so behavior is identical across
runtimes. The parity is asserted by two regression tests:
`tests/test_linear_trigger_hooks_wiring.py` (loops over BOTH
JSONs and asserts every hook is wired under the expected
event + matcher in each runtime) and
`tests/test_provider_divergence_wiring.py` (asserts that the
Claude runtime does not register a SessionStart hook that is
absent from Codex).

## See also

- `docs/hooks/linear-autosync.md` — the Edit|Write hook that
  shares the `auto_sync` entry point.
- `skills/linear/SKILL.md` — full reconciliation contract +
  `is_repo_owner` rationale.
- `tools/linear_sync.py` — the Python entry point all four
  hooks converge on.
