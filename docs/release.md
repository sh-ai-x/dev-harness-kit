# Release process — operator-facing reference

This document is the **single source of truth** for how dev-harness-kit's
release machinery works. Other docs (e.g. `docs/reverts/2026-08-02-revert-eval-prs.md`,
`docs/skills/bump.md`, `skills/sync-version/SKILL.md`) link here instead of
re-stating the flow.

For the why and the alternatives considered, see
[`docs/proposals/release/plugin-version-bump-via-merge-queue.yaml`](proposals/release/plugin-version-bump-via-merge-queue.yaml).

## OLD (pre-2026-08-30) vs NEW

### Trigger that advances the version

| | OLD | NEW |
|---|---|---|
| Trigger | `pull_request: types: [closed]` on `main` | `merge_group` |
| Files touched | none (workflow emits a `git push origin HEAD:main`) | none (the bump lives inside the merge_group ref) |
| Per-PR conflict on `plugin.json:version` | **yes** — every parallel PR hit it | **no** — merge queue rebases each PR onto bumped main before merge |

### What feature PRs had to do

| | OLD | NEW |
|---|---|---|
| Conflict resolution | author runs `git fetch && git merge origin/main && git push` (or `git rebase`) | nothing — queue handles it |
| Auto-sync hook (`.githooks/pre-push`) | mutates working tree via `bin/sync-version.sh --target <main>` and creates a `chore(sync): ...` commit | prints a NOTICE only; no mutation |
| `/dev-kit:sync-version` skill | mutates working tree | no-op compat shim (see `skills/sync-version/SKILL.md`) |
| `bin/sync-version.sh` | real SYNC implementation | no-op compat shim (preserves CLI surface) |

### Tagging

| | OLD | NEW |
|---|---|---|
| Tag emit | after the bump commit lands on main | after the bump lands in the merge_group |
| Tag shape | annotated `dev-kit--vX.Y.Z` at HEAD | unchanged |
| Tag idempotency | local `git rev-parse --verify refs/tags/<TAG>` check | unchanged |

## How the merge queue works in this repo

The GitHub Merge Queue is enabled on `main` with these properties:

- **Merge method**: squash (the merge commit message is the PR title +
  body).
- **Required status checks**:
  - existing `pull_request` checks from `ci.yml` / `review.yml` /
    `maintenance.yml` (these MUST complete before the PR is
    queue-eligible)
  - new `merge-queue-ready-check.yml` (fast gates the queue runs
    immediately before merge; documented below)
- **Auto-merge**: off. Maintainers click "Merge" / run `gh pr merge`;
  the queue handles the rest.

### What `merge-queue-ready-check.yml` enforces

The queue re-runs this workflow on `merge_group` for every queued PR.
If any of the three jobs fails, the queue surfaces the failure to the
PR author and the PR is NOT merged.

| Job | What it checks | Why it's cheap |
|---|---|---|
| `lint (ruff)` | `ruff check` + `ruff format --check` | static AST scan, no LLM |
| `validate` | jq-validates both manifests + `bash -n` every shell script | deterministic, no LLM |
| `scope` | refuses a PR that mixes production-code edits with bypass-file edits in the same commit | pure `git diff --name-only`, no LLM |

Heavy review/security/maintenance judges stay out of this path. They
already ran on `pull_request` and finished before the PR was eligible
for the queue. Adding them here would double the per-merge API spend
and (per PR Conflict Detector) is what makes merge queue slow.

## Operator steps for a maintainer

### Enabling merge queue (one-time, GitHub UI)

1. Repository Settings → Rules → Rulesets (or Branch protection rules
   on legacy) → edit the rule for `main`.
2. Enable "Require merge queue".
3. In "Required status checks" add: `Lint`, `Validate`, `Scope`
   (the three jobs in `.github/workflows/merge-queue-ready-check.yml`).
4. Keep all the other `pull_request` checks (`test`, `validate`,
   `branch-policy`, the LLM judges) also required.
