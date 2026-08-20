---
name: bump
category: ship
description: Explicit version bump of `.claude-plugin/plugin.json` + push of `chore/bump-vX.Y.Z`. Mirrors the auto-bump in `.github/workflows/version-bump.yml` but user-triggered for race recovery and pre-PR explicit bumps. For catching a branch up to origin/main (==, not +1) use `/dev-kit:sync-version` instead.
alpha: state
when_to_use: |
  - User types /dev-kit:bump [major|minor|patch]
  - User wants explicit local bump before PR (e.g. cutting a release candidate)
  - Race recovery: redo an orphan bump locally after the workflow leaves a tip without a PR
allowed-tools: Read Write Bash Glob Grep
disallowed-tools: WebFetch Agent
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:bump — Explicit version bump

## What it does

Bumps `.claude-plugin/plugin.json:version` (default `patch`, or `minor` / `major` via arg), cuts a fresh `chore/bump-v${NEW_VERSION}` branch from `origin/main`, pushes it, and opens a PR whose squash-merge re-fires `.github/workflows/version-bump.yml`. The workflow's idempotency check at `version-bump.yml:97-103` then short-circuits on the bump PR's own merge and tags `dev-kit--v${NEW_VERSION}`. Hand-off writes `.dev-kit/hand-off/bump→ship.md` so `/dev-kit:ship` picks up the release gate.

## Iron Law

1. Never commit directly to `main`. Never push to `main`. (Mirrors git-workflow.md L1.)
2. Default = `patch`. `minor` / `major` reset the trailing components to 0 (semver §11).
3. Refuses to bump if `HEAD_MSG` already matches `^chore\(release\): bump dev-kit to v${CURRENT_VERSION}(\ \(#[0-9]+\))?(\[skip ci\])?$` — same idempotency shape as `version-bump.yml:98`. Prevents double-bump loops when the user re-runs after a successful workflow.
4. Refuses to bump if `.claude-plugin/plugin.json` differs from `HEAD:.claude-plugin/plugin.json` (uncommitted edit guard). User must commit or stash first.
5. No `--force` to `main`. No `gh pr merge`, ever — merging into `main` is always a human action. This skill only opens the bump PR; a human merges it.

## Pre-flight

```bash
set -euo pipefail
# 1. gh auth
if ! gh auth status >/dev/null 2>&1; then
  echo "::error::gh auth status failed; run 'gh auth login'"
  exit 1
fi
# 2. Branch must not be main
CUR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached)"
if [ "$CUR" = "main" ] || [ "$CUR" = "master" ]; then
  echo "::error::bumping from main is forbidden; cut a worktree first"
  exit 1
fi
# 3. Read current version (SSOT: jq direct read; lib/ci_setup.py:plugin_version() is the equivalent for Python callers)
CURRENT_VERSION="$(jq -r .version .claude-plugin/plugin.json)"
if ! [[ "$CURRENT_VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "::error::plugin.json:version=$CURRENT_VERSION is not MAJOR.MINOR.PATCH"
  exit 1
fi
HEAD_MSG="$(git log -1 --format=%s)"
echo "gh auth:    OK"
echo "branch:     $CUR"
echo "version:    $CURRENT_VERSION"
echo "head_msg:   $HEAD_MSG"
# 4. Idempotency check (same regex as version-bump.yml:98)
if [[ "$HEAD_MSG" =~ ^chore\(release\):\ bump\ dev-kit\ to\ v${CURRENT_VERSION}(\ \(#[0-9]+\))?\ (\[skip\ ci\])?$ ]]; then
  echo "idempotent_skip=true already at v${CURRENT_VERSION}; nothing to bump"
  exit 0
fi
# 5. Working-tree guard
if ! git diff --quiet -- .claude-plugin/plugin.json; then
  echo "::error::uncommitted version change in working tree; commit or stash first"
  exit 1
fi
# 6. Race guard: refuse if version-bump workflow is in-flight
INFLIGHT="$(gh run list --workflow=version-bump.yml --status=in_progress --json databaseId -q 'length' 2>/dev/null || echo 0)"
if [ "$INFLIGHT" != "0" ]; then
  echo "::error::version-bump.yml has ${INFLIGHT} in-flight run(s); wait for completion"
  exit 1
fi
```

## Behavior

