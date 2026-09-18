---
name: bootstrap
category: bootstrap
description: 0-arg setup for CLAUDE.md, AGENTS.md, hooks, and optional CI.
alpha: state
when_to_use: |
  - User types `/dev-kit:bootstrap` on a new project
  - User wants to refresh project pointers or active-hooks.json
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch
model: opus
disable-model-invocation: false
---
> [← Skills index](../../README.md) · Detailed reference: [docs/skills/bootstrap.md](../../docs/skills/bootstrap.md)

# `/dev-kit:bootstrap`

Run the deterministic setup pipeline, then offer the optional installers. The
unconditional output is the minimal project context: `CLAUDE.md`, `AGENTS.md`,
`.dev-kit/.active-hooks.json`, and the linked SSOT indexes. Do not create
noise files or ask recurring SessionStart questions.

## Contract

1. Run sanity, codebase-map, hook-matrix, and `lib/write_project_md.py`.
2. On the first run only, ask the guard policy question below.
3. Ask `Also install CI templates (ci-setup)? [y/N]`; N is the default.
4. If Y, call `lib/ci_setup.py:install_ci_config()`; the result matches the
   legacy `/dev-kit:bootstrap-full` end state. If N, print the unavailable
   features list and suggest `/dev-kit:ci-setup --force` later.
5. Ask `Also configure operator-global git defaults (rebase.autoStash + pull.rebase)? [Y/n]`;
   Y is the default unless `--skip-git-defaults` is supplied.
6. Resolve `$DEV_KIT_TEAM` through `hooks/lib/team-resolve.sh`; only the
   explicit team-on path may strip `.dev-kit` from `.gitignore`.

Every deterministic stage is read-only except its documented output. Preserve
existing JSON keys and use the linked docs/code as the implementation SSOT.

## Guard policy (one-time bootstrap choice)

Guards default off everywhere: main, worktrees, detached checkouts, and user
scope. User-scope plugin installation never enables repository guards.

Only when neither `.claude/settings.json` nor `.claude/settings.local.json`
already contains `env.DEV_KIT_GUARDS`, ask:

```text
Enable repository guards (worktree, git, TDD)? [y/N]
If yes, save to project scope or this checkout only? [project/local]
```

`N` leaves the effective value off. `Y + project` writes
`DEV_KIT_GUARDS=on` to `.claude/settings.json`; `Y + local` writes it to
`.claude/settings.local.json`. Preserve all existing JSON keys. Later
On later bootstrap runs, report the current value and do not ask again.
**SessionStart never asks this question**; a main-branch-specific prompt is
intentionally deferred.

Resolution is shell > local > project > default off. User scope is ignored
for this key. The policy controls `worktree`, `git`, and `TDD` guards only;
other safety and verification hooks keep their own contracts.

## Flags and outputs

Hidden flags: `--target DIR`, `--skip-sanity`, `--skip-map`, `--slim|--full`,
`--strict`, `--persist-audit`, `--skip-ci`, `--skip-git-defaults`, `--yes`,
`--force`, `--skip-verify`. `--yes` accepts CI and git defaults; it does not
change guard policy. `--skip-ci` is equivalent to answering `n`.

Without CI, `/dev-kit:ci-doctor`, `/dev-kit:bump`, the 15 workflow templates,
the pre-push hook, and `/dev-kit:evaluate`'s CI harness are unavailable until
`/dev-kit:ci-setup --force` runs. With CI, installation is idempotent unless
`--force` is supplied; `--skip-verify` skips only CI Phase 3 verification.

### y branch (when chosen)

`install_ci_config()` installs the CI workflows and marker; this is the
non-default path and preserves legacy `/dev-kit:bootstrap-full` parity.

### n branch (default)

The unavailable-features list is printed; add CI later with
`/dev-kit:ci-setup --force`.

The final hand-off is `/dev-kit:build <first-feature>` (or
`/dev-kit:plan` for an idea that needs a PRD). For the full stage audit,
template list, git-default details, and output matrix, read
[`docs/skills/bootstrap.md`](../../docs/skills/bootstrap.md).
