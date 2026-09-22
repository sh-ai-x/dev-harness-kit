# ACP Dispatch — M-tier architecture



> Companion to `docs/architecture/acp-harness.md`. Closes #282. Documents the
> M (orchestrator) → T (task sub-agent) → L (leaf sub-agent) hierarchy,
> the four communication channels that connect them, and the parallel
> worktree-cut pattern that fans N PRs out from a single round.

## 1. The three tiers (SSOT)

| Tier | Code | Branch shape | Owns | Forbidden from | First tool call |
|---|---|---|---|---|---|
| **M** | Orchestrator | `orch/<round-slug>` | Round state, dispatch decisions, `handoffs.md` writes | Editing source code, hooks, tests, manifests; pushing/committing to `main`; auto-dispatching after a T finishes | `[tier-assert] I am Tier 1 (M). …` |
| **T** | Task sub-agent | `fix/<slug>` / `feat/<slug>` / `refactor/<slug>` / `chore/<slug>` / `test/<slug>` / `docs/<slug>` / `perf/<slug>` / `hotfix/<slug>` (one branch per T) | One PR's lifecycle (branch → commits → push → PR → review → merge → cleanup) | Editing files outside the PR's scope; pushing to `main`; force-push after review has started | `[tier-assert] I am Tier 2 (T). …` |
| **L** | Leaf sub-agent | inherits T's worktree | One read-only investigation: search files, read code, summarize — no edits | Any `Edit`/`Write`/`MultiEdit`; any `git commit`/`git push`; any `gh` mutation | `[tier-assert] I am Tier 3 (L). …` |

The tier-assertion lint (`hooks/acp-tier-assert.sh`, wired in
`hooks/hooks.json` PreToolUse `*`) refuses the first tool call of any
session that has not emitted its mandatory `[tier-assert]` line. This
single guard closes §1 #4 of `docs/architecture/acp-harness.md` (out-of-scope
operations from agents that forgot their role).

## 2. The four communication channels

The three tiers share state through four channels. Every channel has a
canonical location and a writer/reader contract; mixing them is the
root cause of every symptom in `docs/architecture/acp-harness.md` §1.

### 2.1 Envelope (M → T, M → L)

**Writer**: M.
**Reader**: T / L (the single dispatched agent).
**Location**: `<orch_worktree>/.dev-kit/round-<descriptor>/dispatches/<branch>.md`.

The envelope is the dispatch prompt itself — a copy of the canonical
template at `skills/_acp/sub-agent-prompt.md` with all seven
placeholders resolved:

| Placeholder | Resolved from |
|---|---|
| `<TASK>` | the M's decomposition step |
| `<BRANCH>` | the T's target branch |
| `<WORKTREE_PATH>` | absolute path of the worktree dir |
| `<CWD>` | always = `<WORKTREE_PATH>` |
| `<PLUGIN_VERSION_TARGET>` | `bin/version-slot compute <PR_INDEX>` |
| `<LOCK_FILE>` | `<orch_worktree>/.dev-kit/round-<descriptor>/locks/<branch>.lock` |
| `<PARENT_SESSION_CWD>` | absolute path of the M's cwd when it dispatched |

The lint (`tests/test_acp_hand_off.py`) refuses any dispatch with a
missing placeholder. The Python helper `lib/acp_dispatch.py`
(`ACPDispatcher.fill_placeholders`) raises `ValueError` on missing
values at dispatch time, before the worktree is cut, so a misconfigured
M fails fast.

### 2.2 Round state (M writes, T + L read)

**Writer**: M.
**Reader**: T + L (read-only).
**Location**: `<orch_worktree>/.dev-kit/round-<descriptor>/`.

Three sub-paths:

- `meta.json` — the round manifest (PR list, tier codes, dispatch
  timestamps, dependency edges). M is the sole writer; T and L
  reference it via the embedded copy in the envelope.
- `handoffs.md` — append-only hand-off log. M writes one entry per
  dispatch (`## <utc> — dispatch T(<branch>): <one-line task>`) and
  one per completion (`## <utc> — T(<branch>) done: <exit summary>`).
  T and L append their hand-off note to the envelope's `<TASK>` block
  — they NEVER edit `handoffs.md` directly.
- `locks/<branch>.lock` — per-branch flock lock held by M for the
  lifetime of a T's dispatch. The T's lock-file path is embedded in
  the envelope; the T must `touch` it on first tool call so `ls`
  surfaces the active branch set, and the M releases it after `git
  push` succeeds.

### 2.3 Tier sentinel (T writes, M reads)