5. Save. Confirm by opening a test PR — the PR's merge button should
   change to "Merge when ready" only after all `pull_request` checks
   pass, then the queue takes over.

### Bumping a version manually (operator-only)

`/dev-kit:bump` (`docs/skills/bump.md`) still works as before. It
opens a `chore(release): bump dev-kit to v...` PR; when the queue
merges that PR, `version-bump.yml`'s `merge_group` handler detects
the title and SKIPS the bump step (tag still runs) so we don't
double-bump.

### Investigating a queue failure

If `merge-queue-ready-check.yml` fails on a PR:

1. Look at the failing job's log in the Actions UI.
2. Fix the underlying issue locally (the queue does not retry
   automatically after a human pushes new commits; you need to
   re-mark the PR ready-for-review).
3. If the failure is transient (e.g. a flake in `validate`'s
   `bash -n` parallel runner), push an empty commit
   (`git commit --allow-empty -m "ci: retrigger queue"`) to re-enter
   the queue.

### Investigating a missed version bump

If you merged a PR and the version did NOT advance (visible via
`gh release list` or by checking `origin/main:.claude-plugin/plugin.json`):

1. Check the version-bump workflow run for the merge group. The
   trigger filter and concurrency group should make misses rare, but
   GitHub Actions can occasionally drop a `merge_group` event under
   heavy load.
2. Manually re-run the workflow on `main` via the Actions UI
   (`workflow_dispatch` if the workflow allows it; otherwise use
   `/dev-kit:bump` to cut a release PR and let the queue handle it).

## Where the old code lives now

| Old | Where it moved |
|---|---|
| `.githooks/pre-push` auto-SYNC block | Replaced by a NOTICE-only block (still keeps the direct-push-to-main block + opt-in intent check) |
| `bin/sync-version.sh` real sync | Replaced by a no-op shim that preserves `--check` / `--target` / `--from` / `--help` for callers that haven't migrated |
| `skills/sync-version/SKILL.md` "what it does" | Replaced by a one-paragraph pointer to this doc + the proposal |
| `.github/workflows/version-bump.yml` `pull_request: closed` trigger | Replaced by `merge_group` |
| `chore(sync): ...` commit message | Deprecated — never create one; the queue handles it |

## Evidence trail

See the proposal doc for the full evidence chain:
[proposal](proposals/release/plugin-version-bump-via-merge-queue.yaml#evidence-trail-preserved-for-auditability).

Key external references:

- [Claude Code plugin-marketplaces spec](https://code.claude.com/docs/en/plugin-marketplaces) — `version` is the primary cache-invalidation field
- [issue #43763 installed_plugins.json stale](https://github.com/anthropics/claude-code/issues/43763) — confirms runtime-computed versions break cache
- [Managing a merge queue](https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) — GitHub's official docs
- [community discussion #102764 merge_group trigger semantics](https://github.com/orgs/community/discussions/102764) — when `merge_group` fires
- [PR Conflict Detector](https://github.com/github-community-projects/pr-conflict-detector) — what a per-PR conflict-prevention tool would do (and why we chose the queue instead)

Internal:

- [`docs/proposals/release/plugin-version-bump-via-merge-queue.yaml`](proposals/release/plugin-version-bump-via-merge-queue.yaml) — the proposal
- [`docs/reverts/2026-08-02-revert-eval-prs.md`](reverts/2026-08-02-revert-eval-prs.md) — the maintainer's pre-existing flag that the version-bump workflow was a workaround
- [`docs/skills/bump.md`](skills/bump.md) — the skill whose race-recovery paragraph becomes obsolete after this lands
- [`.github/workflows/version-bump.yml`](.github/workflows/version-bump.yml) — the workflow that moved from `pull_request: closed` to `merge_group`
- [`.github/workflows/merge-queue-ready-check.yml`](.github/workflows/merge-queue-ready-check.yml) — the new required check for the queue