```bash
# 1. Parse arg: default = patch
BUMP_TYPE="${1:-patch}"
case "$BUMP_TYPE" in
  major) MAJOR=$(( ${BASH_REMATCH[1]} + 1 )); MINOR=0; PATCH=0 ;;
  minor) MAJOR=${BASH_REMATCH[1]};         MINOR=$(( ${BASH_REMATCH[2]} + 1 )); PATCH=0 ;;
  patch) MAJOR=${BASH_REMATCH[1]};         MINOR=${BASH_REMATCH[2]}; PATCH=$(( ${BASH_REMATCH[3]} + 1 )) ;;
  *) echo "::error::bump type must be major|minor|patch, got ${BUMP_TYPE}"; exit 1 ;;
esac
NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
BRANCH="chore/bump-v${NEW_VERSION}"

# 2. Cut branch from origin/main (TOCTOU guard)
git fetch origin main
LOCAL_HEAD_VERSION="$(git show origin/main:.claude-plugin/plugin.json | jq -r .version)"
if [ "$LOCAL_HEAD_VERSION" != "$CURRENT_VERSION" ]; then
  echo "::error::origin/main advanced during bump (expected v${CURRENT_VERSION}, found v${LOCAL_HEAD_VERSION}); re-run"
  exit 1
fi
git checkout -B "$BRANCH" origin/main

# 3. Bump
jq --arg v "$NEW_VERSION" '.version = $v' .claude-plugin/plugin.json \
  > .claude-plugin/plugin.json.tmp
mv .claude-plugin/plugin.json.tmp .claude-plugin/plugin.json

# 4. Commit + push (no [skip ci] — global skip suppresses ALL workflows incl. auto-tag)
git add .claude-plugin/plugin.json
git commit -m "chore(release): bump dev-kit to v${NEW_VERSION}"
git push -u origin "$BRANCH"

# 5. Open PR
BODY="Auto-generated by /dev-kit:bump."
gh pr create \
  --base main \
  --head "$BRANCH" \
  --title "chore(release): bump dev-kit to v${NEW_VERSION}" \
  --body "$BODY"

# 6. Hand-off
mkdir -p .dev-kit/hand-off
cat > ".dev-kit/hand-off/bump→ship.md" <<EOF
# bump -> ship hand-off

- pr:        $(gh pr list --head "$BRANCH" --base main --state open --json number -q '.[0].number // TBD')
- new_version: ${NEW_VERSION}
- source_branch: ${BRANCH}
Next step: /dev-kit:ship
EOF
echo "wrote .dev-kit/hand-off/bump→ship.md"
```

## Rules (no exceptions)

- One bump = one branch = one PR. Never amend an existing bump branch; cut a new one.
- Default = `patch`. `minor` / `major` reset lower components to 0 (semver §11).
- Refuse backwards bumps (current `< computed` is OK; otherwise exit 1).
- No local tagging. Tag emission is the workflow's job post-merge at `version-bump.yml:231-263`. User runs `/dev-kit:ship` after the PR merges.
- No `--no-verify`. No `git push --force` to `main`. `force-with-lease` is allowed only on the bump branch, never on `main`.
- Pure bash + `jq`, no `lib/bump.py` — YAGNI per `skill-authoring.md:73`. jq is already required by `lib/ci_setup.py` and `version-bump.yml:72`.

## Hook integration

| Hook | Mode | Why |
|---|---|---|
| `stop-verify` | ON | MUST-L3: every "done" must quote `gh pr view --json number` + `git log -1 --format=%H` |
| `bash-guard` | ON | Guards `git push --force` patterns |
| `git-guard` | ON | Hard-blocks `gh pr merge` (any invocation); a human merges the bump PR |
| `secret-scan` | ON | `.claude-plugin/plugin.json` is not a credential carrier but the hook is globally on |
| `tdd-guard` | OFF | Bump is a release tool, not test authoring |
| `slop-detector` | OFF | Single-line bump commit, no prose to scan |

## Output

- **stdout**: pre-flight probe table (4 rows: gh auth / branch / version / head_msg) + computed `NEW_VERSION` + `gh pr create` URL + hand-off file path.
- **`.dev-kit/hand-off/bump→ship.md`**: PR number, `NEW_VERSION`, source branch, `Next step: /dev-kit:ship`.

## Red flags

- `git push -u origin main` — should never appear; the skill refuses.
- Pushing without a clean working tree — the skill aborts in pre-flight.
- A bump commit message containing `[skip ci]` — breaks T8 in `tests/test_bump_workflow.py` (which greps the workflow's `git commit -m "bump dev-kit to v"` line for any `[skip ci]` variant).
- Two consecutive `/dev-kit:bump patch` runs without an intervening merge → second one cuts a NEW branch (no fast-forward back into the first bump branch; same idempotency shape as the workflow).
- Bumping while `version-bump.yml` is in-flight — pre-flight exit 1.

## Next step

After the bump PR is merged and tagged via the workflow:

- `/dev-kit:ship` — release gate (verdict=Approve + main-block pass). The tag `dev-kit--v${NEW_VERSION}` is already on origin by the time this runs; ship emits the marketplace refresh + log.
- `/dev-kit:babysit-pr` — for babysitting the bump PR during review (CI red, comments).

For a fresh investigation of why the workflow failed before this skill was written, see `tests/test_bump_workflow.py` and `.github/workflows/version-bump.yml:139-146` (race-recovery block comment).
