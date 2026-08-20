---
name: ci-update
category: bootstrap
description: Detect + selectively apply drift between installed CI templates and current dev-kit source. 4-state per-file classification with backup-before-overwrite.
alpha: state
when_to_use:
  - User types /dev-kit:ci-update
  - Consumer wants to see what dev-kit changed since their last /dev-kit:ci-setup
  - User is preparing a PR that refreshes stale CI templates
---

## Invocation

Arguments: `$ARGUMENTS` — forwarded to the skill body. Common shapes:

- `/dev-kit:ci-update` — dry-run diff + table. No writes.
- `/dev-kit:ci-update --apply` — selective apply with prompts on consumer-modified + diverged.
- `/dev-kit:ci-update --force` — overwrite all four states with backup.

## What it does

Delegates to `skills/ci-update/SKILL.md`. Reads the consumer marker
(`.dev-kit/ci-config.json`), classifies every `EXPECTED_PATHS` entry
into one of four drift states (`new` / `updated` / `consumer_modified`
/ `diverged`), and offers a safe apply path that backs up before
overwriting. Closes the dev-kit ⇄ consumer gap that previously left
consumers blind to plugin upgrades.

## Hand-off

After a successful apply, the skill recommends `/dev-kit:ci-doctor` to
confirm broader CI readiness (secrets, gh auth, branch protection),
then commit + push + open a PR with the refreshed templates and
marker.

See `skills/ci-update/SKILL.md` for the full 3-phase orchestration
spec, exit codes, and Iron Laws.
