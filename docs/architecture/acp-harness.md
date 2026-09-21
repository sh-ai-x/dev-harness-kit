---
paths:
  - "hooks/**"
  - "bin/**"
  - "tests/**"
  - "skills/**"
  - "docs/architecture/acp-harness.md"
---

# ACP — Agent Coordination Protocol (dev-harness-kit)



The ACP is the deterministic contract between the orchestrator (M), task
sub-agents (T), and leaf sub-agents (L). It encodes the coordination rules
that prior rounds discovered the hard way so future rounds do not reinvent
them.

This document is the design SSOT for ACP. Implementation lives in the
narrow PRs listed in §7; each implementation PR is constrained by the
contract pinned here.

## 1. Problem (the five symptoms this protocol closes)

| # | Symptom | Root cause | ACP clause that closes it |
|---|---|---|---|
| 1 | Sub-agent `cwd` is the parent session's main checkout, not the worktree. "Reuse this worktree" hints are not propagated; hooks trip and the agent runs from `main`. | Dispatch prompt carries no `<WORKTREE_PATH>` and no `<PARENT_SESSION_CWD>`; the child has no way to know the parent's cwd was wrong. | §3 hand-off template (mandatory placeholders) + §5 cwd-discipline hook |
| 2 | Parallel branches see the same `origin/main` HEAD and each auto-bumps `+1` from it → identical `plugin.json` versions on multiple branches (slot collision). | The "+1" auto-bump is local; no allocator allocates slots across parallel branches. | §4 version-slot allocator |
| 3 | After an agent finishes, a follow-up dispatch fires automatically ("next PR") — the user did not ask for it. | The dispatcher is event-driven, not user-gated. | §2 tier-cognition (T must stop and assert done; M decides next dispatch) |
| 4 | Agents forget whether they are M / T / L and try out-of-scope operations (commit, push, source-file edits). | The role is implicit; nothing in the dispatch prompt makes it explicit. | §2 tier-assertion + §5 orch-branch isolation already in `worktree-guard.sh` |
| 5 | Six bash-heredoc bypass patterns accumulate because the hook chain reads session cwd, not file-path. | Hooks scope by cwd, not by resolved-file-path. | §5 cwd-discipline hook scopes by `git -C <path> symbolic-ref --short HEAD` resolved from the file_path the tool is touching |

## 2. Tier-cognition contract

### 2.1 Roles (single source of truth)

| Tier | Code | Lives on branch | Owns | Forbidden from |
|---|---|---|---|---|
| **M** | Orchestrator | `orch/<round-slug>` (or the M's session-worktree of choice) | Round state, dispatch decisions, `handoffs.md` writes | Editing code, hooks, tests, manifests; pushing/committing to `main`; auto-dispatching after a T finishes |
| **T** | Task sub-agent | `fix/<slug>` / `feat/<slug>` / `refactor/<slug>` / `chore/<slug>` / `test/<slug>` / `docs/<slug>` / `perf/<slug>` / `hotfix/<slug>` (one branch per T) | One PR's lifecycle (branch → commits → push → PR → review response → merge → worktree cleanup) | Editing files outside the PR's scope; pushing to `main`; force-pushing after review has started |
| **L** | Leaf sub-agent | inherits its T's worktree (cwd = T's worktree) | One read-only investigation: search files, read code, summarize — no edits | Any `Edit`/`Write`/`MultiEdit`; any `git commit`/`git push`; any `gh` mutation |

### 2.2 Tier-assertion (mandatory on first tool call of every dispatched agent)

Every M, T, and L agent MUST emit, on its **first tool call of the session**, the literal line:

```
[tier-assert] I am Tier <N> (<M|T|L>). cwd is <WORKTREE_PATH>. I own <OWNERSHIP_SENTENCE>.
```

where:

- `<N>` is `1` for M, `2` for T, `3` for L.
- `<WORKTREE_PATH>` is the absolute path of the worktree the agent's session cwd resolves to. For L, that is the inherited T's worktree; for M, that is the orch-branch worktree.
- `<OWNERSHIP_SENTENCE>` is one of:
  - M: `the round state and dispatch decisions only`
  - T: `ONE PR's lifecycle on branch <BRANCH>`
  - L: `read-only investigation for T on branch <BRANCH>; no edits`

### 2.3 Tier-assertion lint (`hooks/acp-tier-assert.sh`)

