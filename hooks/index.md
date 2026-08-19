# Hooks (SSOT)

> Matrix state lives in `.dev-kit/.active-hooks.json` (MUST-13).
> Shells live in `hooks/*.sh` and are wired via `hooks/hooks.json`.

## Hook matrix (per stage)

```
| Hook                  | Bootstrap | Plan | Design | Build | Review | Security | Ship |
|-----------------------|:----:|:----:|:----:|:----:|:----:|:----:|:----:|
| tdd-guard             |  -    |  -    |  -    |  ✅    |  -    |  -    |  -    |
| bash-guard            |  -    |  -    |  -    |  ✅    |  -    |  -    |  -    |
| secret-scan           |  R    |  -    |  -    |  ✅    |  ✅    |  ✅    |  -    |
| slop-detector         |  -    |  -    |  -    |  ✅    |  ✅    |  ✅    |  -    |
| stop-verify           |  -    |  ✅    |  ✅    |  ✅    |  ✅    |  ✅    |  ✅    |
| linear-autosync       |  -    |  ✅*   |  -    |  ✅*   |  -    |  -    |  -    |
| linear-session-start  |  ✅*   |  ✅*   |  ✅*   |  ✅*   |  ✅*   |  ✅*   |  ✅*   |
| linear-worktree-create|  -    |  ✅*   |  -    |  ✅*   |  -    |  -    |  -    |
| linear-task-change    |  -    |  ✅*   |  -    |  ✅*   |  -    |  -    |  -    |
| l4-todo-scan           |  -    |  -    |  -    |  ✅    |  ✅    |  ✅    |  -    |
| sub-agent-handoff     |  A    |  A    |  A    |  A    |  A    |  A    |  A    |
```
(R = read-only) (* = fires only when Linear is configured.) (A = always-on with per-worktree opt-out via `.dev-kit/.sub-agent-handoff-disabled`.)

## Hook shells

| Hook | Stage ON | Purpose |
|------|----------|---------|
| `tdd-guard` | build | active when `lib/methodology/tdd.py` is loaded (MUST-48). |
| `bash-guard` | build | blocks dangerous shell patterns (`rm -rf`, force-push, etc.). |
| `l4-todo-scan` | build / review / security | PostToolUse deferred-work marker scan (Iron Law #4). Fails closed on TODO/FIXME/'we'll extend later' markers in non-allowed paths. Allowed paths: `*.md`, `tests/fixtures/**`, `docs/adoption/**`. `L4_STRICT=1` overrides the allowed-path exemption. |
| `secret-scan` | build / review / security | PostToolUse credential-pattern grep. |
| `slop-detector` | build / review / security | KO+EN banned-phrase scan. |
| `stop-verify` | plan / design / build / review / security / ship | Stop hook: AC claim verification. |
| `worktree-guard` | n/a | PreToolUse Edit/Write block on main checkout (this repo). |
| `git-guard` | n/a | PreToolUse Bash block on `git commit`/`push` to main. |
| `linear-autosync` | always (gated) | PreToolUse Edit/Write block (gated) that calls `tools/linear_sync.py auto-sync`. No-op when `LINEAR_API_KEY` and `.dev-kit/.enabled.json:mcp.linear` are both absent. Always exit 0 (non-blocking per #539). The `auto-sync` entry point applies the **repo-owner gate** — non-owners bail silently so contributors never leak their work into the owner's Linear workspace. |
| `linear-session-start` | all (gated, worktree-only) | SessionStart hook. Fires once at every session start inside a Linear-configured worktree and triggers one auto-sync round so a fresh session is reflected in Linear immediately, without waiting for the first Edit/Write. Same owner-gate contract as `linear-autosync`. |
| `linear-worktree-create` | all (gated) | PostToolUse:Bash hook. Catches a `git worktree add` after the Bash tool returns and runs an auto-sync from inside the new worktree, so the handoff is registered before the first Edit/Write or SessionStart in the new path. Falls back to `git worktree list --porcelain` when the bash command cannot be parsed (e.g. multi-line commands). |
| `linear-task-change` | all (gated) | UserPromptSubmit hook. Detects plan / task changes mid-session and triggers one auto-sync round only when the scope (branch + latest commit subject) differs from the last-recorded handoff scope. Delegates to `tools/linear_sync.py task-change-sync` for the diff. |
| `sub-agent-handoff` | all (opt-out per worktree) | PostToolUse Agent advisory verifying the agent response carries the STATUS / EVIDENCE / NEXT-ACTION pieces needed for the standard handoff template (SHO-154). **Always-on** (the handoff contract applies regardless of stage); per-worktree opt-out via `.dev-kit/.sub-agent-handoff-disabled`. Non-blocking on parse errors (per #539). Fail-closed (exit 2 + plain stderr ERROR) when `jq` or `python3` is missing — PostToolUse cannot actually block, so we emit a stderr signal instead of a `permissionDecision: deny` envelope (which is decorative in PostToolUse; see `slop-detector.sh` / `secret-scan.sh` for the precedent). |
