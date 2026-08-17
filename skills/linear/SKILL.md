---
name: linear
category: config
description: Optional Linear task tracker. Reconcile the current repository task with a canonical project and non-duplicate issue. Auto-syncs on every Claude Code edit when configured. Owner-gated auto-triggers also fire on worktree create, session start, and task change.
alpha: state
when_to_use: |
  - User types /dev-kit:linear
  - User types /dev-kit:linear on | off | status | setup | project-name <name>
  - A workflow skill starts a new implementation, debugging, refactor, or plan task
  - The user asks to register, reconcile, or update work in Linear
  - Every Edit|Write|MultiEdit fires the auto-sync hook (when configured)
  - A new worktree is created (fires linear-worktree-create)
  - A new session starts in a worktree (fires linear-session-start)
  - A UserPromptSubmit signals a scope change (fires linear-task-change)
allowed-tools: Read Write Bash Glob
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../README.md)

## What it does

`linear` is an optional task-tracking skill. It can be invoked directly by a user, called once by a workflow skill at task start, or fired automatically on every Edit|Write when Linear is configured. The skill itself describes the reconciliation contract; the actual sync is implemented by `tools/linear_sync.py` and invoked through `hooks/linear-autosync.sh`. The skill never treats an existing handoff as proof of registration, and it never blocks normal work when Linear is unavailable.

## Optional capability

Resolve the current repository name and the user's Linear capability before making a request.

- If this is an implicit workflow call and Linear is disabled or unavailable, return `LINEAR_SKIP` and let the caller continue.
- If this is an explicit `/dev-kit:linear` call and Linear is unavailable, report the missing connection/setup clearly; do not pretend the task was registered.
- If `.dev-kit/.enabled.json` exists, respect its Linear/MCP selection. Missing configuration means `auto`, not a hard failure.
- Do not invoke Linear for read-only work such as inspect, review, security, or code-viz unless the user explicitly requests registration.

## Auto-sync trigger (every Edit|Write)

When Linear is configured (`LINEAR_API_KEY` env var OR user-scope `~/.config/dev-kit/.env` OR per-worktree `.dev-kit/.env.linear` OR per-worktree `.dev-kit/linear-config.json:enabled` OR legacy `.dev-kit/.enabled.json:mcp.linear` ∈ {`auto`, `on`}), `hooks/linear-autosync.sh` runs `tools/linear_sync.py auto-sync` before every Edit|Write|MultiEdit. The script:

1. Resolves the task description in priority order:
   1. **Latest commit subject on the current branch**, when it carries a work verb (`implement`, `build`, `fix`, `refactor`, `add`, `create`, `update`, `remove`, `delete`, `ship`, `migrate`, `wire`, `integrate`, `sync`, `register`, `track`). The freshest signal of what the operator is actually working on — a new commit in the same worktree updates the resolution immediately so the script never gets stuck on a previous task's prompt.
   2. **Branch name**, when it carries a work verb. Falls back here on a fresh worktree that still points at `origin/main`'s release commit (`chore(release): ...`), which has no work verb — without this fallback the auto-sync silently skipped on every first Edit|Write of every task branch. The branch `<type>/<slug>` format is the work signal (e.g. `fix/...`, `refactor/...`).
   3. **Any commit subject**, when both commit-subject-with-verb and branch-name-with-verb fail. Falls through to the `_should_skip_prompt` gate below.
   4. **Branch name** as final fallback (e.g. a brand-new worktree with no commit yet).

   The active hand-off's `prompt` field is **not** used as a resolution source (per adversarial Codex review on #543) — trusting it would let a stale prompt shadow the current task forever. The hand-off is a cache for the issue reference (`issue` field), not for the task description.
2. Skips read-only / non-task prompts (`/`, `#`, `!`, `ls `, `cat `, `grep `, `git status`, and prompts that lack a work verb).
3. Finds or creates the project named after the configured project-name (per-worktree override) or the repository basename.
4. Searches for **every** open issue whose `description` starts with `<!-- scope:<branch>::<prompt-head> -->`. When more than one match exists, the older ones are silently archived via Linear's `issueArchive` mutation and the newest is kept (see "Automatic transitions" below).
5. Creates a new issue with the same scope marker when no match exists, landing it in the team's `Todo` state (falling back to `Backlog`); updates an existing match's description otherwise.
6. Writes the updated handoff at `.dev-kit/hand-off/linear/<worktree-slug>.json` so the next edit reuses the same issue.

