# dev-harness-kit

> AI-native unified harness plugin — absorbs 5 repos + A2A typed + Eval-Repair loop + Human-on-the-Loop.

[![Tests](https://img.shields.io/badge/tests-255%20passed-brightgreen)](tests/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Skills](https://img.shields.io/badge/skills-29-blueviolet)](skills/)
[![Version](https://img.shields.io/badge/version-0.1.1-blue)](.claude-plugin/marketplace.json)

## What

- **Plan+Design automation**: One `/dev-kit:plan` command auto-generates PRD.md + `phases/<name>/step<N>.md` (pm-prd-fast + interview-harness absorbed).
- **Per-step sub-agent delegation in Build**: harness-runner engine + TDD + auto-fix loop.
- **Review/Security fan-out**: 3-dim (correctness + security + architecture) + 10-dim OWASP A01–A10, parallel in a single message.
- **Eval-Repair 8-step loop**: auto-check asset freshness + Specialized Fixer + final = Human Review.
- **Human-on-the-Loop auto + user approves last**: zero response fatigue.
- **Worktree enforcement (NEW in 0.1.1)**: `worktree-guard` blocks Edit/Write in the main checkout; `task-detector` nudges new tasks to a worktree; `session-start-check` reminds at session start. See `.claude/rules/git-workflow.md`.
- **Consumer-install (NEW in 0.1.1)**: `/dev-kit:ci-setup` ships a self-aware `review.yml` that works in both the dev-harness-kit repo itself (self-install) and consumer repos (clones from public source).

## Install (plugin-only)

```bash
# marketplace install (recommended)
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit

# or local checkout
git clone https://github.com/sh-ai-x/dev-harness-kit
claude plugin marketplace add ./dev-harness-kit
claude plugin install dev-kit

# at the start of every session
/reload-plugins
```

After install, `claude plugin update dev-kit` advances the cache whenever you push. Per the [plugins docs](https://code.claude.com/docs/en/plugins), omitting `version` in the manifest pins by commit SHA — the marketplace catalog auto-bumps the pin on each merge to `main`.

## Live-source dev (alias recommended)

The marketplace install pins a specific commit SHA. When you're developing this repo, point Claude Code at your local checkout so edits are live without re-installing:

```bash
claude --plugin-dir /Users/sanghee/dev/dev-harness-kit
```

To save the keystrokes, drop a shell alias in `~/.zshrc` (or `~/.bashrc`):

```bash
alias claude-dev='claude --plugin-dir /Users/sanghee/dev/dev-harness-kit'

# Then in a project directory:
claude-dev            # loads your local edits, no rebuild step
claude                # falls back to marketplace-pinned install
```

Per the docs, when both paths are available the local copy takes precedence for that session — so `claude-dev` always wins.

> **Don't symlink `~/.claude/skills/dev-kit`** to the repo. The marketplace install and a skills-dir plugin carrying the same `name` collide; the loader rejects the second copy. If you want a no-flag live-source install, use the alias above.

> **CLI Node compatibility.** The bundled Claude Code CLI crashes on Node ≥ 25 (`TypeError: Cannot read properties of undefined (reading 'prototype')` from `cli.js:384`). Run `claude plugin …` and `claude plugin update` on Node 22 (`nvm install 22 && nvm use 22`). The `--plugin-dir` flag isn't affected; it bypasses the failing CLI path entirely.

## Plugin cache refresh (when and how)

The marketplace install pins a specific commit SHA. After a PR is merged to `main`, your local plugin cache (`~/.claude/plugins/cache/dev-kit/dev-kit/<sha>/`) is stale until refreshed.

**When to refresh:**
- After a PR is merged to `main` and you want the new behavior in your current session
- When `/dev-kit:*` skill output doesn't match the latest source
- When a consumer repo's `/dev-kit:ci-setup` reports missing files (e.g. `scripts/branch-policy.sh: No such file or directory`) — the cache is stale

**Three refresh paths, ordered by recommendation:**

```bash
# 1. devkit-refresh.sh (RECOMMENDED for maintainers + power users)
#    git pull marketplace clone + rsync to cache.
#    Bypasses the `claude plugin install` Node TypeError bug.
bin/devkit-refresh.sh

# 2. claude plugin update (uses marketplace catalog auto-pin bump)
#    Requires Node 22 (see CLI Node compatibility above).
claude plugin update dev-kit

# 3. Manual rsync (escape hatch when both above fail)
cd ~/.claude/plugins/marketplaces/dev-kit && git pull origin main --ff-only
rsync -a --delete --exclude=.git \
  ~/.claude/plugins/marketplaces/dev-kit/ \
  ~/.claude/plugins/cache/dev-kit/dev-kit/$(git -C ~/.claude/plugins/marketplaces/dev-kit rev-parse HEAD)/
```

After any of the three, run `/reload-plugins` in your session so the new SHA is picked up.

**Why `devkit-refresh.sh` exists:** `claude plugin install dev-kit --force` and `claude plugin update dev-kit` both invoke the same CLI path that throws `TypeError: Cannot read properties of undefined (reading 'prototype')` from `cli.js:384` when called from inside a Claude Code session. A SessionStart hook that auto-ran the install was tried (PR #24) and reverted (PR #26) for the same reason. `devkit-refresh.sh` uses raw `git pull` + `rsync`, which works in every environment.

## Usage (0-arg, namespaced)

```
/dev-kit:bootstrap              # first entry (auto-generate CLAUDE.md)
/dev-kit:ci-setup                # install dev-kit CI templates (workflows + hooks + scripts + worktree-rule files) into target repo
/dev-kit:plan                    # PRD + phases auto (Plan+Design unified)
/dev-kit:build                   # run per-step sub-agents
/dev-kit:review                  # 3-dim code review (correctness + security + architecture)
/dev-kit:security                # 10-dim OWASP A01–A10
/dev-kit:audit                   # batch slop + secret audit
/dev-kit:eval                    # asset freshness eval
/dev-kit:repair approve|reject|defer <asset>   # Eval-Repair Human Review
/dev-kit:ship                    # release tag
/dev-kit:babysit-pr              # 0-arg PR babysitter loop

# Shortcuts (urgent hotfix)
/dev-kit:tdd-fast                # skip Bootstrap+Plan → straight to Build
/dev-kit:quick-fix               # verify+debug on demand
```

Full set: 29 skills. Invoke with `/<skill-name>` or `dev-kit:<skill-name>`.

## Directory layout

```
dev-harness-kit/
├── .claude-plugin/        # marketplace.json (source: url object) + plugin.json
├── skills/                # 29 skills, flat: skills/<skill-name>/SKILL.md
├── hooks/                 # 9 hook scripts (5 original + 4 worktree-rule) + lib/ + hooks.json
├── lib/                   # state_codec / active_hooks_codec / write_project_md / execute / methodology/ / ci_setup
├── bin/                   # devkit-refresh.sh (manual cache refresh, optional)
├── templates/ci/          # CI workflow templates shipped to consumer repos via /dev-kit:ci-setup
├── tests/                 # 255 tests (pytest) — 18 files
├── eval/                  # golden set (13 assets) + judge prompts + fixtures
├── docs/                  # STAGES, NAMING, COST-ANALYSIS, PRE-IMPL-CHECK, ci-setup, adr/
└── CLAUDE.md              # SSOT (auto-generated by /dev-kit:bootstrap)
```

### Hook scripts (9)

| Hook | Event | Purpose | Mode |
|---|---|---|---|
| `tdd-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | TDD test-first enforcement | advisory / `--strict` |
| `bash-guard.sh` | PreToolUse (Bash) | Block destructive commands | advisory / `--strict` |
| `git-guard.sh` | PreToolUse (Bash) | Branch strategy enforcement | hard-block |
| `worktree-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | **Block edits in main checkout** | hard-block |
| `task-detector.sh` | UserPromptSubmit | Nudge new tasks to a worktree | advisory |
| `session-start-check.sh` | SessionStart | Remind about worktree rule at session start | advisory |
| `secret-scan.sh` | PostToolUse (Write\|Edit) | Detect credentials in edits | hard-block |
| `slop-detector.sh` | PostToolUse (Write\|Edit) | Block AI slop in edits | hard-block |
| `stop-verify.sh` | Stop | Run regression tests on session end | hard-block |

The 3 new hooks (worktree-guard, task-detector, session-start-check) implement the worktree enforcement rule. The worktree-rule scripts (hooks + `lib/worktree-detect.sh`) also ship to consumer repos via `templates/ci/`.

### Skill categories (14)

| Category | Skills |
|---|---|
| `bootstrap` | `bootstrap`, `bootstrap-active-hooks`, `bootstrap-codebase-map`, `bootstrap-sanity`, `ci-setup` |
| `plan` | `plan`, `plan-ralph` |
| `design` | `design` (merged into `plan`), `build-harness-engine` |
| `build` | `build`, `build-debug`, `build-engine`, `build-methodology`, `build-simplify`, `build-tdd`, `build-verify` |
| `review` | `review` (3-dim, unified) |
| `security` | `security` (10-dim OWASP, unified) |
| `audit` | `audit`, `audit-secret`, `audit-slop` |
| `eval` | `eval` |
| `onboard` | `onboard` |
| `repair` | `repair` |
| `ship` | `ship`, `babysit-pr` |
| `config` | `config` |
| `status` | `status` |
| `shortcuts` | `shortcut-quick-fix`, `shortcut-tdd-fast` |

Category is preserved in each SKILL.md `category:` frontmatter. Directory nesting is none (Claude Code plugin rule: `skills/<name>/SKILL.md`, one level).

## Worktree rule (new in 0.1.1)

The `.claude/rules/git-workflow.md` rule makes this a hard requirement:

> **Every task = new worktree + new session + new branch.** No edits on the previous task's branch. No edits in the main checkout.

Enforced by 3 hooks:
- `worktree-guard.sh` — hard-block any Edit/Write in the main checkout
- `task-detector.sh` — early warning on new-task prompts ("implement X", "add Y", etc.)
- `session-start-check.sh` — gentle reminder at session start

Plus `bin/devkit-refresh.sh` for the consumer side (refresh cache after PR merge via `git pull` + `rsync`).

## Consumer-install via `/dev-kit:ci-setup` (new in 0.1.1)

`/dev-kit:ci-setup` is what makes dev-kit work in OTHER repos. It copies:
- 3 GitHub Actions workflows (ci, auto-fix-pr, review)
- 4 scripts (validate, test, branch-policy, ci-local)
- 1 pre-push hook
- **NEW in 0.1.1**: 4 worktree-rule files (hooks, lib, rule, tests)

Total: 15 files. The shipped `review.yml` is **self-aware**: it detects whether the checkout IS the dev-kit plugin (self-install, symlink) or a plain consumer repo (clones from public), so the same workflow file works in both contexts.

After install, consumer repos get:
- The branch strategy enforced
- TDD + slop + secret hooks
- A worktree rule that prevents editing in the main checkout
- 4 regression tests for the worktree rule

### `/dev-kit:ci-setup --force` — when to use vs when NOT

`ci-setup` is **idempotent by default**. The marker file `.dev-kit/ci-config.json` records installed_at + content hashes; if all 15 EXPECTED_PATHS files are present and match, the re-run is a no-op (skip + report). `--force` overwrites the 15 EXPECTED_PATHS files regardless.

**Use `--force` when:**
- A new template was added to dev-kit (e.g. `templates/ci/scripts/branch-policy.sh`) and you want it on your consumer repo
- A template was fixed in dev-kit (e.g. PR #45 patched a stale `review.yml` gate) and you want the fix
- You suspect the install is stale: marker exists but a template file is missing or drifted (often from a stale plugin cache — see "Plugin cache refresh" above)
- The lint pass on a previous install reported warnings that need a refresh to clear

**Do NOT use `--force` when:**
- First install on a fresh repo (default install is correct)
- Re-running after no upstream changes (default is a no-op skip; `--force` creates noise in the diff and may overwrite local customizations)
- The consumer repo has hand-edited any of the 15 EXPECTED_PATHS files (e.g. a customized `branch-policy.sh`). `--force` overwrites your edits — review the diff before committing
- You're unsure what changed in dev-kit since your last install. Run `git log --oneline templates/ci/` in your dev-kit checkout first

**Workflow:**
```bash
# 1. Refresh dev-kit plugin cache (see section above) so you see the LATEST templates
bin/devkit-refresh.sh

# 2. Run ci-setup --force in the consumer repo
cd /path/to/consumer-repo
claude --plugin-dir /Users/sanghee/dev/dev-harness-kit   # or use marketplace install
/dev-kit:ci-setup --force

# 3. Review the diff before committing — ci-setup prints a per-file created/overwritten/skipped table
git diff .github/ scripts/ .githooks/ hooks/ .claude/ tests/

# 4. Commit + push
git add -A && git commit -m "chore(ci): refresh dev-kit templates"
```

## Design principles (ADR-0011~22)

- **NO-DUP**: Iron Law in one place (CLAUDE.md §1). 2-layer verification (hook + skill).
- **NO-BOTTLENECK**: 0-arg UX. Lazy CLAUDE.md. Sub-agents in parallel.
- **NO-MEANINGLESS-LOOP**: 5-field loop semantics + auto-STOP + user interrupt.
- **HOTL (MUST-29)**: auto-progress + user supervisor + 1× interrupt.
- **Methodology extension (MUST-48)**: TDD/SDD/DDD/BDD/FDD selectable.
- **A2A typed (MUST-39)**: Sub-agent ↔ main JSON Schema SSOT.
- **Plugin-only (v0.1.0+)**: `.claude/skills/`, `commands/`, `install.sh` all removed. plugin manifest is SSOT.
- **Worktree-per-task (v0.1.1+)**: every task is a new worktree + new session + new branch. Enforced by 3 hooks, documented in `.claude/rules/git-workflow.md`.
- **Consumer-install (v0.1.1+)**: the same `review.yml` + worktree-rule files work in both dev-harness-kit and consumer repos via a self-aware install step.

## Contributing

Pass pre-impl gate (`docs/PRE-IMPL-CHECK.md`) + 8-dimension cost (`docs/COST-ANALYSIS.md`).

```bash
python3 -m pytest tests/ -q          # 255 tests
claude plugin validate .claude-plugin/plugin.json
```

## License

MIT

## Status

🚧 **Phase 1~5 skeleton complete. E2E runtime + A2A + Eval-Repair in progress.**

See [`docs/STAGES.md`](docs/STAGES.md), [`docs/NAMING.md`](docs/NAMING.md), [`CHANGELOG.md`](CHANGELOG.md).
