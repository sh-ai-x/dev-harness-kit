# Hooks (SSOT)

> **Scope and mode reference** → [`docs/scopes/README.md`](../docs/scopes/README.md)
>
> Hooks are loaded based on three scopes (user / project / local) and three modes (`full` / `lite` / `undev`). If a hook fires in an unexpected project, or your `enabledPlugins` doesn't seem to take effect, the answer is in `docs/scopes/troubleshooting.md`.



> The active-hooks state lives in `.dev-kit/.active-hooks.json` (MUST-13).
> Shells live in `hooks/*.sh` and are wired via `hooks/hooks.json`.
> Two writers share the file via namespaced top-level keys:
>   - `tools/regenerate_active_hooks.py` owns `schema_version`,
>     `generated_at`, and `events` (event-keyed snapshot derived from
>     `hooks/hooks.json`; one entry per `{name, path, when, fail_closed}`).
>     `fail_closed` is read from the explicit `fail_closed: true|false`
>     field on each `hooks/hooks.json` entry (no inference from script
>     headers — the explicit field is the SSOT).
>   - `lib/active_hooks_codec.py` owns `matrix` (stage-keyed
>     activation grid) and `override` (override flags). The codec's
>     `ensure_matrix` writes this slice on a fresh checkout.
> The regen tool is run on every `SessionStart` by
> `hooks/session-start-check.sh`. It preserves any pre-existing
> `matrix` / `override` slice verbatim so neither writer ever clobbers
> the other's slice — the two schemas coexist on the same file.
> Regeneration is cheap, idempotent (sorted keys + sorted entries),
> and best-effort (silent skip when `python3` is missing or
> `hooks/hooks.json` is unreadable). Schema version: `1.0.0`.

## Hook matrix (per stage)