**Writer**: T (via `hooks/acp-tier-assert.sh`).
**Reader**: M (audit), future `Eval` harness (regression scoring).
**Location**: `<orch_worktree>/.dev-kit/round-<descriptor>/tier-state/<session-id>.json`.

Once the tier-assertion lint passes for a session, the hook writes:

```json
{
  "asserted": true,
  "n": "2",
  "letter": "T",
  "cwd": "/Users/.../acp-dispatch",
  "ownership": "ONE PR's lifecycle on branch feat/acp-dispatch",
  "first_tool": "Bash",
  "asserted_at": "2026-07-18T22:34:12Z"
}
```

The hook then no-ops on every subsequent tool call in the same session
(the sidecar is the "tier-asserted" cache). The M can read the sidecar
set to confirm each T has actually asserted before `git push` is
permitted; a future `Eval` regression can score the round on
assertion-presence rate.

### 2.4 Hand-off notes (T → M, append-only)

**Writer**: T (appends one line at completion).
**Reader**: M (next dispatch reads the previous N entries to seed
`<TASK>` context for dependent PRs).
**Location**: appended to the T's dispatch envelope file at
`<orch_worktree>/.dev-kit/round-<descriptor>/dispatches/<branch>.md`
under a `## Hand-off` heading. M copies the last N entries into the
next dispatch prompt so context propagates without a second writer
on `handoffs.md`.

## 3. The M-tier dispatcher (`lib/acp_dispatch.py`)

`ACPDispatcher` is the single M-tier entry point for the envelope +
cut + lock pattern. It owns:

1. **Read the canonical template** from
   `skills/_acp/sub-agent-prompt.md` (override via
   `--template`).
2. **Fill the seven placeholders** in input order, raising
   `ValueError` on any missing value. Mirrors the
   `tests/test_acp_hand_off.py` lint contract.
3. **Cut a worktree per PR** via `git worktree add -b <branch>
   <path> origin/main`. Fails closed on duplicate paths or pre-existing
   branches; cleans up partial state on failure.
4. **Write one envelope file per PR** under
   `<orch_worktree>/.dev-kit/round-<descriptor>/dispatches/`.
5. **Return `DispatchResult` per PR** so the M can write `meta.json`
   and `handoffs.md` without re-reading the dispatcher.

CLI shape:

```bash
python3 lib/acp_dispatch.py \
    --round thin-harness \
    --prs "PR-3:l6-alpha,PR-2:launcher" \
    --parent-session-cwd /Users/sanghee/dev/dev-harness-kit \
    --plugin-version-target 0.3.84 \
    --dry-run
```

`--dry-run` renders the envelopes without touching the filesystem —
useful for `plan` and `review` stages where the M wants to preview the
fan-out before cutting.

## 4. Parallel worktree-cut pattern

The dispatcher's fan-out loop extends the single-cut pattern in
`hooks/worktree-auto-cut.sh:247-269`. Where the hook cuts ONE worktree
per task prompt, `ACPDispatcher.dispatch()` cuts N worktrees from a
single M invocation, with these guarantees:

- **Per-PR isolation**: each `git worktree add` runs in its own
  subprocess. One failure rolls back that worktree only (`git worktree
  remove --force` + `git branch -D`); other worktrees stay.
- **Lock per branch**: each T owns a lock file at
  `<round>/locks/<branch>.lock`. The dispatcher does not `flock` the
  locks itself — the M holds them externally and passes the path in
  the envelope. This keeps the dispatcher stateless so the M can
  re-dispatch a failed PR without lock coordination.
- **Pre-existing branch refuse**: a PR whose target branch already
  exists (e.g. a stale branch from a prior aborted dispatch) raises
  `FileExistsError`. The M can rename + re-dispatch; this prevents the
  silent-collision class of bugs from §1 #2 of `docs/architecture/acp-harness.md`.

The same shape works for both `single` and `parallel` orchestration
modes (`git config --global dev-kit.orch.concurrency`). `single` runs
the dispatcher once per PR; `parallel` runs the dispatcher once with
all PRs in one batch.

## 5. First-entry-point 4-tier stack

When a dispatched T begins its session, it encounters four guards in
order. Each closes a distinct failure mode from `docs/architecture/acp-harness.md` §1.

