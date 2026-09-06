# Hook reference — the enforcement layer

**Language:** English

This plugin's load-bearing surface is **deterministic enforcement**, not
prompt prose. Per `CLAUDE.md` Iron Law L7 ("a skill's alpha lives in the
parts the model can't self-impose"), the hooks below short-circuit the
model's tool calls before they run — they block or redact directly, so they
hold even when the model would rather skip them, and they can't be
"absorbed" by a smarter future model the way a purely reasoning-based skill
can.

The skills (`/dev-kit:*`) are convenience wrappers around these hooks plus
the build state machine (`phases/<name>/index.json`). If you only remember
one thing from this page: **the hooks are what actually enforces the rules;
the skills just make them pleasant to drive.**

For the companion audit of *where hook coverage is still thin* (known gaps,
per-runtime wiring differences), see
[`hook-coverage-gaps.md`](hook-coverage-gaps.md).

---

## Enforcement hooks, by what they guard

| Hook | What it does | Stage |
|---|---|---|
| `tdd-guard` | Blocks `lib/` edits without a failing test | Build |
| `bash-guard` | Two-tier destructive-command block. Tier 1 (catastrophic: `rm -rf /`, `curl\|sh`, `mkfs.*`, `npm publish`, `kubectl delete namespace`, `terraform destroy -auto-approve`, guard self-disable) denies unconditionally in every stage. Tier 2 (recoverable: `git reset --hard`, force-push, `DROP TABLE`, `docker system prune`) is advisory unless `DEV_KIT_STRICT=1` | Tier 1 all / Tier 2 Build |
| `destructive-confirm` | Ask-tier human confirmation (`permissionDecision: "ask"`) on credential-file writes, bare `git worktree remove`, `--force-with-lease`, and first `git push -u`. Opt out via `DEV_KIT_NO_CONFIRM=1` | All |
| `secret-scan` | Redacts credential patterns in tool inputs | All |
| `slop-detector` | Catches AI-typical patterns across phrase + structure banks (KO+EN) | Build + Review + Security |
| `l4-todo-scan` | PostToolUse deferred-work marker scan (Iron Law L4): fails closed on TODO/FIXME/'we'll extend later'/starting-point/placeholder markers in `Write`/`Edit`/`MultiEdit` payloads outside allowed paths (`*.md`, `tests/fixtures/**`, `docs/adoption/**`). Strict mode via `L4_STRICT=1`. Marker bank SSOT: `hooks/references/l4/markers.md` | Build + Review + Security |
| `loop-detect` | Warns after three consecutive identical Bash calls using per-session fingerprints | All |
| `worktree-guard` | Hard-blocks Edit/Write in the main checkout; on deny, prints the live worktree list via `git worktree list --porcelain` | All |
| `git-guard` | Enforces branch strategy: blocks commit/push to main, force-push, `gh pr merge`; verifies `plugin.json` slot on `git push` to a feature branch (slot check extracted to `hooks/lib/slot-check.sh` for unit-testable truth table — see *Shared helpers* below) | All |
| `worktree-auto-cut` | Creates the per-task worktree + branch | All |
| `stop-verify` | Quoted exit codes / test counts + 5-item intent checklist (`lib/pre_completion_checklist.py`) before session end | Plan + Design + Build + Review + Security + Ship |
| `review-yml-isolation` | Forces `review.yml` PRs to be `review.yml`-only | All |
| `notification-collapse` | Stderr WARN when ≥ 2 `<task-notification>` envelopes are in a UserPromptSubmit payload (the `Monitor` / `run_in_background` bloat pattern from the 2026-08-11 `/dev-kit:token-analyzer` diagnostic) | All |
| `context-window-guard` | Stderr tiered WARN (100K / 200K / 300K cumulative input tokens) recommending `/compact` per `rules/session-hygiene.md` Iron Law 4; thresholds tunable via `CONTEXT_WINDOW_*_KB` env vars | All |

## Hook inventory, by event

The same hooks, indexed by the Claude Code / Codex event that fires them —
useful when you're debugging *why* a hook did or didn't run:

| Hook | Event | Purpose | Mode |
|---|---|---|---|
| `tdd-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | TDD test-first enforcement | advisory / `--strict` |
| `bash-guard.sh` | PreToolUse (Bash) | Block destructive commands | tier 1 hard-block (always) / tier 2 advisory / `--strict` |
| `destructive-confirm.sh` | PreToolUse (Bash\|Write\|Edit\|MultiEdit) | Human confirmation before credential writes, worktree removal, force-with-lease, first branch push | ask (`permissionDecision: "ask"`) |
| `git-guard.sh` | PreToolUse (Bash) | Branch strategy enforcement | hard-block |
| `pre-push` (.githooks/) | pre-push (local) | Block direct push to `main` + auto-SYNC (not auto-bump) `plugin.json:version` from origin/main on `local < origin/main`; refuses on uncommitted `plugin.json` edits; opt-in LLM-judge intent check via `DEV_KIT_PUSH_INTENT=1`. Calls `bin/sync-version.sh` for the actual version-only sync. | hard-block + auto-commit |
| `worktree-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | Block edits in main checkout | hard-block |
| [`linear-autosync.sh`](linear-autosync.md) | PreToolUse (Write\|Edit\|MultiEdit) | Auto-sync every Edit into the user's Linear workspace via `tools/linear_sync.py` (silent-bail on non-dev-kit project dirs) | advisory (silent exit 0) |
| `review-yml-isolation.sh` | PreToolUse (Bash) | Force `review.yml` changes into their own commit/PR | hard-block |
| `worktree-auto-cut.sh` | UserPromptSubmit | Auto-cut a worktree for a new-task prompt in main | advisory (fails open) |
| `session-start-check.sh` | SessionStart | Remind about the worktree rule | advisory |
| `log-on-session-start.sh` | SessionStart | Auto-install loghooks each session (idempotent) | advisory |
| `provider-divergence-check.sh` | SessionStart | Nudge when `.env:CI_REVIEW_PROVIDER` is off-list, diverges, or missing | advisory |
| `worktree-janitor-session-start.sh` | SessionStart | Nudge when merged-into-main or stale `fix/classify-request-*` worktrees are present; opt-out via `DEV_KIT_JANITOR_OFF=1` (issue #717). Optional auto-apply when `DEV_KIT_JANITOR_AUTO_PRUNE=1` *and* `DEV_KIT_JANITOR_AUTO_PRUNE_YES=1` are exported (capped at `DEV_KIT_JANITOR_AUTO_PRUNE_MAX`, default 50/session) — restricted to stale-classify predicate only, skips current + main worktree, requires clean `git status`, drops `--force`. Audit log at `.dev-kit/janitor-audit.log`. | advisory |
| `secret-scan.sh` | PostToolUse (Write\|Edit) | Detect credentials in edits | hard-block |
| `slop-detector.sh` | PostToolUse (Write\|Edit) | Block AI slop (phrase + structure + scoring, KO+EN) | advisory (opt-in strict) |
| `l4-todo-scan.sh` | PostToolUse (Write\|Edit) | Fail-closed scan for TODO/FIXME deferred-work markers in `Write`/`Edit`/`MultiEdit` payloads; strict-mode via `L4_STRICT=1` (MUST-4) | hard-block (advisory under allowed-path exemption) |
| `worktree-log-auto-install.sh` | PostToolUse (Bash) | Install loghooks into a newly-added worktree | advisory |
| `loop-detect.sh` | PostToolUse (Bash) | Warn before another retry after repeated identical Bash calls | advisory (fails open) |
| `notification-collapse.sh` | UserPromptSubmit | Stderr WARN when 2+ `<task-notification>` envelopes are in the prompt (harness `Monitor` / `run_in_background` bloat signal) | advisory (fails open) |
| `context-window-guard.sh` | UserPromptSubmit | Stderr tiered WARN at 100K / 200K / 300K cumulative input tokens recommending `/compact` | advisory (fails open) |
| `acp-tier-assert.sh` | PreToolUse (`*`) | Enforce ACP agent tier-assertion line on first tool call (M/T/L) | hard-block |
| `stop-verify.sh` | Stop | Run regression tests + pre-completion intent checklist on session end | hard-block |
| `sub-agent-handoff.sh` | PostToolUse (Agent) | Verify sub-agent response carries STATUS / EVIDENCE / NEXT-ACTION pieces; advisory; fail-closed on jq missing | advisory (fail-closed on missing jq) |

**Reading the "Mode" column:** `hard-block` means the tool call is denied
outright — there is no override short of removing the hook. `advisory`
means the hook warns (and, for `tdd-guard`/`bash-guard`, can be escalated to
`--strict` to hard-block too). `fails open` means an internal error in the
hook itself doesn't block your work — it just skips the check for that
call.

### Shared helpers (`hooks/lib/`)

These are not hooks themselves — they are `source`-d by the hooks above
to keep their logic unit-testable in isolation (rather than inlined
inside a PreToolUse shell script). Each helper carries its own
`tests/test_<helper>.py` regression coverage.

| Helper | Sourced by | Purpose |
|---|---|---|
| `payload-parse.sh` | most PreToolUse hooks | `read_stdin_json`, `require_jq` |
| `secret-patterns.sh` | `secret-scan.sh` | Bash ERE credential bank (SSOT with `lib/analysis_core/runner.py::_SECRET_PATTERNS`) |
| `worktree-detect.sh` | `worktree-guard.sh`, `git-guard.sh` | `worktree_detect` (single source of truth for the `--git-dir == --git-common-dir` discriminator) |
| `hook-preamble.sh` | 6 hooks (see `tests/test_hook_preamble.py`) | Common preamble: `set -euo pipefail`, `LC_ALL=C.UTF-8`, `$0`-relative path setup |
| `locale-utf8.sh` | preamble-using hooks | One-shot `LC_ALL=C.UTF-8` / `LANG=C.UTF-8` setup |
| `slot-check.sh` | `git-guard.sh` | `slot_should_deny <claude> <codex> <expected>` truth table for the `plugin.json` version-slot check (added 2026-08-03, inspect finding #2) |
| `stage-gate.sh` | `stop-verify.sh` | `hook_stage_active` + `pre_completion_checklist_active` stage-activation helpers (the second follows stop-verify's stage + override rules so the intent checklist fires under the same gate) |
| `loop-detect.sh` | `hooks/loop-detect.sh` | Append per-session Bash fingerprints and detect consecutive matches at the configured threshold |

---


## Active-hooks state file (`.dev-kit/.active-hooks.json`)

The single file `.dev-kit/.active-hooks.json` carries two writer-owned
slices, namespace-separated by top-level key. Issue #676 originally
shipped the regen tool writing a single payload that clobbered the
codec's `matrix` slice on every SessionStart, which silently turned
off the stage-gated hooks (`tdd-guard`, `bash-guard`, `secret-scan`,
`slop-detector`, `stop-verify`, `pre_completion_checklist`). The
current schema is two-slice:

| Top-level key | Owner | Shape | Purpose |
|---|---|---|---|
| `schema_version` | regen (`tools/regenerate_active_hooks.py`) | string `"1.0.0"` | bumps only on breaking schema changes |
| `generated_at` | regen | ISO-8601 UTC with `+00:00` | when the event-wiring slice was last regenerated |
| `events` | regen | `{<event>: [{name, path, when, fail_closed}]}` | event-keyed snapshot of every entry in `hooks/hooks.json`; one entry per (matcher, command) tuple |
| `matrix` | codec (`lib/active_hooks_codec.py`) | `{<stage>: {<hook>: bool|"read-only"}}` | stage-keyed activation grid consumed by `is_hook_active()` |
| `override` | codec | `{disabled_hooks, strict_mode, env_override}` | runtime override flags consumed by `is_hook_active()` |

**Why two writers, one file?** The regen snapshot (event-keyed) and
the codec matrix (stage-keyed) answer different questions:

- Regen answers *"which hook shells does `hooks/hooks.json` wire into
  which Claude Code / Codex event?"* — derived purely from the wiring
  manifest, deterministic across re-runs, used by tooling that walks
  the wiring (e.g. coverage-gap reports).
- Codec answers *"for the current dev-kit stage, which of those
  wired-in hooks should actually run?"* — operator-tunable, mutates
  across the session as `set_stage` / `disable_override` are called.

**Writer order matters:**

- The regen tool ALWAYS preserves the codec's `matrix` and `override`
  slice verbatim when re-running. It reads the existing file via
  `lib.atomic.read_json_or_default` and copies those two keys into
  the new payload before overwriting.
- The codec's `set_stage` / `disable_override` / `ensure_matrix`
  read the file with `read_json_or_default` (defaults to a fresh
  `matrix` payload) and write back; they do NOT touch `events`,
  `schema_version`, or `generated_at`.

**Regression coverage:** `tests/test_active_hooks_codec.py::TestCrossCodecCoexistence`
pins the contract — ensure_matrix → regen → regen keeps the matrix
slice byte-equal; regen → ensure_matrix → set_stage keeps the events
slice byte-equal; the stage-gated hooks (`tdd-guard`, `bash-guard`,
`secret-scan`, `slop-detector`, `stop-verify`) stay active after
every transition.

## See also

- [Hook coverage gaps](hook-coverage-gaps.md) — known gaps in this matrix and per-runtime wiring differences (Claude Code vs. Codex).
- [`linear-autosync.sh`](linear-autosync.md) — per-edit Linear auto-sync hook (PROJECT_DIR guard, env fast-path, non-blocking contract).
- [`rules/git-workflow.md`](../../rules/git-workflow.md) — the worktree + branch rules `worktree-guard` and `git-guard` enforce.
- [`docs/architecture/RUNTIME-PORTABILITY.md`](../architecture/RUNTIME-PORTABILITY.md) — how the same hooks run under both Claude Code and Codex.
- Main [`README.md`](../../README.md) — the short version, under "Under the hood".

## Timeout policy

UserPromptSubmit hooks (specifically `tdd-scope-judge.sh` and
`worktree-auto-cut.sh`) carry an explicit `timeout: 60` in
`hooks.json`. The 30s default is insufficient for these because:

- `worktree-auto-cut.sh` runs `git fetch origin main` + `git worktree add`,
  both of which can exceed 30s on slow origin or large HEAD.
- `tdd-scope-judge.sh` runs an LLM judge (`lib.tdd_scope_judge`) as
  fallback for path-rule misses. The judge honors
  `DEV_KIT_BUILD_AGENT` (default `claude`; `codex` routes through
  `codex exec`) and `DEV_KIT_SKIP_TDD=1` (escape hatch that bypasses
  the judge entirely — issue #647). It resolves its state-file root via
  `${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel)}` — the same
  fallback `tdd-guard.sh` uses to read `.tdd-scope.json` — so the two
  hooks agree on where state lives even when `DEV_KIT_TDD_ROOT` points
  outside the git toplevel.

Both hooks are advisory (exit 0 on failure per the script-level
contract), so a timeout silently discards the nudge rather than
breaking correctness — but the user loses the suggestion.
60s is well above the typical case (<10s) and well below the
600s default hook ceiling. Other hook groups (PreToolUse,
SessionStart, PostToolUse, Stop) inherit the 30s default; none
currently run heavy paths so defaults are fine.