- **Event**: `PostToolUse`, matcher `Bash | Edit | Write | MultiEdit`.
- **Trigger**: fires on every agent's first non-empty tool call. The hook tracks per-session state in a sidecar file under `<orch_worktree>/.dev-kit/round-<descriptor>/tier-state/<session-id>.json` (see §6).
- **Behavior**: reads the first ~4 KiB of the agent's session transcript from stdin (`{"transcript":"..."}`); if the literal `[tier-assert] I am Tier` prefix is absent OR the `<WORKTREE_PATH>` does not match the discriminator (`git_dir != git_common_dir` resolved from the cwd) OR the `<OWNERSHIP_SENTENCE>` is malformed, **deny with reason** naming the missing field.
- **Fail-closed contract**: missing `jq` → `PreToolUse` deny + exit 2 (same `require_jq` pattern as `hooks/lib/payload-parse.sh`).
- **Test surface**: `tests/test_acp_tier_assert.py` — covers: presence + absence + cwd-mismatch + malformed ownership + jq-missing fail-closed + empty stdin no-op + non-Bash/Edit/Write matcher no-op.

## 3. Hand-off format

### 3.1 Canonical template

The canonical sub-agent prompt template lives at `skills/_acp/sub-agent-prompt.md`. The leading underscore on `_acp` distinguishes it as a *private template directory*, not a discoverable skill (project skill rules: `rules/skill-authoring.md`).

### 3.2 Mandatory placeholders (the seven)

Every dispatch MUST populate all seven placeholders. The hand-off lint (`tests/test_acp_hand_off.py`) refuses any dispatch prompt missing one.