| # | Hook (event) | Matcher | Closes | Mechanism |
|---|---|---|---|---|
| 1 | `acp-tier-assert.sh` (PreToolUse) | `*` | §1 #4 (out-of-scope ops from agents that forgot their role) | Deny the first tool call until the literal `[tier-assert] I am Tier …` line appears in the transcript. Sidecar at `tier-state/<sid>.json` caches the assert for the rest of the session. |
| 2 | `acp-cwd-discipline.sh` (PreToolUse) | `Bash` | §1 #1 (sub-agent cwd is parent checkout) | Resolves the command's intended cwd from argv (`git -C <path>`, `cd <path>`, etc.) and compares against the T's expected branch. Deny with literal reason when main is the resolved branch. |
| 3 | `worktree-guard.sh` (PreToolUse) | `Write|Edit|MultiEdit` | §1 #5 (six bash-heredoc bypass patterns) | Hard block on Edit/Write in the main checkout; the discriminator is `git_dir != git_common_dir` resolved from the worktree, not the session cwd. |
| 4 | `git-guard.sh` (PreToolUse) | `Bash` | §1 #2 (slot collision on parallel PRs) | Pre-push slot check via `bin/version-slot pre-push-gate`; refuses `git push` when current version < target slot. |

Hooks 1 and 2 are ACP-specific (this PR + PR-3); hooks 3 and 4 are the
existing project rules that ACP layers on top of.

## 6. Acceptance criteria (closes #282)

| AC | Where verified |
|---|---|
| `lib/acp_dispatch.py` exists with `ACPDispatcher.dispatch(round, prs)` | `tests/test_acp_dispatch.py::DispatchDryRun::test_dry_run_returns_results_without_cutting` |
| 7 mandatory placeholders filled | `tests/test_acp_dispatch.py::FillPlaceholders::test_seven_placeholders_match_canonical_template` |
| 3-PR decomposition → 3 worktrees + 3 envelopes | `tests/test_acp_dispatch.py::DispatchFullCut::test_three_prs_produce_three_worktrees_and_three_envelopes` |
| `hooks/hooks.json` PreToolUse `*` wired to `acp-tier-assert.sh` | `tests/test_acp_tier_assert.py::WiringTests::test_hooks_json_wires_pretooluse_star_to_acp_tier_assert` |
| Tier-assert lint denies missing / malformed assertions | `tests/test_acp_tier_assert.py::BehaviorTests::test_missing_assertion_denies` + `test_malformed_assertion_denies` |
| Tier-assert lint allows valid T assertion and caches via sidecar | `tests/test_acp_tier_assert.py::BehaviorTests::test_valid_t_assertion_allows` + `test_repeat_call_with_sidecar_is_noop` |
| Hook fails closed when `jq` missing | `hooks/acp-tier-assert.sh:23-26` (deny + exit 2 self-contained printf, mirrored from `worktree-guard.sh:74-79`) |

## 7. Out of scope

- **`lib/acp_hand_off.py`** — the hand-off lint (`tests/test_acp_hand_off.py`)
  is a sibling PR (T3 in the thin-harness round).
- **`bin/version-slot`** — extracted into a separate PR; the dispatcher
  accepts the pre-computed value via `--plugin-version-target`.
- **`hooks/acp-cwd-discipline.sh`** — sibling PR; the dispatcher's
  argv-resolving pattern is documented in §4 but the hook itself is
  out of scope here.
- **Auto-dispatch on T completion** — explicitly forbidden by
  `docs/architecture/acp-harness.md` §1 #3. The M reads the `[tier-done]` line
  emitted by the T and decides what runs next; the dispatcher does not
  spawn follow-up PRs.

## 8. Related

- `docs/architecture/acp-harness.md` — ACP design SSOT (§1–§6).
- `skills/_acp/sub-agent-prompt.md` — canonical dispatch template.
- `hooks/acp-tier-assert.sh` — tier-assertion lint (this PR).
- `hooks/acp-cwd-discipline.sh` — cwd-discipline hook (sibling PR).
- `hooks/worktree-guard.sh` — pre-existing worktree rule; layered on top of.
- `hooks/git-guard.sh` — pre-existing pre-push slot gate; layered on top of.
- `lib/acp_dispatch.py` — M-tier dispatcher (this PR).
- `tests/test_acp_dispatch.py` — dispatcher regression (this PR).
- `tests/test_acp_tier_assert.py` — tier-assert wiring + behavior (this PR).
- `rules/git-workflow.md` — worktree + branch protocol.
- `rules/session-hygiene.md` — model selection + cache discipline for ACP dispatches.
- `bin/version-slot` — slot allocator (sibling PR).
- Issue #282 — original feature request.
- PR #266 (`feat/p3-skill-governance-gate`) — L6/L7 Iron Laws source.
- PR #270 (`fix/worktree-guard-routing-question`) — version-slot rule prototype.