The script always returns exit code 0. Transport errors, missing tokens, and GraphQL failures are logged to stderr and never block the edit (per #539: "Linear failures are non-blocking for implicit workflow calls."). Users without Linear configured are unaffected — the hook fast-paths on missing env var and config. Set `LINEAR_DEBUG=1` to surface every skip reason on stderr.

### Owner-gated auto-trigger (worktree, session, task change)

The Edit|Write hook is one of FOUR auto-trigger points. The other three — **worktree create, session start, task change** — fire on events that have no Edit|Write yet (the new worktree may sit idle, the session may open with a question first, the user may switch plans before saving). For each, a dedicated hook runs `auto-sync` from the right cwd so the Linear issue is registered immediately, without waiting for the user to type a work verb or for the next accidental save.

| Trigger | Hook | Event | Sync cwd |
|---|---|---|---|
| Edit\|Write\|MultiEdit | `hooks/linear-autosync.sh` | PreToolUse | session cwd |
| New worktree | `hooks/linear-worktree-create.sh` | PostToolUse:Bash (after `git worktree add`) | the new worktree path |
| Auto-cut worktree | `hooks/worktree-auto-cut.sh` (extension) | UserPromptSubmit (after `git worktree add`) | the new worktree path |
| Session start | `hooks/linear-session-start.sh` | SessionStart (worktree-only) | session cwd |
| Plan / task change | `hooks/linear-task-change.sh` | UserPromptSubmit | session cwd |

The four hooks all delegate to a single Python entry point: `tools/linear_sync.py auto-sync` (for the always-fire triggers) or `task-change-sync` (for the scope-diff trigger). Both entry points are **owner-gated** via `is_repo_owner()`:

```text
is_repo_owner(repo)
  1. LINEAR_REPO_OWNER_AUTO_SYNC=1|true  → True  (explicit opt-in, e.g. for forks)
  2. LINEAR_REPO_OWNER_AUTO_SYNC=0|false → False (explicit opt-out)
  3. detection: gh api user --jq .login  == OWNER(remote.origin.url)
        case-insensitive; 3s timeout; non-GitHub origin → False
  4. anything fails (gh missing, no auth, no remote)  → False
```

The first two are the explicit escape hatches for forks (`gh auth` points at the contributor) and shared machines (the owner doesn't want auto-sync there). Detection compares the GitHub login of the authenticated `gh` user against the OWNER segment of `git remote get-url origin` — `sh-ai-x` for `https://github.com/sh-ai-x/dev-harness-kit.git`. A negative result is cached for the lifetime of the Python process (each Edit|Write re-forks, so the cache is short-lived in practice).

The manual CLI path (`/dev-kit:linear` → `sync()`) is intentionally **ungated** — a contributor who has configured Linear can still register work explicitly. The gate is about implicit hook-driven updates, not about refusing to ever touch the API.

### Automatic transitions

Once a worktree has auto-sync enabled and a matching issue exists, the hook performs four transitions without any explicit user request. None of them block the edit (always exit 0, never raise):

1. **Auto-open** — on the first Edit/Write that contains a work verb, the script creates a new issue in the team's `Todo` state (falling back to `Backlog` when no `Todo` column is configured). The scope-marker `<!-- scope:... -->` is prepended to the description so the next edit reuses the same issue.
2. **Auto-In-progress** — on subsequent Edit/Write with a starting-verb (`implement`, `build`, `wire`, `integrate`, `start`, `sync`, `register`, `track`, `add`, `create`), the matching issue is transitioned to `In Progress`. The transition is idempotent (skipped when the handoff cache already records `In Progress`); the Linear API is the source of truth, so a manual state change on the issue is preserved.
3. **Auto-Done** — when the prompt contains a completion verb (`done`, `finished`, `complete[d]?`, `shipped`, `merged`, `closed`), the matching issue is transitioned to `Done` and the sync round exits without further mutations. Already-terminal issues (Done / Canceled) are left alone — manual moves in the Linear UI always win.
4. **Auto-archive duplicates** — when more than one open issue shares the same `<!-- scope:... -->` marker (typically from a `linear off` + `linear on` cycle), the older issues are archived via the `issueArchive` mutation. Archive is reversible + idempotent, never delete. Only the newest match survives and is used for subsequent updates.

Set `LINEAR_DEBUG=1` to surface every activation decision, state transition, and archive event on stderr. Without the flag, silent no-ops are reported only on transport failures.

## Per-worktree CLI

`/dev-kit:linear` accepts subcommands. Each one delegates to `tools/linear_sync.py`, which is the authoritative implementation. The skill exists so the user does not have to remember the script path; the script exists so the hook, the skill, and any future caller share one code path.

| Subcommand | Effect |
|---|---|
| `/dev-kit:linear` (no args) | Run one auto-sync round (re-evaluates the current task and creates/updates the matching Linear issue). |
| `/dev-kit:linear on` | Enable auto-sync in this worktree. Writes `enabled: true` to `<worktree>/.dev-kit/linear-config.json`. |
| `/dev-kit:linear off` | Disable auto-sync in this worktree. Writes `enabled: false`. Project name and team id are preserved. |
| `/dev-kit:linear setup` | Print the one-time setup checklist + the current state (whether `LINEAR_API_KEY` is set, what the resolved project name is, whether the worktree config exists). |
| `/dev-kit:linear project-name <name>` | Override the auto-detected project name for this worktree. Without an argument, prints the resolved name. |
| `/dev-kit:linear list` | Print recent Linear issues (default 25, newest first). Flags: `--state=<name>`, `--team=<key>`, `--project=<name>`, `--all-projects`, `--assignee=me|none|<id>`, `--limit=<N>`. By default the list is scoped to the active repo project (per-worktree override or repo basename); pass `--all-projects` to see every project the team can see. Non-blocking; never raises. |
| `/dev-kit:linear status` | Print a JSON snapshot of the resolved state (worktree path, slug, config, env-var presence, resolved project + team). |

The skill must invoke the CLI rather than replicate its logic. The standard pattern for each subcommand is:

```bash
python3 tools/linear_sync.py <subcommand> [args...]
```

The CLI writes the config at `<repo>/.dev-kit/linear-config.json` (untracked) and the handoff at `<repo>/.dev-kit/hand-off/linear/<worktree-slug>.json`. The API key is **read from** (but never written to) disk via the env files documented in the Setup section below.

### Setup (one-time, per machine)

Three equivalent ways to provide `LINEAR_API_KEY`. Pick **one**. Priority order is always: shell env > user-scope `.env` > per-worktree `.env.linear` — the first match wins per key.

**Option A — shell env (recommended for shared machines / CI):**

```bash
export LINEAR_API_KEY=<your-linear-api-token>     # https://linear.app/settings/api
cd .worktrees/<your-worktree>
python3 tools/linear_sync.py on
python3 tools/linear_sync.py project-name "<name>"    # optional
```

**Option B — user-scope env file (recommended for solo dev, shared across repos):**

A single file holds the key for every repo on your machine. XDG-aware: `$XDG_CONFIG_HOME/dev-kit/.env` when set, otherwise `~/.config/dev-kit/.env`. The script only injects `LINEAR_*` keys from this file so unrelated app env vars in the same file do not leak into the Linear subprocess.

```bash
mkdir -p ~/.config/dev-kit
echo 'LINEAR_API_KEY=<your-linear-api-token>' >> ~/.config/dev-kit/.env
# Optional, same file:
# LINEAR_TEAM_ID=...
# LINEAR_PROJECT_NAME=...
```

Lines starting with `#` are comments. Values may be quoted (`"..."` or `'...'`); trailing `# comment` is stripped.

**Option C — per-worktree env file (backward compat, Linear-only):**

The script also reads `.dev-kit/.env.linear` (untracked, `.gitignore`'d) as a fallback when neither shell env nor the user-scope file is present. All keys pass through (no `LINEAR_` filter) because the file is Linear-only by convention.

```bash
# .dev-kit/.env.linear (you create this yourself — never committed)
LINEAR_API_KEY=<your-linear-api-token>
# LINEAR_TEAM_ID=...    # optional
# LINEAR_PROJECT_NAME=...   # optional override
```

Shell env always wins; files only fill in missing values.

After picking an option:

```bash
cd .worktrees/<your-worktree>
python3 tools/linear_sync.py on
python3 tools/linear_sync.py project-name "<name>"    # optional
```

Run `python3 tools/linear_sync.py setup` to print all three checklists plus the current state (which sources resolved, whether the user-scope file is present, etc.).

Or, equivalently, through the skill:

```
/dev-kit:linear on
/dev-kit:linear project-name "My Project"
```

### Linear MCP server (chat-side access)

The Linear team publishes an MCP server (https://linear.app/docs/mcp) that exposes Linear objects (issues, projects, comments) to any MCP client. Registering it is optional and orthogonal to `linear_sync.py` — the sync script handles every Edit|Write, the MCP server handles ad-hoc chat queries and updates.

**One-time setup:**

```bash
claude mcp add --transport http linear-server https://mcp.linear.app/mcp
```

Then in any session run `/mcp` and authenticate via OAuth (or paste a Linear API key as the Bearer token). The server exposes tools like `list_issues`, `get_issue`, `create_issue`, `update_issue`, and `list_projects` — the same surface the CLI's `list` subcommand queries, but reachable from chat without a sub-shell.

The MCP server uses the same `LINEAR_*` scope (issues / projects / comments) as `linear_sync.py`; no extra env vars are required beyond the bearer token provided at `/mcp` time.

### Config file shape

```json
{
  "enabled": true,
  "project_name": "My Linear Project",
  "team_id": "",
  "set_at": "2026-08-03T00:54:03Z"
}
```

### Why per-worktree?

Each worktree represents a different task, branch, and Linear scope. Storing the config under `<worktree>/.dev-kit/` means parallel Claude Code sessions in different worktrees each get their own enabled flag, their own project-name override, and their own hand-off state under `.dev-kit/hand-off/linear/<slug>.json` — no cross-talk, no shared mutable state.

### State priority (Linear API > hand-off file)

The hand-off file is a *cache*, not a source of truth. The priority order is:

1. **Linear API** — every sync round issues `_find_issue(projectId, scope)` which lists the project's open issues and matches by the `<!-- scope:<branch>::<prompt-head> -->` prefix. This is the only mechanism that decides "reuse vs. create."
2. **Hand-off file** — `<worktree>/.dev-kit/hand-off/linear/<slug>.json` is consulted only to:
   - carry the previous prompt across sessions when the hook fires on a brand-new task
   - fall back to a human-readable identifier (`SHO-151`) when the API returns a bare uuid
   - record the resolution timestamp and the action taken

   The file ships with a `_meta` block declaring its priority and its source of truth:

   ```json
   {
     "_meta": {
       "priority": 2,
       "kind": "cache",
       "source_of_truth": "linear_api",
       "written_by": "tools/linear_sync.py"
     },
     "issue": "SHO-151 (d81ee2dd-...)",
     "project": "dev-harness-kit",
     ...
   }
   ```

A stale or wrong issue id in the hand-off file can never cause a duplicate or a wrong-target update — the next sync round always re-validates against the API and overwrites the file with the authoritative result.

## Reconciliation workflow

1. Read the current repository, branch/worktree, task request, and any existing handoff as context.
2. List or search the user's Linear teams and projects. Use the canonical repository name as the project name.
3. If that project does not exist, create it in the selected team. Do not silently substitute a similarly named project.
4. Search open and recently updated issues in that project using the current task's concrete scope and keywords.
5. Reuse an issue only when its scope and intended outcome match the current task. A present, old, closed, or unrelated handoff is not sufficient evidence.
6. Create a new issue when no matching issue exists or when an old issue represents a different task. Use the appropriate Feature, Improvement, or Bug label when available.
7. Set a newly started issue to `Todo` or `In Progress` according to the caller's stage. Preserve existing status when merely reconciling.
8. Write a small handoff record only after the Linear result is known. The record is a resume hint, not an authorization gate.

## Workflow callers

When called by `plan`, `build`, `build-debug`, or `refactor`, perform this workflow once at the start of that skill and return a compact result:

```text
LINEAR_OK: project=<name> issue=<identifier> action=<reused|created> status=<state>
LINEAR_SKIP: reason=<disabled|not-connected|not-configured>
LINEAR_ERROR: reason=<actionable failure>; continue=<yes for implicit calls>
```

The caller must continue on `LINEAR_SKIP` and implicit `LINEAR_ERROR`. It must not duplicate Linear calls inside loops, phases, retries, or every user prompt.

## Handoff and PR linking

Use `.dev-kit/hand-off/linear.json` when the repository has a handoff directory. Replace the current task entry when the task changes; do not use file age or presence as a gate. Include the project, issue identifier, task summary, registration action, and timestamp.

When a PR exists, add its URL to the matching issue and update the issue status only when the caller explicitly owns that transition. Never create a second issue solely because a PR already exists.

## Next step

After direct reconciliation, continue with the requested workflow, usually `/dev-kit:plan`, `/dev-kit:build`, `/dev-kit:build-debug`, or `/dev-kit:refactor`.
