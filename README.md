# dev-harness-kit

> AI-native unified harness plugin — Plan → Build → Review → Ship with typed
> sub-agent delegation, an Eval-Repair loop, and Human-on-the-Loop supervision.

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Languages:** English · [한국어](README.ko.md)

---

## Table of contents

- [What it is](#what-it-is)
- [Install](#install)
- [Keeping the plugin up to date](#keeping-the-plugin-up-to-date)
- [First-time consumer setup](#first-time-consumer-setup)
- [Command reference](#command-reference)
- [Core concepts](#core-concepts)
  - [Worktree rule](#worktree-rule)
  - [Skills by audience](#skills-by-audience)
- [Tooling](#tooling)
  - [Loghooks](#loghooks-dev-kitlog)
  - [Token efficiency analyzer](#token-efficiency-analyzer)
  - [Cost gate](#cost-gate)
- [Consumer CI install](#consumer-ci-install)
- [Codex CLI compatibility](#codex-cli-compatibility)
- [Agent-behavior eval](#agent-behavior-eval)
- [Repository layout](#repository-layout)
- [Design principles](#design-principles)
- [Contributing](#contributing)

---

## What it is

`dev-harness-kit` ships as a single Claude Code / Codex plugin (`dev-kit`) that
covers the full delivery loop. Highlights:

- **Plan + Design in one command** — `/dev-kit:plan` auto-generates `PRD.md` +
  `phases/<name>/{index.json, step<N>.md}` through a 5-gate loop
  (`frame → validate → non-goals → decompose → emit`) driven by quantified
  ambiguity and value scores rather than a fixed questionnaire.
- **Per-step sub-agent Build** — `/dev-kit:build` delegates each step to a
  sub-agent with an integrated TDD + auto-fix loop.
- **Parallel Review / Security** — `/dev-kit:review` (correctness + security +
  architecture) and `/dev-kit:security` (OWASP A01–A10) fan out to subagents and
  run a verification pass that rejects false positives.
- **Agent-behavior eval** — `/dev-kit:eval` replays recorded transcripts and
  judges them against per-dimension rubrics plus a code-sanity checklist.
- **Eval-Repair loop** — auto-check → specialized fixer → final Human Review.
- **Human-on-the-Loop** — the harness auto-progresses; the user approves last.
- **Worktree enforcement** — hooks block edits in the main checkout and nudge
  every new task onto its own worktree + branch.
- **Consumer install** — `/dev-kit:ci-setup` ships a self-aware CI workflow set
  that works both inside this repo and in downstream consumer repos.
- **Cost visibility** — a token-efficiency dashboard and a live cost gate,
  fed by opt-in session loghooks.

---

## Install

Requires the Claude Code CLI. See [Node compatibility](#node-compatibility)
before running any `claude plugin …` command.

```bash
# Marketplace install (recommended)
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit

# …or from a local checkout
git clone https://github.com/sh-ai-x/dev-harness-kit
claude plugin marketplace add ./dev-harness-kit
claude plugin install dev-kit

# At the start of every session
/reload-plugins
```

The install pins the `version` field from `.claude-plugin/plugin.json`, and the
loaded copy lives in a version-named cache directory
(`~/.claude/plugins/cache/dev-kit/dev-kit/<version>/`). The marketplace source
tracks the `main` branch (`marketplace.json` → `source.ref: main`), so a new
version is available after each merge — see
[Keeping the plugin up to date](#keeping-the-plugin-up-to-date).

### Live-source dev (recommended for contributors)

The marketplace install pins one published version. When you are developing this
repo, point Claude Code at your local checkout instead so edits are live with no
re-install:

```bash
claude --plugin-dir /path/to/dev-harness-kit
```

Save the keystrokes with a shell alias in `~/.zshrc` (or `~/.bashrc`):

```bash
alias claude-dev='claude --plugin-dir /path/to/dev-harness-kit'

claude-dev   # in a project dir: loads your local edits, no rebuild
claude       # falls back to the marketplace-pinned install
```

When both are available, the local `--plugin-dir` copy wins for that session.

> **Don't symlink `~/.claude/skills/dev-kit` to the repo.** A marketplace install
> and a skills-dir plugin sharing the same `name` collide, and the loader rejects
> the second copy. Use the alias above for a no-flag live-source install.

### Node compatibility

The bundled Claude Code CLI crashes on **Node ≥ 25**
(`TypeError: Cannot read properties of undefined (reading 'prototype')` at
`cli.js:384`). Run every `claude plugin …` command on **Node 22**:

```bash
nvm install 22 && nvm use 22
```

The `--plugin-dir` flag is unaffected — it bypasses the failing CLI path
entirely.

---

## Keeping the plugin up to date

A marketplace install loads a cached copy at
`~/.claude/plugins/cache/dev-kit/dev-kit/<version>/`. After a PR merges to
`main`, that cache is stale until refreshed.

**Refresh when:**

- A PR merged to `main` and you want the new behavior in your current session.
- `/dev-kit:*` output no longer matches the latest source.
- A consumer repo's `/dev-kit:ci-setup` reports a missing file (e.g.
  `scripts/branch-policy.sh: No such file or directory`) — the cache is stale.

### Claude Code

The `dev-kit` marketplace entry points at `main`, so after each merge the
marketplace catalog auto-bumps the pinned version. The cleanest path is:

```bash
# Preferred: pull the latest pinned version from the marketplace.
# Works from any shell — and from inside a Claude Code session, where the
# updater path bypasses the CLI bug (see "Node compatibility" above).
claude plugin update dev-kit
```

If that fails (most commonly because you're inside a Claude Code session and
the bundled CLI throws the Node `TypeError`), the maintenance script does the
same job with raw `git pull` + `rsync`:

```bash
# Escape hatch: pull the marketplace clone + rsync into the versioned cache.
bin/devkit-refresh.sh
bin/devkit-refresh.sh --dry-run    # show what would change first
```

> **Why `devkit-refresh.sh` exists:** `claude plugin install --force` and
> `claude plugin update` both hit the same CLI path that throws the Node
> `TypeError` above when invoked *from inside* a Claude Code session. The script
> does the same job with plain `git pull` + `rsync`, which works everywhere. It
> reads the cache version from `plugin.json` (falling back to the marketplace
> clone's short SHA if the field is absent) and preserves executable bits on
> shipped hook/template scripts.

If even that is unavailable, you can refresh the cache by hand:

```bash
cd ~/.claude/plugins/marketplaces/dev-kit && git pull origin main --ff-only
rsync -a --delete --exclude=.git \
  ~/.claude/plugins/marketplaces/dev-kit/ \
  ~/.claude/plugins/cache/dev-kit/dev-kit/<version>/
```

### Codex

```bash
bash skills/codex-cache-update/scripts/update.sh
bash skills/codex-cache-update/scripts/update.sh --dry-run   # inspect only
```

It upgrades the Codex marketplace checkout and synchronizes the matching
versioned cache — even when the marketplace command reports it is already
current — then prints the marketplace path, manifest version, cache path, and a
final `cache synchronized` line. Override paths for a non-default install:

```bash
CODEX_MARKETPLACE_DIR="$HOME/.codex/.tmp/marketplaces/dev-kit" \
CODEX_CACHE_ROOT="$HOME/.codex/plugins/cache/dev-kit/dev-kit" \
bash skills/codex-cache-update/scripts/update.sh
```

After any refresh, restart the client or run `/reload-plugins` where supported.

---

## First-time consumer setup

Most users are consumers. End-to-end "I have a new repo" flow:

```bash
# 1. Create + clone
gh repo create myorg/myrepo --private --clone && cd myrepo

# 2. Install the plugin
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit
# (live source: claude --plugin-dir /path/to/dev-harness-kit)

# 3. One-shot setup: CLAUDE.md + AGENTS.md + active-hooks.json + CI templates
/dev-kit:bootstrap-full
#    = /dev-kit:bootstrap then /dev-kit:ci-setup --force.
#    Run them separately if you only want one half.

# 4. First commit + push
git add -A && git commit -m "chore: bootstrap dev-kit"
git push -u origin main
```

**Use `--force` on first install.** On a fresh repo the result is identical to a
default install (all files copy either way), but `--force` is robust against a
partial previous attempt and a stale plugin cache. Re-run with `--force` later to
pull upstream template changes — see
[Consumer CI install](#consumer-ci-install) for refresh vs first-install
semantics.

Typical next step: `/dev-kit:plan` to generate the PRD and phases.

---

## Command reference

Invoke with `/dev-kit:<skill>`. This list groups the user-facing entry points by
workflow stage; only skills with `user-invocable: true` in their `SKILL.md`
appear in slash autocomplete. Inspect that frontmatter (or use autocomplete) for
the authoritative, current surface — see [Skills by audience](#skills-by-audience).

**Setup**

| Command | Purpose |
|---|---|
| `/dev-kit:bootstrap` | First entry — generate `CLAUDE.md` |
| `/dev-kit:bootstrap-full` | One-shot bootstrap + ci-setup (new-project default) |
| `/dev-kit:ci-setup` | Install CI templates (workflows + hooks + scripts + worktree files) |
| `/dev-kit:ci-doctor` | Read-only PASS/FAIL audit of CI readiness |
| `/dev-kit:log setup\|on\|off\|status` | Toggle session loghooks per project |
| `/dev-kit:config` | Skill / MCP / hook / methodology picker |

**Plan → Build**

| Command | Purpose |
|---|---|
| `/dev-kit:plan` | PRD + phases (Plan + Design unified) |
| `/dev-kit:build` | Run per-step sub-agents |
| `/dev-kit:adapt` | Mid-build plan/spec amendment |
| `/dev-kit:feat-add` | Add a feature under TDD |
| `/dev-kit:feat-fix` | Reproduce-first fix for one named feature |
| `/dev-kit:feat-revise` | Revise a feature under TDD |
| `/dev-kit:feat-remove` | Remove a feature (call-graph sweep + deletion report) |

**Review → Ship**

| Command | Purpose |
|---|---|
| `/dev-kit:review` | 3-dim review (correctness + security + architecture) |
| `/dev-kit:security` | OWASP A01–A10 audit |
| `/dev-kit:audit` | Batch slop + secret audit |
| `/dev-kit:inspect` | 8-dim code-health audit (read-only) |
| `/dev-kit:refactor` | 3-phase refactor: inspect → cleanup → review |
| `/dev-kit:prune` | 3-phase deletion sweep: inspect → delete → review |
| `/dev-kit:babysit-pr` | PR babysitter loop (poll CI, fix, re-iterate) |
| `/dev-kit:ship` | Release tag |
| `/dev-kit:bump [major\|minor\|patch]` | Explicit version bump + push |

**Eval / cost / reporting**

| Command | Purpose |
|---|---|
| `/dev-kit:eval` | Agent-behavior eval (review/security/plan + code-sanity) |
| `/dev-kit:repair approve\|reject\|defer <asset>` | Eval-Repair Human Review |
| `/dev-kit:report` | HTML viewer for eval + inspect reports |
| `/dev-kit:token-analyzer` | Token-efficiency dashboard from session logs |
| `/dev-kit:cost-gate` | Live cost gate (spend + threshold + commit footer) |

**Docs / shortcuts**

| Command | Purpose |
|---|---|
| `/dev-kit:docs-maintenance` | Audit stale docs, refresh README, drop volatile facts |
| `/dev-kit:tdd-fast` | Skip Bootstrap + Plan → straight to Build (hotfix) |
| `/dev-kit:shortcut-quick-fix` | Verify + debug on demand |

---

## Core concepts

### Worktree rule

The canonical rule is `rules/git-workflow.md`. Claude Code discovers it through
the `.claude/rules` compatibility symlink; Codex reads the same file through
`AGENTS.md`. The requirement is hard:

> **Every task = new worktree + client handoff + new branch.** Claude Code opens
> a new session in the worktree; Codex spawns a subagent there. No edits on the
> previous task's branch or in the main checkout.

Enforced by three hooks:

- `worktree-guard.sh` — hard-blocks any Edit/Write in the main checkout.
- `task-detector.sh` — early warning on new-task prompts ("implement X", …).
- `session-start-check.sh` — gentle reminder at session start.

The canonical worktree path is the client-neutral `.worktrees/<slug>/` at the
repo root, so Claude Code and Codex open the same checkout for a branch. Legacy
`.claude/worktrees/` and `.codex/worktrees/` checkouts stay discoverable for log
analysis, but new automatic cuts use `.worktrees/`. These worktree-rule files
also ship to consumer repos via `templates/ci/`.

### Skills by audience

Each `SKILL.md` carries a `user-invocable` frontmatter flag:

- **`user-invocable: true`** (or unset) — surfaces in `/dev-kit:` autocomplete.
  *You* type it.
- **`user-invocable: false`** — hidden. *Claude* auto-invokes it as a sub-step
  when its parent skill runs.

If a skill name doesn't autocomplete, it's an internal sub-skill — type the
user-facing parent instead (e.g. `/dev-kit:refactor`, not
`/dev-kit:build-refactor`). Mental model: user-facing skills are the verbs (the
*what*); internal skills are the machinery (the *how*). This README does not
duplicate a changing skill inventory — inspect the `skills/` frontmatter or use
autocomplete for the live surface.

---

## Tooling

### Loghooks (`/dev-kit:log`)

Wraps the standalone [`loghooks`](https://github.com/sh-ai-x/loghooks) repo
(Claude Code `Stop` + `SessionEnd`, plus Codex equivalents) as a one-command
on/off toggle per project.

```bash
/dev-kit:log setup   # copy tools/save_log.py + scaffold logs/{claude-code,codex}/
/dev-kit:log on      # merge hooks into .claude/settings.json + .codex/hooks.json
/dev-kit:log status  # managed=N captured=N
/dev-kit:log off     # strip sentinel-tagged entries; scaffold left in place
```

Every installed entry carries `_loghooks_managed=true`; `off` strips only those,
so pre-existing user hooks survive. Captured transcripts land in
`logs/<tool>/<branch>/<sid>.jsonl` (grouped by `gitBranch`) and are gitignored.
See [`logs/README.md`](logs/README.md) and `skills/log/SKILL.md`.

### Token efficiency analyzer

A stdlib-only Python CLI (`tools/token_efficiency_analyzer.py`) that consumes the
`logs/{claude-code,codex}/**/*.jsonl` transcripts captured by loghooks and emits
one self-contained HTML dashboard — no dependencies, no JavaScript, no network.
The user-facing entry point is the `/dev-kit:token-analyzer` skill; the CLI is
also directly invokable for CI use:

```bash
python3 tools/token_efficiency_analyzer.py --repo "my-project" --days 30
open token-dashboard-my-project-30d.html
```

The dashboard answers three questions per repo over the last N days:

1. **Where is the spend going?** Per-repo, per-tool (flags `Read`-heavy), and
   per-session cost share.
2. **How efficient is each session?** A 0–100 score across four dimensions.
3. **What should I fix?** Six anti-pattern warnings + a USD savings estimate.

**Common flags**

| Flag | Default | Purpose |
|---|---|---|
| `--repo <name>` | (required) | Matches each session's `Path(cwd).name` (repo dir basename) |
| `--days <n>` | `30` | Look-back window; older sessions are dropped |
| `--logs-dir <path>` | `./logs` | Root for `claude-code/` and `codex/` subdirs |
| `--out <path>` | `token-dashboard-<repo>-<days>d.html` | Output HTML path |

**Scoring dimensions** (weighted to 100)

| Dimension | Weight | Penalizes |
|---|---:|---|
| Cache Utilization | 0.35 | Prefix misalignment that re-primes the full prompt |
| Output Density | 0.25 | Reading a lot and shipping little |
| Read Redundancy | 0.20 | Re-reading the same file (missing cartography) |
| Tool Economy | 0.20 | Many tool calls for thin output |

**Warning triggers**

| Code | Condition |
|---|---|
| `CACHE_HIT_LOW` | `cache_hit_ratio < 50%` |
| `READ_HEAVY` | `Read` ≥ 40% of total tool cost |
| `HEAVY_CONTEXT` | `total_input > 500K` tokens in one session |
| `MODEL_OVERSPEC` | Opus + density score < 20 |
| `WRITE_NOT_REUSED` | `cache_write > 50K` and `cache_read < 2 × cache_write` |
| `REPEATED_USER_MSG` | Any user message text appears ≥ 2× |

Per-model pricing is baked into the script (`opus` / `sonnet` / `haiku`, matched
by model-id substring; unknown ids fall back to Sonnet). Override `PRICING` at
the top of the file for contracted rates. Savings are a conservative reclaim
model — cache-miss penalty + duplicate-read waste only, not the whole bill. The
per-tool cost column is an imputed heuristic (`n_calls × 2K tokens × input
price`), since Anthropic billing doesn't break out per-tool spend.

**Why a tool, not a skill:** it transforms local files with no LLM in the loop —
wrapping it as a skill would force a needless model round-trip. The loghooks that
*produce* the input stay a skill; the analyzer that *consumes* it is a script.

Verify against the synthetic fixtures:

```bash
python3 fixtures/make_fixture.py
python3 tools/token_efficiency_analyzer.py --repo "fixture-repo" --days 30 \
  --logs-dir fixtures/logs --out fixtures/out/dashboard.html
```

### Cost gate

A **read-only** cost layer, distinct from the post-hoc token dashboard:
cost-gate prints the running ledger on demand and emits the trailer block the PR
aggregator needs; the analyzer replays historical sessions. State lives at
`<cwd>/.dev-kit/.cost-gate/state.json`. **The gate is observed only — it never
blocks a tool call.**

| Layer | Trigger | Default threshold | Behavior |
|---|---|---:|---|
| Session warn | `/dev-kit:cost-gate` | `$5.00` | One-screen `ok`/`warn` status; never a deny |
| PR flag | PR opened/synchronize/reopened | `$20.00` | Applies `cost-flag` label + upserts one comment |

Override via `DEV_KIT_COST_WARN_USD` and `DEV_KIT_PR_COST_FLAG_USD`. Emit the
commit trailer the PR aggregator (`.github/workflows/cost-flag.yml`) reads:

```bash
git commit -m "feat: thing" -m "$(python3 tools/cost_gate_status.py --footer)"
```

`lib/cost_gate.py` and `tools/cost_gate_status.py` keep pricing tables, state
files, and transcript scanners fully independent of the analyzer — a regression
test asserts no cross-import.

---

## Consumer CI install

`/dev-kit:ci-setup` is what makes dev-kit work in *other* repos. It copies:

- GitHub Actions workflows (ci, auto-fix-pr, review)
- scripts (validate, test, branch-policy, ci-local)
- a pre-push hook
- worktree-rule files (hooks, lib, rule, tests)

The shipped `review.yml` is **self-aware**: it detects whether the checkout is
the dev-kit plugin itself (self-install) or a plain consumer repo (clones from
public source), so one workflow file works in both contexts.

**Switching the CI review provider:** run `bin/set-provider.sh <provider>`. It
edits the git-tracked `.github/ci-review-provider.txt`, prints the diff, then you
commit + push. `.env` is **not** consulted (a GitHub-hosted runner can't read it
anyway). Each provider needs its matching repo secret (`MINIMAX_API_KEY`,
`ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY`) pushed via `gh secret set` first. A
PR that itself edits `.github/workflows/review.yml` is skipped by
`claude-code-action`'s anti-tampering guard — expected, and it resolves once the
PR merges.

### `--force`: when and when not

`ci-setup` is **idempotent by default** — the marker `.dev-kit/ci-config.json`
records install time + content hashes, so a matching re-run is a no-op. `--force`
overwrites the expected files regardless.

**Use `--force`** for a first install, to pull a newly added or fixed template,
or when you suspect a stale/partial install (marker present but a file missing or
drifted). **Avoid `--force`** on a clean re-run with no upstream changes, or when
you've hand-edited installed files (it overwrites local customizations — review
the diff first).

```bash
bin/devkit-refresh.sh                         # 1. refresh cache → latest templates
cd /path/to/consumer-repo
/dev-kit:ci-setup --force                      # 2. install
git diff .github/ scripts/ .githooks/ hooks/ .claude/ tests/   # 3. review the diff
/dev-kit:ci-doctor                             # 4. verify readiness (repeat until PASS)
git add -A && git commit -m "chore(ci): refresh dev-kit templates"   # 5. commit
```

---

## Codex CLI compatibility

Codex CLI's plugin format ([openai/plugins](https://github.com/openai/plugins))
is a `.codex-plugin/plugin.json` manifest with a `"skills"` field pointing at the
skills directory and a `"hooks"` field pointing at the bundled
`.codex-plugin/hooks/hooks.json`. That bundled copy mirrors the canonical
`hooks/hooks.json` (Codex requires plugin hook files inside the plugin root); a
regression test keeps the two event inventories synchronized. Codex commands use
`${PLUGIN_ROOT}`; Claude Code uses `${CLAUDE_PLUGIN_ROOT}` and keeps loading
`hooks/hooks.json` directly.

After enabling the plugin, review and trust its hooks with `/hooks` in Codex —
new or changed non-managed hooks are skipped until trusted. Check local status:

```bash
python3 bin/dev-kit-hooks-status.py          # human-readable
python3 bin/dev-kit-hooks-status.py --json    # machine-readable
```

The report distinguishes Claude Code registration, Codex registration + trust,
the `.dev-kit/.active-hooks.json` matrix, and Git's separate pre-push hook. Git
enforcement is active only after you opt in:

```bash
git config core.hooksPath .githooks
```

### Hook inventory

| Hook | Event | Purpose | Mode |
|---|---|---|---|
| `tdd-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | TDD test-first enforcement | advisory / `--strict` |
| `bash-guard.sh` | PreToolUse (Bash) | Block destructive commands | advisory / `--strict` |
| `git-guard.sh` | PreToolUse (Bash) | Branch strategy enforcement | hard-block |
| `worktree-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | Block edits in main checkout | hard-block |
| `task-detector.sh` | UserPromptSubmit | Nudge new tasks to a worktree | advisory |
| `session-start-check.sh` | SessionStart | Remind about the worktree rule | advisory |
| `secret-scan.sh` | PostToolUse (Write\|Edit) | Detect credentials in edits | hard-block |
| `slop-detector.sh` | PostToolUse (Write\|Edit) | Block AI slop (phrase + structure + scoring, KO+EN) | advisory (opt-in strict) |
| `stop-verify.sh` | Stop | Run regression tests on session end | hard-block |

---

## Agent-behavior eval

`/dev-kit:eval` measures whether the **agent produces the right output for the
right input** when running the dev-kit skills. The unit is a *case fixture + a
recorded transcript → per-dimension rubric judgment*. Replay-only in v1: a case
without a recorded transcript is `SKIPPED` (a setup gap, not a regression).

**Three dimensions** (each axis 0–10):

| Dim | Axes | Measures |
|---|---|---|
| `review` | verdict consistency · severity calibration · precision · recall · code-sanity | review verdict + findings quality |
| `security` | OWASP classification · severity accuracy · precision | A01–A10 mapping + false-positive rate |
| `plan` | spec clarity · step atomicity · AC executability · dependency ordering | atomic, runnable, buildable plans |

Per-case axis mean → verdict: **OK** ≥ 8.0 · **DRIFT_WARNING** 5.0–7.9 · **ROT**
< 5.0 · **SKIPPED** (no transcript). The `review` dim embeds a 20-checkbox
code-sanity rubric (clean-code + over-engineering + value/meaning), frozen in
`ADR-0022`.

```bash
# Full eval → .dev-kit/eval-report.md
python lib/eval_runner.py --project-root . [--dry-run]
python lib/eval_runner.py --project-root . --dim plan
python lib/eval_runner.py --project-root . --case review-04-factory-one-impl
```

`--dry-run` skips LLM calls (mocks each case at 7.0/DRIFT_WARNING) — useful in CI
without an API key. Adding a case requires no code change: drop a case JSON in
`eval/cases/<dim>/` and a transcript in `eval/transcripts/<dim>/`, then re-run.
See `docs/adr/ADR-0022-eval-agent-behavior.md` for the full rationale.

---

## Repository layout

```
dev-harness-kit/
├── .claude-plugin/   # marketplace.json + plugin.json (Claude Code manifest)
├── .codex-plugin/    # plugin.json + bundled hooks (Codex manifest)
├── skills/           # flat: skills/<skill-name>/SKILL.md
├── hooks/            # hook scripts + lib/ + hooks.json
├── lib/              # Python engine (state, execute, ci_setup, eval, cost_gate, …)
├── bin/              # devkit-refresh.sh + dev-kit-hooks-status.py + dev-kit-report.py
├── tools/            # save_log.py + token_efficiency_analyzer.py + cost_gate_status.py
├── templates/ci/     # CI templates shipped to consumer repos
├── tests/            # pytest suite
├── eval/             # cases/ + transcripts/ + prompts/ + golden/
├── docs/             # STAGES, NAMING, COST-ANALYSIS, adr/, …
├── rules/            # shared canonical rules (git-workflow, session-hygiene, …)
└── CLAUDE.md         # SSOT (auto-generated by /dev-kit:bootstrap)
```

---

## Design principles

- **NO-DUP** — Iron Laws live in one place (`CLAUDE.md §1`), enforced by hook +
  skill.
- **NO-BOTTLENECK** — 0-arg UX, lazy `CLAUDE.md`, parallel sub-agents.
- **NO-MEANINGLESS-LOOP** — explicit loop semantics + auto-STOP + user interrupt.
- **Human-on-the-Loop** — auto-progress with the user as supervisor and a 1×
  interrupt.
- **Methodology extension** — TDD / SDD / DDD / BDD / FDD selectable.
- **A2A typed** — sub-agent ↔ main communication via a JSON-Schema SSOT.
- **Plugin-only** — the plugin manifest is the single source of truth.
- **Worktree-per-task** — enforced by hooks, documented in `rules/git-workflow.md`.
- **Consumer-install** — one self-aware workflow set works in this repo and in
  consumer repos.

See `docs/adr/` for the full ADR series.

---

## Contributing

Pass the pre-impl gate (`docs/PRE-IMPL-CHECK.md`) and the 8-dimension cost check
(`docs/COST-ANALYSIS.md`), then:

```bash
python3 -m pytest tests/ -q
claude plugin validate .claude-plugin/plugin.json
```

Reference docs: [`docs/STAGES.md`](docs/STAGES.md),
[`docs/NAMING.md`](docs/NAMING.md), [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT
