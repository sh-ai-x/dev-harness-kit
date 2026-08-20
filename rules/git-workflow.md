---
paths:
  - ".claude/**"
  - "hooks/**"
  - "docs/adr/ADR-00**-*.md"
  - "tests/test_git_workflow.py"
  - "tests/test_worktree_guard.py"
stale_after: 2027-05-31
---

# Git workflow rules (dev-harness-kit)

The canonical worktree root is `.worktrees/`. This path is shared by Claude
Code and Codex; `.claude/worktrees/` and `.codex/worktrees/` are legacy paths
that remain readable but must not be used for new worktrees.

These rules apply to every code change — feature, fix, refactor, docs, test, chore.
Violation = rejected by `git-guard` hook at commit/push time.

## Iron Laws (read first)

1. **`main` is sacred.** Never commit directly to `main`. Never push to `main`. Never fast-forward a feature branch into `main` locally.
2. **Every task = new worktree + client handoff + new branch.** Claude Code
   opens a new session in the worktree; Codex spawns a subagent in it. No edits
   on the previous task's branch or in the main checkout.
3. **Always branch from latest `main`.** Cut from `origin/main` (just-fetched), not from a stale local ref or another feature branch.

## Branch naming (mandatory)

Format: **`<type>/<slug>`**

| Type | When | Examples |
|---|---|---|
| `fix/` | bug fix from review or reported defect | `fix/review-findings`, `fix/cli-nameerror` |
| `feat/` | new user-facing feature or skill | `feat/pm-prd-tracking`, `feat/eval-repair-v2` |
| `refactor/` | no behavior change, internal cleanup | `refactor/dedup-state-codec` |
| `docs/` | documentation only | `docs/adr-0021-branch-strategy` |
| `test/` | tests only, no source change | `test/team-hooks-coverage` |
| `chore/` | deps, build, CI, tooling | `chore/bump-pytest-9` |
| `perf/` | performance, no behavior change | `perf/index-json-parse` |
| `hotfix/` | emergency revert of a merged main commit | `hotfix/revert-abc123` |
| `prune/` | deletion pass over slop/dead features (not refactoring; see `/dev-kit:prune`) | `prune/dead-skill`, `prune/inspect-2026-07-17` |

### Slug rules

- **kebab-case**, lowercase, English only (no Korean, no spaces, no underscores).
- **Action-first**: describe what the branch does, not what file it touches.
  - ✅ `fix/cli-nameerror`, `feat/eval-repair-v2`
  - ❌ `fix/state_codec`, `feature_eval` (wrong type), `MyFeature` (not kebab)
- **Length**: 2–40 chars. Single-char slugs (e.g. `fix/a`) are too vague.
- No author names, no issue numbers (issue #N goes in the PR body, not the branch name).
- No personal/scratch names: `wip`, `tmp`, `foo`, `bar`, `asdf`, `test`, `scratch`, `untitled` → rejected by `tests/test_git_workflow.py::FORBIDDEN_RE`.

## Worktree + session protocol (mandatory for every new task)

```
# 1. Update local main to match origin
git fetch origin main
git checkout main
git pull --ff-only origin main

# 2. Cut a fresh worktree for the new task (auto-creates branch from origin/main)
git worktree add -b fix/<slug> .worktrees/fix-<slug> origin/main

# 2a. (When you see a `modified:` line on a file you did not touch)
# `git worktree add` does NOT overwrite a pre-existing file at the target
# path (issue #215). If the worktree dir carried stale content from a
# prior aborted operation or a `git worktree remove` that left files
# behind, the new worktree's working tree may disagree with HEAD for
# those files (the diff looks like `modified:` against the same SHA).
# That was previously recovered with `hooks/worktree-verify-clean.sh`,
# removed in #238 after the inspect audit found no live callers.
# If you hit it, recover manually per file:
#
#   git checkout HEAD -- <path>
#
# then re-run your test to confirm the divergence is gone.

# 3a. Claude Code: open a new Claude Code session IN THAT WORKTREE.
#     (the session cwd is the worktree path, not the main checkout)
# 3b. Codex: spawn/hand off a subagent with its working directory set to THAT
#     WORKTREE. The parent session may remain in the main checkout.

# 4. Do all edits, tests, commits, push — all from inside the worktree
git push -u origin fix/<slug>
gh pr create --base main --head fix/<slug> --title "..." --body "Closes #N"

# 5. After PR is merged (or abandoned), remove the worktree
git worktree remove .worktrees/fix-<slug>
git branch -d fix/<slug>        # local branch gone
```

**Why client-specific handoff?** Claude Code can enter a new interactive
session at the worktree path. Codex's parent session cannot change its status
line or cwd, so Codex must pass the worktree path, branch, task prompt, and
verification requirements to a spawned subagent.

**Why a new worktree?** Multiple branches in one checkout collide on `phases/`, `.dev-kit/`, the running test process, and uncommitted edits. A worktree is a free, isolated copy.

## PR conventions (mandatory)

- **Base branch**: `main` (always).
- **Title**: `<type>(<scope>): <subject>` (Conventional Commits)
  - Examples: `fix(review): address 10 blockers`, `feat(execute): add started_at field`
- **Body** must include:
  - Summary (2-5 bullets)
  - Test plan with quoted exit codes / test counts
  - `Closes #N` (or `Refs #N` if not auto-closing)
- **One commit per task** unless the task explicitly requires a WIP series.
- **No force-push** to shared branches (use `git push --force-with-lease` only on your own unmerged branch, never after review has started).
- **No merge commits in the PR** — rebase or squash before merge.

## Enforcement

1. **`hooks/git-guard.sh`** (PreToolUse, Bash matcher) — blocks:
   - `git commit` when current branch is `main` (deny with reason)
   - `git push` to `main` or `origin main` (deny with reason)
   - `git checkout main` followed by `git commit` in the same command
   - `git push --force` and `git push -f` (already blocked by `bash-guard.sh` — kept for redundancy)
   - **`hooks/review-yml-isolation.sh`** (PreToolUse, Bash matcher, paired with `git-guard.sh`) — blocks `git commit` when `git diff --cached --name-only` contains a file named `review.yml` AND at least one other path. The PR must be `review.yml`-only. Shipped via `ci-setup` (`EXPECTED_PATHS`, `EXECUTABLE_PATHS`, marker `hooks` list — see `lib/ci_setup.py`). **Fails closed** (deny) when `jq` is missing.
2. **`hooks/worktree-guard.sh`** (PreToolUse, Write|Edit|MultiEdit matcher) — HARD BLOCK on edits in the main checkout. Discriminator: `git rev-parse --git-dir == --git-common-dir` evaluated from the repo toplevel (canonicalized via `realpath`). Any Edit/Write/MultiEdit attempted while the session cwd is the main checkout is denied with an actionable message naming the worktree command. **Fails closed** (deny) when `jq` is missing.
3. **`hooks/session-start-check.sh`** (SessionStart) — GENTLE NUDGE at session start. If the session begins in the main checkout (not a worktree), emits an `additionalContext` reminder. Never blocks. **Fails open with a stderr warning** when `jq` is missing.
4. **`hooks/lib/worktree-detect.sh`** — shared `worktree_detect()` helper. Both rule-hooks source this so the `--git-dir`/`--git-common-dir` discriminator doesn't drift across files.
6. **`tests/test_worktree_guard.py`** + **`tests/test_git_workflow.py`** (regression) — on every CI run, asserts:
   - All non-main branches match `<type>/<slug>` format
   - No `TODO` / `wip` / `tmp` slugs in the last 30 commits' branch names
   - Recent merged PR titles follow Conventional Commits
   - `worktree-guard.sh` denies Edit/Write in the main checkout, allows in worktrees, fails closed when `jq` is missing, exits 0 on empty payload, and exits 0 outside any git repo
   - `task-detector.sh` was removed (PR-1) — early-warning advisory duplicated the worktree rule that worktree-guard.sh enforces as a hard block
   - `session-start-check.sh` nudges when started in the main checkout, stays silent in worktrees, stays silent on missing `cwd`
   - `hooks.json` wires the remaining hooks into the correct event matchers
7. **PreToolUse `stop-verify`** (existing) — at session end, runs the regression test to catch any rule violations before allowing the session to stop.

## Out of scope (intentionally not enforced)

- Branch deletion hygiene (handled by `git worktree remove` discipline).
- Merge queue / protected-branch GitHub settings (operational concern, lives in repo Settings — see ADR-0021 if added later).
- Issue template enforcement (handled by `.github/ISSUE_TEMPLATE/`).

## Exceptions

- **`hotfix/*`**: only used to revert a merged main commit. PR is auto-mergeable. Still requires a worktree (the revert is a real change). Still requires CI green.
- **Documentation-only fixes to README/CHANGELOG** that the user explicitly requests as "just fix it now" with no PR review: maintainer judgment, but **never** on `main` directly — branch + PR is the only path.

## Review.yml PR isolation

`review.yml` (both `.github/workflows/review.yml` and the template mirror
`templates/ci/.github/workflows/review.yml`) is the CI workflow that runs
`/dev-kit:review` and `/dev-kit:security` on every PR. Because the gate
verdict lives in the workflow itself, a `review.yml` change is the ONE
file that should never share a PR with unrelated edits — mixing makes
the gate's verdict unreadable (which finding belongs to which intent?)
and blocks targeted revert when only the workflow is broken.

Rule: any commit that modifies `review.yml` (basename match) must contain
ONLY `review.yml`. No source code, no other workflow files, no template
edits, no unrelated cleanup. If you also need to change other files,
make two commits on the same branch (or split into two PRs) — the gate
will still review the `review.yml` commit in isolation, and a broken
`review.yml` can be reverted without dragging unrelated diff back.

## Related

- `tests/test_git_workflow.py` (branch-naming + `git-guard` regression)
- `tests/test_worktree_guard.py` (`worktree-guard` + `session-start-check` regression)
- `tests/test_review_yml_isolation.py` (`review-yml-isolation` regression — staged-set isolation, jq-missing fail-closed, hooks.json wiring)
- `hooks/lib/worktree-detect.sh` (shared discriminator — single source of truth)
- `hooks/git-guard.sh` (PreToolUse Bash block)
- `hooks/worktree-guard.sh` (PreToolUse Edit/Write block, fails closed)
- `hooks/review-yml-isolation.sh` (PreToolUse Bash block, fails closed — `review.yml` PR isolation)
- `hooks/session-start-check.sh` (SessionStart nudge, fails open with warning)
- `hooks/hooks.json` (wires all hooks into Claude Code)

> *Removed in #238:* `hooks/worktree-verify-clean.sh`, `hooks/lib/worktree-verify-clean.sh`, and `tests/test_worktree_verify_clean.py` (issue #215) were deleted by the inspect audit; the related `## Step 2a` block above was rewritten to direct the rare recovery case to `git checkout HEAD -- <path>`.
