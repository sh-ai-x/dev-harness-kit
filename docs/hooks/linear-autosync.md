# `linear-autosync.sh`

> PreToolUse hook for `Edit` / `Write` / `MultiEdit`. Fires
> `tools/linear_sync.py` so every Claude Code edit is reflected in the
> user's Linear workspace without a manual `/dev-kit:linear` invocation.

## What it does

The hook is a thin shell wrapper. It:

1. Pulls `cwd` from the JSON payload sent by Claude Code on every
   Edit/Write/MultiEdit. Falls back to `$CLAUDE_PROJECT_DIR` or `pwd`
   when the payload omits the field.
2. **`cd` into the project dir** so the Python script can locate its
   own `tools/linear_sync.py` via a relative path.
3. **PROJECT_DIR guard** (added 2026-08-06, PR #590): if
   `$PROJECT_DIR/tools/linear_sync.py` does not exist, exit 0 silently.
   This is the cross-project plugin-share case — other Claude Code
   projects that clone just `hooks/linear-autosync.sh` (without
   `tools/`) would otherwise print "No such file or directory" on
   every Edit. The silent-bail preserves the non-blocking contract.
4. **Env fast-path**: if no activation source is present (no
   `LINEAR_API_KEY` env var, no user-scope `~/.config/dev-kit/.env`,
   no per-worktree `.dev-kit/.env.linear` or `linear-config.json`,
   no legacy `.dev-kit/.enabled.json`), exit 0 without forking
   Python. This is the cheap path — a single bash conditional check
   for the most common case (Linear is not configured).
5. **Python fork**: if any activation source is set, run
   `tools/linear_sync.py` via the first `python3` / `python` / `py`
   in `$PATH`. The Python script is the authoritative gate (config
   validation, GraphQL calls, handoff writes); the shell wrapper
   just provides the fast-path.

The hook always exits 0. Transport failures, missing tokens, GraphQL
errors, and any other problem inside the Python script are reported
to stderr (visible under `LINEAR_DEBUG=1`) but never block the Edit.
This is the non-blocking contract documented in the parent
[SKILL.md](../../skills/linear/SKILL.md) and the issue thread (#539).

## Task-description resolution (`_resolve_prompt`)

The Python script resolves the "current task" string via
`tools/linear_sync.py::_resolve_prompt`. The priority order is
**commit-subject-first with a branch-name fallback** so a fresh
worktree cut from `origin/main` still produces a usable prompt on
its very first Edit|Write (otherwise the skip gate bails on the
`chore(release): ...` commit subject and the user reads this as
"Linear auto-update isn't working"):

1. **Latest commit subject with a work verb** (`implement`, `build`,
   `fix`, `refactor`, `add`, `create`, `update`, `remove`, `delete`,
   `ship`, `migrate`, `wire`, `integrate`, `sync`, `register`,
   `track`). The freshest signal of what the operator is actually
   working on; a new commit in the same worktree updates this
   immediately so the script never gets stuck on a previous task's
   prompt.
2. **Branch name with a work verb**. Falls back here on a fresh
   worktree still pointing at `origin/main`'s release commit (no
   work verb). The branch `<type>/<slug>` format carries the signal
   (e.g. `fix/...`, `refactor/...`). Without this fallback the
   auto-sync silently skipped on every first Edit|Write of every
   task branch — closed by [PR #661](https://github.com/sh-ai-x/dev-harness-kit/pull/661).
3. **Any commit subject** (fall-through to the `_should_skip_prompt`
   gate below).
4. **Branch name** as a final fallback (e.g. a brand-new worktree
   with no commit yet).

The active hand-off's `prompt` field is **not** consulted as a
resolution source (per adversarial Codex review on [#543](https://github.com/sh-ai-x/dev-harness-kit/issues/543))
— trusting a cached prompt would let a stale task shadow the
current one forever. The hand-off caches the issue reference, not
the task description.

The resolved prompt then runs through
`tools/linear_sync.py::_should_skip_prompt`, which short-circuits on
the `_SKIP_MARKERS` prefix list (`/`, `#`, `!`, `ls `, `cat `, `grep `,
`git status`, …) and on the missing-work-verb case. A release commit
on the `main` checkout (no work verb in either signal) still skips
— the fix is worktree-scoped, not a permission to spam Linear from
the main checkout.

## Cross-reference

- [skills/linear/SKILL.md](../../skills/linear/SKILL.md) — user-facing
  skill that wraps the same Python script for explicit invocations
  (`/dev-kit:linear on`, `/dev-kit:linear list`, etc.).
- [docs/skills/linear.md](../skills/linear.md) — public docs page for
  the linear skill.
- [tools/linear_sync.py](../../tools/linear_sync.py) — the Python
  implementation (the hook only invokes it; all behavior lives here).
- [HOOK-REFERENCE.md](./HOOK-REFERENCE.md) — the hook index.