```
| Hook                  | Bootstrap | Plan | Design | Build | Review | Security | Ship |
|-----------------------|:----:|:----:|:----:|:----:|:----:|:----:|:----:|
| tdd-guard             |  -    |  -    |  -    |  ✅    |  -    |  -    |  -    |
| bash-guard (tier 2)   |  -    |  -    |  -    |  ✅    |  -    |  -    |  -    |
| bash-guard (tier 1)   |  C    |  C    |  C    |  C    |  C    |  C    |  C    |
| destructive-confirm   |  K    |  K    |  K    |  K    |  K    |  K    |  K    |
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
(R = read-only) (* = fires only when Linear is configured.) (A = always-on with per-worktree opt-out via `.dev-kit/.sub-agent-handoff-disabled`.) (C = catastrophic tier: always denies, ignores both `DEV_KIT_STRICT` and this matrix.) (K = ask-tier: always on, surfaces a human confirmation; opt out with `DEV_KIT_NO_CONFIRM=1`.)

## Hook shells

### Runtime hook ordering

For the committed runtime manifests, `trace-session-end.sh` MUST be the
first hook for Claude `SessionEnd` and `Stop`, and for Codex `Stop`. It writes
the terminal trace record before `save_log.py` runs with
`SAVE_LOG_ARCHIVE_STALE=1` and can archive the trace file.

| Hook | Stage ON | Purpose |
|------|----------|---------|
| `tdd-guard` | build | active when `lib/methodology/tdd.py` is loaded (MUST-48). Session-scoped bypass: `/dev-kit:guard-mode off tdd` (see `session-start-guard-mode-reset` below). |
| `bash-guard` | build (tier 2) / all stages (tier 1) | Two tiers. **Tier 1 (catastrophic)** — `rm -rf /`, `rm -rf ~`, `chown -R /`, `mkfs.*`, `dd of=/dev/sd*`, `curl\|sh`, `npm publish`, `kubectl delete namespace`, `aws s3 rm --recursive`, `terraform destroy -auto-approve`, and any attempt to set `DEV_KIT_HOOK_OFF=.bash-guard`. Denies unconditionally, checked *before* the stage gate, not overridable by `DEV_KIT_STRICT`. **Tier 2 (recoverable)** — `git reset --hard`, `git clean -f`, force-push, `DROP TABLE`, `docker system prune`, `find -delete`, `pkill -9`. Stage-gated to build; advisory unless `DEV_KIT_STRICT=1`. |
| `destructive-confirm` | all stages (not gated) | PreToolUse `Bash\|Write\|Edit\|MultiEdit` **ask-tier** gate — the only hook that emits `permissionDecision: "ask"` (human confirmation prompt) rather than deny-or-silence. Asks on: writes to `.env` / `*.pem` / `*.key` / `.ssh/*` / `.aws/credentials` / `.netrc` / `.kube/config` / `secrets.*` (`.env.example` and friends are exempt so the prompt stays meaningful); bare `git worktree remove` (bypasses `bin/worktree-remove-safe.sh`, discarding the worktree's `logs/`); `git push --force-with-lease`; and first-time `git push -u`. Opt out with `DEV_KIT_NO_CONFIRM=1`. Both push asks can be bypassed loop-locally via `push_confirm=off` in `.dev-kit/guard-mode.session.json` (set by `/dev-kit:babysit-pr` and `/dev-kit:babysit-pr-local` on entry, restored to `on` in the EXIT trap); the hard git-guard policy remains active. Fails closed when `jq` is missing. |
| `l4-todo-scan` | build / review / security | PostToolUse deferred-work marker scan (Iron Law #4). Fails closed on TODO/FIXME/'we'll extend later' markers in non-allowed paths. Allowed paths: `*.md`, `tests/fixtures/**`, `docs/adoption/**`. `L4_STRICT=1` overrides the allowed-path exemption. |
| `secret-scan` | build / review / security | PostToolUse credential-pattern grep. |
| `slop-detector` | build / review / security | KO+EN banned-phrase scan. |
| `stop-verify` | plan / design / build / review / security / ship | Stop hook: AC claim verification. |
| `worktree-session-cleanup` | all (Stop advisory) | After a completion-shaped response in a clean task worktree, asks the user to keep it or explicitly archive logs and remove it. Never deletes from the hook itself. |
| `worktree-guard` | n/a | PreToolUse Edit/Write block on main checkout (this repo). Enabled only when `DEV_KIT_GUARDS=on`; session override: `/dev-kit:guard-mode off worktree` (see `session-start-guard-mode-reset` below). |
| `git-guard` | n/a | PreToolUse Bash block on `git commit`/`push` to main. Enabled only when `DEV_KIT_GUARDS=on`. |
| `pre-push` (git hook) | n/a | Git-level pre-push hook installed by `bin/install-pre-push.sh` (idempotent, worktree-aware). Runs `tools/issue_sync.py pre-push --strict --from-log` so a stale `Issue #N` / `Closes #N` ref in a commit message fails the push locally (~9s wall-clock + <10MB RSS) before LLM judges fire in GH Actions. Opt-in stages (`validate` / `lint` / `pytest` / `cache-decay-audit`) are wired against `.dev-kit/local-gates.yaml` and stay off by default. See `docs/gates/local-pre-push.md` for the per-gate contract. |
| `linear-autosync` | always (gated) | PreToolUse Edit/Write block (gated) that calls `tools/linear_sync.py auto-sync`. No-op when `LINEAR_API_KEY` and `.dev-kit/.enabled.json:mcp.linear` are both absent. Always exit 0 (non-blocking per #539). The `auto-sync` entry point applies the **repo-owner gate** — non-owners bail silently so contributors never leak their work into the owner's Linear workspace. |
| `linear-session-start` | all (gated, worktree-only) | SessionStart hook. Fires once at every session start inside a Linear-configured worktree and triggers one auto-sync round so a fresh session is reflected in Linear immediately, without waiting for the first Edit/Write. Same owner-gate contract as `linear-autosync`. |
| `linear-worktree-create` | all (gated) | PostToolUse:Bash hook. Catches a `git worktree add` after the Bash tool returns and runs an auto-sync from inside the new worktree, so the handoff is registered before the first Edit/Write or SessionStart in the new path. Falls back to `git worktree list --porcelain` when the bash command cannot be parsed (e.g. multi-line commands). |
| `linear-task-change` | all (gated) | UserPromptSubmit hook. Detects plan / task changes mid-session and triggers one auto-sync round only when the scope (branch + latest commit subject) differs from the last-recorded handoff scope. Delegates to `tools/linear_sync.py task-change-sync` for the diff. |
| `sub-agent-handoff` | all (opt-out per worktree) | PostToolUse Agent advisory verifying the agent response carries the STATUS / EVIDENCE / NEXT-ACTION pieces needed for the standard handoff template (SHO-154). **Always-on** (the handoff contract applies regardless of stage); per-worktree opt-out via `.dev-kit/.sub-agent-handoff-disabled`. Non-blocking on parse errors (per #539). Fail-closed (exit 2 + plain stderr ERROR) when `jq` or `python3` is missing — PostToolUse cannot actually block, so we emit a stderr signal instead of a `permissionDecision: deny` envelope (which is decorative in PostToolUse; see `slop-detector.sh` / `secret-scan.sh` for the precedent). |
| `session-start-harness-mode-reset` | all (SessionStart) | Resets `.dev-kit/harness-mode.session.json` to `{"mode": "full"}` at the start of every session. New window = strict by default; `fast`/`custom` must be chosen explicitly via `/dev-kit:harness-mode` every session (workflow-fast-mode-lean design). Best-effort — silent no-op when `python3` is missing. |
| `session-start-guard-mode-reset` | all (SessionStart) | Applies `DEV_KIT_GUARDS` (shell → local → project → default off) to `.dev-kit/guard-mode.session.json` and records policy source + checkout class. It never prompts and never enables main specially. Best-effort — silent no-op when `python3` is missing. |
| `install-pre-push` | all (SessionStart) | Runs `bin/install-pre-push.sh` to (re-)symlink `hooks/pre-push.sh` as the git pre-push hook on every session start. Idempotent (no-op if already installed) and worktree-aware (uses `--git-common-dir` to install into the shared hooks directory). The pre-push hook itself runs `tools/issue_sync.py pre-push --strict --from-log` so a stale-ref push is caught locally before LLM judges fire in GH Actions. See `docs/gates/local-pre-push.md` for the per-gate contract. Best-effort — silent no-op when `bin/install-pre-push.sh` is missing. |
| `plugin-cache-refresh` | all (SessionStart) | If `dev-kit` marketplace HEAD short-SHA differs from the marker at `<cache-dir>/.devkit-refresh-head`, rsync marketplace → cache and update the marker. Closes the same-version-update gap left by `/reload-plugins` (cache is keyed by `plugin.json:version`, so a new commit at the same version is invisible until `claude plugin install dev-kit --force` is run manually). Fails open — never blocks session start. |

## Complete hook registry

> **SSOT for the on-disk hook inventory.** The matrix above stages hooks
> by lifecycle; this table lists every hook script on disk with its
> event / matcher / one-line purpose. Updated by code review, not by
> regenerator. Helper files under `hooks/lib/` are listed separately
> below the table.

| Script | Event | Matcher | Purpose |
|---|---|---|---|
| `acp-tier-assert.sh` | PreToolUse | `*` | ACP tier assertion (catch-all). PreToolUse safety net that asserts the active ACP tier before any tool call. |
| `bash-guard.sh` | PreToolUse | `Bash` | Tier-1 catastrophic + Tier-2 recoverable deny gate. See row above. |
| `context-window-guard.sh` | UserPromptSubmit | `*` | Warns when input-token count crosses 100K / 200K / 300K thresholds, nudging the operator to `/compact` or sub-agent delegation. |
| `destructive-confirm.sh` | PreToolUse | `Write\|Edit\|MultiEdit\|Bash` | Ask-tier confirmation for `.env` / `*.pem` / `*.key` / `.ssh/*` / force-with-lease / first push / bare worktree remove. |
| `git-guard.sh` | PreToolUse | `Bash` | Hard block on `git commit` / `git push` to `main`, force-pushes, and `git checkout main && commit` patterns. Paired with `review-yml-isolation.sh`. |
| `injection-content-guard.sh` | PostToolUse | `Agent` + `WebFetch` | Scans sub-agent output and fetched web content for prompt-injection / credential-leak patterns. |
| `l4-todo-scan.sh` | PostToolUse | `Write\|Edit\|MultiEdit` | Iron Law #4 deferred-work marker scan (TODO/FIXME/"we'll extend later"). |
| `linear-autosync.sh` | PreToolUse | `Write\|Edit\|MultiEdit` | Linear auto-sync on edit (owner-gated). No-op without `.dev-kit/linear-config.json:enabled` or `$LINEAR_API_KEY`. |
| `linear-session-start.sh` | SessionStart (fanout) | `*` | One auto-sync round at session start inside a Linear-configured worktree. |
| `linear-task-change.sh` | UserPromptSubmit | `*` | Detects plan / task scope change mid-session; triggers one sync round only on scope diff. |
| `linear-worktree-create.sh` | PostToolUse | `Bash` | Catches `git worktree add` and syncs from inside the new worktree before the first Edit/Write. |
| `log-on-session-start.sh` | SessionStart (fanout) | `*` | Auto-installs loghooks into the active `.claude/settings.json` on a fresh checkout. Best-effort. |
| `plugin-cache-refresh.sh` | SessionStart (fanout) | `*` | rsyncs marketplace → versioned cache on HEAD drift. Closes the same-version-update gap left by `/reload-plugins`. |
| `pr-create-route.sh` | PreToolUse | `Bash` | Routes `gh pr create` through `actor_classifier` so fork PRs use the review-environment path. |
| `provider-divergence-check.sh` | SessionStart (fanout) | `*` | SessionStart nudge when `.env:CI_REVIEW_PROVIDER` drifts from the canonical provider. |
| `ralph-attended-lock.sh` | PreToolUse | `AskUserQuestion` | During `/dev-kit:ralph`, locks `AskUserQuestion` so autonomous-loop can't escape into user prompts. |
| `review-yml-isolation.sh` | PreToolUse | `Bash` | Blocks `git commit` if the staged set contains `review.yml` + any other path. review.yml PRs must be review.yml-only. |
| `secret-scan.sh` | PostToolUse | `Write\|Edit\|MultiEdit` | Credential-pattern grep on edited content (mirror of `lib/secret_scan.py`). |
| `session-start.sh` | SessionStart | `*` | Single SessionStart entry. Fans out to `session-start-check.sh` and the 7 child hooks (see fanout table below). Emits merged `additionalContext`. |
| `session-start-check.sh` | SessionStart (fanout) | `*` | Regenerates `.dev-kit/.active-hooks.json`; emits `trace.started`; runs first-pass-quality smoke probe; records enrollment. |
| `session-start-guard-mode-reset.sh` | SessionStart (fanout) | `*` | Applies `DEV_KIT_GUARDS` policy (shell → local → project → default) to `.dev-kit/guard-mode.session.json`. |
| `session-start-harness-mode-reset.sh` | SessionStart (fanout) | `*` | Resets `.dev-kit/harness-mode.session.json` to `{"mode": "full"}` every session (strict-by-default). |
| `slop-detector.sh` | PostToolUse | `Write\|Edit\|MultiEdit` | KO+EN banned-phrase scan (model-output slop detector). |
| `stop-verify.sh` | Stop | `*` | AC claim verification before session stop. |
| `sub-agent-handoff.sh` | PostToolUse | `Agent` | Advisory verifying the sub-agent response carries the STATUS / EVIDENCE / NEXT-ACTION handoff template (SHO-154). |
| `tdd-guard.sh` | PreToolUse | `Write\|Edit\|MultiEdit` | RED-evidence block on prod-code edits (no test was added/updated). |
| `trace-session-end.sh` | Stop + SessionEnd | `*` | Terminal trace record. MUST be the first hook on Stop and SessionEnd so `save_log.py` archives the trace correctly. |
| `worktree-auto-cut.sh` | UserPromptSubmit | `*` | Suggests auto-cutting a fresh worktree from main when the operator's prompt indicates a new task. |
| `worktree-guard.sh` | PreToolUse | `Write\|Edit\|MultiEdit` | Hard block on Edit/Write in the main checkout. Forces the worktree protocol. |
| `worktree-janitor-session-start.sh` | SessionStart (fanout) | `*` | Orphan-worktree nudge at session start; optional auto-prune when configured. |
| `worktree-log-auto-install.sh` | PostToolUse | `Bash` | Auto-installs loghooks in a fresh worktree when `git worktree add` is detected. |
| `worktree-session-cleanup.sh` | Stop | `*` | After a completion-shaped response in a clean task worktree, asks the user to keep it or archive logs + remove. Never deletes from the hook itself. |

### SessionStart fanout children

`session-start.sh` is the single `SessionStart` entry registered in
`hooks/hooks.json`. It invokes the 7 hooks below in deterministic order;
none of these have their own `SessionStart` matcher in `hooks.json`.

| Child | Purpose |
|---|---|
| `session-start-check.sh` | Active-hooks regeneration + trace.started + smoke probe |
| `session-start-harness-mode-reset.sh` | `harness-mode.session.json` → `{"mode": "full"}` |
| `session-start-guard-mode-reset.sh` | `guard-mode.session.json` ← scoped `DEV_KIT_GUARDS` |
| `plugin-cache-refresh.sh` | marketplace → cache rsync on HEAD drift |
| `log-on-session-start.sh` | loghooks auto-install |
| `provider-divergence-check.sh` | `.env:CI_REVIEW_PROVIDER` drift nudge |
| `worktree-janitor-session-start.sh` | orphan worktree nudge |

### Helpers (`hooks/lib/`)

Sourced by hook scripts via `. "${BASH_SOURCE[0]%/*}/lib/<helper>.sh"`.
No hook scripts themselves live in this directory.

| Helper | Purpose |
|---|---|
| `guard-policy.sh` | Resolves `DEV_KIT_GUARDS` policy + branch class |
| `hook-preamble.sh` | Shared boilerplate: `set -uo pipefail`, `INPUT=$(cat)`, jq-missing warn |
| `locale-utf8.sh` | Forces UTF-8 locale for `grep -E` |
| `log_state.sh` | Reads/writes loghook install state |
| `mode-resolve.sh` | Resolves `DEV_KIT_MODE` |
| `payload-parse.sh` | Stdin / JSON / content extraction; defines `deny`, `ask`, `emit_guard_event` |
| `secret-patterns.sh` | ERE credential patterns (mirror of Python SSOT) |
| `slot-check.sh` | `plugin.json` version-slot freshness predicate |
| `stage-gate.sh` | `hook_stage_active` — consults `.dev-kit/.active-hooks.json` |
| `team-resolve.sh` | Resolves `DEV_KIT_TEAM` |
| `worktree-detect.sh` | `worktree_detect` — main-vs-worktree discriminator (SSOT) |
| `find-python.sh` | Resolves the first available Python 3 interpreter from `{python3, python, py}`. Shared by `hooks/sub-agent-handoff.sh` and `hooks/worktree-auto-cut.sh`. |
| `hook-cwd.sh` | Resolves the hook invocation's effective cwd (handles path-aware lookups so sub-agents inherit the right working dir). |
| `linear-fast-path.sh` | Bakes the Python 3 lookup into a one-shot for Linear hooks (avoids the per-invocation `find-python.sh` cost). |
| `worktree-classify.sh` | Classifies the current worktree against the session policy (returns branch class + scope). |