| Placeholder | Resolved from | What goes wrong if missing |
|---|---|---|
| `<TASK>` | the user request or the M's decomposition step | sub-agent has no goal |
| `<BRANCH>` | `git symbolic-ref --short HEAD` of the worktree | T edits on the wrong branch (misfire §1 #1) |
| `<WORKTREE_PATH>` | absolute path of the worktree dir | sub-agent `cd`s to the parent checkout (misfire §1 #1) |
| `<CWD>` | the cwd the dispatched agent must use (always = `<WORKTREE_PATH>`) | cwd is parent session's checkout (misfire §1 #1) |
| `<PLUGIN_VERSION_TARGET>` | `bin/version-slot compute <PR_INDEX>` output (§4) | slot collision (§1 #2) |
| `<LOCK_FILE>` | absolute path to `<orch_worktree>/.dev-kit/round-<descriptor>/locks/<branch>.lock` | two T's race the same branch (misfire §1 #1, §6) |
| `<PARENT_SESSION_CWD>` | absolute path of the M's cwd when it dispatched | sub-agent has no way to detect the parent-cwd misfire (§1 #1) |

### 3.3 Test surface

`tests/test_acp_hand_off.py` enforces:

- The template file exists at `skills/_acp/sub-agent-prompt.md`.
- The template's frontmatter (if any) declares it is a template, not a skill (no `name:`/`category:` matching the `skills/<name>/SKILL.md` shape).
- All seven placeholders are present as literal `<NAME>` strings.
- A redacted sample dispatch (provided in the test fixture) parses and resolves every placeholder against a stub orch-worktree.

## 4. Version-slot allocator

### 4.1 Algorithm (canonical)

```
slot = origin/main HEAD .claude-plugin/plugin.json version + (PR_merge_index - 1)
```

where `PR_merge_index` is 1-based: the first PR that lands after `origin/main`'s HEAD gets `+0` (no bump — it equals main's version), the second gets `+1`, and so on. **Naive `+1` per PR is wrong** because parallel branches all start from `origin/main` HEAD and would all bump `+1`.

### 4.2 The four subcommands (`bin/version-slot`)

| Subcommand | Stdout | Exit | Purpose |
|---|---|---|---|
| `bin/version-slot compute <PR_INDEX>` | the slot version (e.g. `0.3.84`) | 0 | T computes what its target version is |
| `bin/version-slot check` | `ok` or `drift: current=X target=Y` | 0 if ok else 1 | T verifies its branch's `plugin.json` matches its slot before push |
| `bin/version-slot pin <PR_INDEX>` | re-pins `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` to the slot version (writes both files in the current worktree) | 0 | T re-pins after a slot drift |
| `bin/version-slot pre-push-gate` | `ok` or the drift reason | 0 if ok else 1 | invoked from `git-guard.sh` pre-push; refuses to push when current < target |

### 4.3 Existing inline helper (reference, not canonical)

`hooks/worktree-guard.sh` already carries a `_compute_version_slot` bash function (lines 46–62) that is the prototype. The future `bin/version-slot` implementation MUST be the deterministic extraction of that helper into a standalone Python script (so it is testable, lintable, and callable from pre-push hooks). The inline helper stays as a thin reference; removing it is out of scope for the slot PR.

### 4.4 Test surface

`tests/test_version_slot.py` covers:

- `compute` against a stubbed `git show origin/main:.claude-plugin/plugin.json` (Python `unittest.mock` against `subprocess.run`).
- `check` passes when branch's `plugin.json` matches slot, fails with the drift string otherwise.
- `pin` writes both `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` and bumps the trailing integer by exactly `(PR_INDEX - 1)`.
- `pre-push-gate` exits 0 when branch ≥ target, exits 1 when branch < target, includes the drift reason on stderr.
- Fallback (no `origin/main` reachable): the script returns `0.3.75` (the documented fallback) and stderr-logs `WARN: origin/main unreachable; using fallback`.

## 5. Worktree-cwd discipline

### 5.1 Hook contract (`hooks/acp-cwd-discipline.sh`)

- **Event**: `PreToolUse`, matcher `Bash`.
- **Discriminator**: when the Bash command's argv starts with `git`, `gh`, `core.hooksPath=`, `cat >`, `cat >>`, `tee `, or any write-tool heredoc (`<<EOF`, `<<-EOF`, `<<'EOF'`), resolve the **command's intended working dir** from the argv (default: parent's cwd) and compare its `git symbolic-ref --short HEAD` to the expected branch.
- **Source of "expected branch"**: for `git <anything>`, parse `git -C <path>` if present, else use parent cwd; for `cat > <path>`, resolve `<path>` to absolute and `git -C <absolute_dir>`; for `gh`, use parent cwd.
- **Deny condition**: the resolved branch is `main` AND the command is not a read-only inspection (`git status`, `git log`, `git diff`, `git show`).
- **Deny reason**: literal — "Use `git -C <expected-worktree>` or `cd <expected-worktree> && <cmd>`. Branch resolution returned main for command: <command_prefix>."
- **Fail-closed contract**: missing `jq` → deny + exit 2 (same `require_jq` pattern).
- **Out of scope**: `git status`/`git log`/`git diff`/`git show` against `main` are allowed (they are read-only and the user may want to compare). Mirroring the existing `bash-guard.sh` heredoc block list (`base64`, `python -c`, `python3 -c`); the future impl PR widens that list to cover the six documented bypass patterns.

### 5.2 Mirroring in `bash-guard.sh`

`hooks/bash-guard.sh` already blocks `git push --force` and a small set of destructive patterns. The cwd-discipline hook complements it; **no change to `bash-guard.sh` is required** for the cwd-discipline contract. (If a future round finds that `bash-guard.sh`'s block-list grows stale, that is a separate PR on `bash-guard.sh`.)

### 5.3 Test surface

`tests/test_acp_cwd.py` covers:

- `git commit` from a non-orch `main` checkout → deny with the literal reason.
- `git push` from a non-orch `main` checkout → deny.
- `cat > .worktrees/fix-x/foo.sh <<EOF` from the main checkout → deny.
- `git -C .worktrees/fix-x commit ...` from the main checkout → allow (the `-C` re-roots the command).
- `cd .worktrees/fix-x && git commit ...` from the main checkout → allow (the `cd` re-roots; hook reads argv after `cd`).
- `git status` from the main checkout → allow (read-only inspection).
- jq missing → deny + exit 2.
- Empty stdin → exit 0.
- Non-Bash matcher (e.g. Edit) → no-op (hook is Bash-scoped).

## 6. Round-meta protocol

### 6.1 Round directory

Each round's metadata lives at **`<orch_worktree>/.dev-kit/round-<descriptor>/`** where `<orch_worktree>` is the absolute path of the M's worktree and `<descriptor>` is a short kebab-case slug for the round (e.g. `p3-skill-governance`).

Layout:

```
<orch_worktree>/.dev-kit/round-<descriptor>/
├── handoffs.md         # M-only writer; T/L readers (see §6.2)
├── locks/              # branch-locks for parallel T dispatch (see §6.3)
│   └── <branch>.lock   # flock(2)-style file lock per T branch
├── tier-state/         # per-session tier-assert sidecar (see §2.3)
│   └── <session-id>.json
└── decisions.md        # M-only; material design choices and rationale
```

### 6.2 `handoffs.md` write discipline

- **M is the sole writer.** T and L append a hand-off note to their dispatch prompt's `<TASK>` block; they NEVER edit `handoffs.md` themselves.
- M appends entries on each dispatch (`## <timestamp> — dispatch T(<branch>): <one-line task>`) and each T-completion (`## <timestamp> — T(<branch>) done: <exit summary>`).
- T's read-only access to `handoffs.md` is via the dispatch prompt's embedded copy of the relevant entries (the M embeds the last N entries in the dispatch).

### 6.3 Lock isolation

- Per-branch flock lock at `<orch_worktree>/.dev-kit/round-<descriptor>/locks/<branch>.lock`.
- Acquired by the dispatcher (M) before spawning the T that will own that branch.
- Released by M after the T's `git push` succeeds (or on explicit user cancel).
- A `git push` outside the lock → denied by a paired `git-guard.sh` rule (out of scope for THIS issue; tracked in the future lock-isolation PR).

### 6.4 Round close

- Round directory is removed **only by explicit user request** (`rm -rf .dev-kit/round-<descriptor>/`).
- No auto-cleanup. No `git clean`. No `git worktree remove` from the round directory.
- The M's worktree removal follows the normal `git worktree remove` discipline in `rules/git-workflow.md` §5.

## 7. Acceptance criteria → future PR mapping

Each acceptance-criterion bullet from issue #274 maps to one future implementation PR. **None of those PRs are part of this design issue.** This design issue ships §1–§6 as the contract; the PRs below implement against it.

| AC bullet (issue #274) | Future PR (one each, narrow scope) | Touches |
|---|---|---|
| Tier-cognition assertion in `tests/test_skill_governance.py` (L6 enforcement) | `feat(acp-tier-assert): lint hook + governance test` | `hooks/acp-tier-assert.sh`, `hooks/hooks.json`, `tests/test_acp_tier_assert.py`, `tests/test_skill_governance.py` |
| Hand-off template + tests | `feat(acp-hand-off): canonical template + lint` | `skills/_acp/sub-agent-prompt.md` (template scaffold only — the template ships with this design issue), `tests/test_acp_hand_off.py` |
| `bin/version-slot` script + tests | `feat(acp-version-slot): standalone allocator + pre-push gate` | `bin/version-slot`, `tests/test_version_slot.py`, `hooks/git-guard.sh` (paired pre-push rule) |
| `hooks/acp-cwd-discipline.sh` + tests | `feat(acp-cwd-discipline): bash-scoped worktree resolver` | `hooks/acp-cwd-discipline.sh`, `hooks/hooks.json`, `tests/test_acp_cwd.py` |
| `docs/deterministic-harness.md` updated with ACP sections | `docs(acp): merge into deterministic-harness.md` | `docs/deterministic-harness.md` (renames `docs/architecture/acp-harness.md`; pulls §1–§6 into the umbrella doc; adds force-push safety and lock-isolation sections) |
| Round-meta write discipline: M-only `handoffs.md` | `chore(acp-round-meta): hand-off lock + lifecycle doc` | `docs/architecture/acp-harness.md` §6 cross-link; a future enforcement hook (out of ACP scope; tracked separately) |
| No PR adds new docs UNLESS explicit user ask | enforced at review time by `/dev-kit:review` (existing gate) | n/a |

## 8. Out of scope (explicit)

- **A "master ACP orchestrator" process** — the M role stays the existing orchestrator pattern (Claude Code session + Codex sub-agent). No new daemon.
- **A separate `bin/` daemon for sub-agent dispatch** — the Agent tool stays the dispatch primitive.
- **Hooks on consumer projects** — this protocol lives in `dev-harness-kit` only.
- **A consumer-project `.claude-plugin/plugin.json` migration** — the slot allocator's fallback (`0.3.75`) handles unreachable `origin/main`; no migration tool ships in this issue.
- **`docs/deterministic-harness.md` creation or update** — future PR; tracked in §7.
- **Removal of the inline `_compute_version_slot` from `hooks/worktree-guard.sh`** — the inline helper stays as a reference until the standalone `bin/version-slot` is battle-tested.

## 9. Related

- PR #266 (`feat/p3-skill-governance-gate`) — L6/L7 Iron Laws source (`alpha:` frontmatter gate, `tests/test_skill_governance.py`).
- PR #270 (`fix/worktree-guard-routing-question`) — version-slot rule + inline `_compute_version_slot` prototype.
- `rules/git-workflow.md` — branch + worktree protocol (every-task-new-worktree rule).
- `rules/skill-authoring.md` — skill frontmatter contract (L6 alpha gate).
- `rules/session-hygiene.md` — model selection + cache discipline for ACP dispatches.
- `hooks/worktree-guard.sh` — orch-branch isolation (M lives on `orch/*`; only `.dev-kit/round-*/**` is writable from a `orch/*` worktree).
- `hooks/lib/worktree-detect.sh` — shared `worktree_detect()` discriminator (single source of truth).
- `hooks/lib/payload-parse.sh` — shared `require_jq` + `deny` envelope (fail-closed pattern reused by every ACP hook).
- `tools/token_efficiency_analyzer.py` — session cost dashboard; ACP dispatch prompts MUST follow session-hygiene §3 (volatile content in prompt tail).
- Memory: `feedback-rounds-leave-no-files.md`, `feedback-minimal-action-on-vague-prompts.md`.
