# dev-harness-kit

> AI-native unified harness plugin — absorbs 5 repos + A2A typed + Eval-Repair loop + Human-on-the-Loop.

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

## What

- **Plan+Design automation**: One `/dev-kit:plan` command auto-generates PRD.md + `phases/<name>/{index.json, step<N>.md}`. 5 gates in 1 Ralph loop: `frame` → `validate` (evidence + value_score + ambiguity loop) → `non-goals` → `decompose` → `emit`. The old 5-question grill-me is replaced by a quantified loop (ambiguity 0-10, target ≤ 3; value_score = LTV × reachable / cost, target ≥ 3.0).
- **Per-step sub-agent delegation in Build**: harness-runner engine + TDD + auto-fix loop.
- **Review/Security fan-out**: 3-dim (correctness + security + architecture) + 10-dim OWASP A01–A10, parallel in a single message.
- **Agent-behavior eval** (`/dev-kit:eval`): replays recorded agent transcripts and judges them against per-dim rubrics (review / security / plan) plus a 20-checkbox code-sanity rubric (clean-code + over-engineering + value/meaning). Seed cases in the box.
- **Eval-Repair 8-step loop**: auto-check + Specialized Fixer + final = Human Review.
- **Human-on-the-Loop auto + user approves last**: zero response fatigue.
- **Worktree enforcement**: `worktree-guard` blocks Edit/Write in the main checkout; `task-detector` nudges new tasks to a worktree; `session-start-check` reminds at session start. See `.claude/rules/git-workflow.md`.
- **Consumer-install**: `/dev-kit:ci-setup` ships a self-aware `review.yml` that works in both the dev-harness-kit repo itself (self-install) and consumer repos (clones from public source).
- **Token efficiency analyzer (`tools/token_efficiency_analyzer.py`)**: stdlib-only CLI that turns `logs/{claude-code,codex}/**/*.jsonl` (per-branch layout: `<tool>/<branch>/<session>.jsonl`) into a single self-contained HTML dashboard — 4-dim session scoring (cache utilization · output density · read redundancy · tool economy), 6 anti-pattern warnings (prefix misalignment, Read-heavy cartography failure, heavy context, model overspec, cache write-not-reused, repeated user-message injection), a USD savings estimate, and a "Cost by Branch" panel. See [Token efficiency analyzer](#token-efficiency-analyzer-toolstoken_efficiency_analyzerpy).

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

**Claude Code refresh paths:**

```bash
# 1. devkit-refresh.sh (RECOMMENDED for maintainers + power users)
#    git pull marketplace clone + rsync to cache.
#    Bypasses the `claude plugin install` Node TypeError bug.
bin/devkit-refresh.sh

# 2. claude plugin update (uses marketplace catalog auto-pin bump)
#    Requires Node 22 (see CLI Node compatibility above).
claude plugin update dev-kit

# 2. Manual rsync (escape hatch when the updater fails)
cd ~/.claude/plugins/marketplaces/dev-kit && git pull origin main --ff-only
rsync -a --delete --exclude=.git \
  ~/.claude/plugins/marketplaces/dev-kit/ \
  ~/.claude/plugins/cache/dev-kit/dev-kit/<marketplace-commit>/
```

For Codex, use the bundled updater from the repository root. It upgrades the
marketplace checkout and synchronizes the matching versioned cache, including
when the marketplace command reports that it is already current:

```bash
bash skills/codex-cache-update/scripts/update.sh

# Inspect differences without changing the cache
bash skills/codex-cache-update/scripts/update.sh --dry-run
```

The updater prints the marketplace path, manifest version, cache path, and a
final `cache synchronized` line. Override paths when using a non-default Codex
installation:

```bash
CODEX_MARKETPLACE_DIR="$HOME/.codex/.tmp/marketplaces/dev-kit" \
CODEX_CACHE_ROOT="$HOME/.codex/plugins/cache/dev-kit/dev-kit" \
bash skills/codex-cache-update/scripts/update.sh
```

After a refresh, restart the client or run `/reload-plugins` where supported so
the new plugin files are loaded.

**Why `devkit-refresh.sh` exists:** `claude plugin install dev-kit --force` and `claude plugin update dev-kit` both invoke the same CLI path that throws `TypeError: Cannot read properties of undefined (reading 'prototype')` from `cli.js:384` when called from inside a Claude Code session. A SessionStart hook that auto-ran the install was tried (PR #24) and reverted (PR #26) for the same reason. `devkit-refresh.sh` uses raw `git pull` + `rsync`, which works in every environment.

## First-time consumer setup

Most users are consumers. The end-to-end "I have a new repo" flow:

```bash
# 1. Create + clone
gh repo create myorg/myrepo --private --clone && cd myrepo

# 2. Install the dev-kit plugin
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit
# (live source: claude --plugin-dir /path/to/dev-harness-kit)

# 3. One-shot setup (CLAUDE.md + AGENTS.md + active-hooks.json + CI templates + marker)
/dev-kit:bootstrap-full
#    Equivalent to running `/dev-kit:bootstrap` then `/dev-kit:ci-setup --force` in sequence.
#    Use the two separately when you only want one half.

# 4. First commit + push
git add -A
git commit -m "chore: bootstrap dev-kit"
git push -u origin main
```

**Use `--force` on first install** — for consumers (the dominant case), `--force` is the safe standard. On a truly fresh repo the result is identical to default install (all files copy either way), but `--force` is robust against partial installs from a previous attempt and is robust to a stale plugin cache. Re-run with `--force` later to pull in upstream template changes. See the [Consumer-install deep-dive](#consumer-install-via-dev-kitci-setup) for refresh vs first-install semantics.

Typical next step: `/dev-kit:plan` for PRD + phases auto.

## Usage (0-arg, namespaced)

```
/dev-kit:bootstrap              # first entry (auto-generate CLAUDE.md)
/dev-kit:ci-setup                # install dev-kit CI templates (workflows + hooks + scripts + worktree-rule files) into target repo
/dev-kit:ci-doctor               # read-only PASS/FAIL audit of CI readiness after /dev-kit:ci-setup (verifies workflows, marker, provider file, secrets, gh auth)
/dev-kit:bootstrap-full          # one-shot: bootstrap + ci-setup in a single call (new-project default)
/dev-kit:log setup|on|off|status # toggle loghooks (Stop/SessionEnd transcripts) per project
/dev-kit:plan                    # PRD + phases auto (Plan+Design unified)
/dev-kit:build                   # run per-step sub-agents
/dev-kit:adapt                    # mid-build plan/spec amendment (pauses current step, diffs PRD, proposes minimal patch)
/dev-kit:feat-add                 # add a new feature under TDD
/dev-kit:feat-fix                 # reproduce-first fix for a single named feature
/dev-kit:feat-remove              # safely remove a feature (call-graph sweep + deletion report)
/dev-kit:feat-revise              # revise a feature under TDD
/dev-kit:review                  # 3-dim code review (correctness + security + architecture)
/dev-kit:security                # 10-dim OWASP A01–A10
/dev-kit:audit                   # batch slop + secret audit
/dev-kit:inspect                 # 8-dim code health audit (read-only, project-wide)
/dev-kit:eval                    # agent-behavior eval (review/security/plan + code-sanity rubric)
/dev-kit:report                  # HTML viewer for eval + inspect markdown reports
/dev-kit:token-analyzer           # token-efficiency dashboard from logs/{claude-code,codex}/**/*.jsonl
/dev-kit:repair approve|reject|defer <asset>   # Eval-Repair Human Review
/dev-kit:ship                    # release tag
/dev-kit:babysit-pr              # 0-arg PR babysitter loop
/dev-kit:refactor               # 3-phase refactor: inspect -> 4-pass cleanup -> review (rewrites code)
/dev-kit:prune                  # 3-phase deletion sweep: inspect -> 3-pass deletion -> review (deletes slop/dead features)
/dev-kit:cost-gate               # live cost gate (current spend + threshold distance + commit footer)
/dev-kit:bump [major|minor|patch] # explicit version bump + push of chore/bump-vX.Y.Z (race recovery / pre-PR)

# Documentation and maintenance
/dev-kit:docs-maintenance       # audit stale docs, refresh README, remove volatile facts

# Shortcuts (urgent hotfix)
/dev-kit:tdd-fast                # skip Bootstrap+Plan → straight to Build
/dev-kit:shortcut-quick-fix      # verify+debug on demand
```

Invoke with `/<skill-name>` or `dev-kit:<skill-name>`.

## Directory layout

```
dev-harness-kit/
├── .claude-plugin/        # marketplace.json (source: url object) + plugin.json
├── skills/                # flat: skills/<skill-name>/SKILL.md
├── .codex-plugin/         # plugin.json pointing Codex CLI at the same ./skills/ (no copy)
├── hooks/                 # hook scripts + lib/ + hooks.json
├── lib/                   # state_codec / active_hooks_codec / write_project_md / execute / methodology/ / ci_setup / cost_gate / atomic / eval_runner / llm_judge / render_report_html / install.sh
├── bin/                   # devkit-refresh.sh + dev-kit-hooks-status.py + dev-kit-report.py
├── tools/                 # save_log.py (Stop-hook transcript saver) + token_efficiency_analyzer.py (HTML dashboard) + cost_gate_status.py (live cost CLI + hook driver)
├── templates/ci/          # CI workflow templates shipped to consumer repos via /dev-kit:ci-setup
├── tests/                 # pytest suite
├── eval/                  # cases/ + fixtures/ + transcripts/ + prompts/ + golden/ (agent-behavior eval)
├── docs/                  # STAGES, NAMING, COST-ANALYSIS, PRE-IMPL-CHECK, ci-setup, auto-fix, team-adoption, adr/, rules/, screenshots/
├── rules/                 # shared canonical rules (git-workflow, session-hygiene, skill-authoring, test-files, token-pricing)
└── CLAUDE.md              # SSOT (auto-generated by /dev-kit:bootstrap)
```

### Hook scripts

| Hook | Event | Purpose | Mode |
|---|---|---|---|
| `tdd-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | TDD test-first enforcement | advisory / `--strict` |
| `bash-guard.sh` | PreToolUse (Bash) | Block destructive commands | advisory / `--strict` |
| `git-guard.sh` | PreToolUse (Bash) | Branch strategy enforcement | hard-block |
| `worktree-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | **Block edits in main checkout** | hard-block |
| `task-detector.sh` | UserPromptSubmit | Nudge new tasks to a worktree | advisory |
| `session-start-check.sh` | SessionStart | Remind about worktree rule at session start | advisory |
| `secret-scan.sh` | PostToolUse (Write\|Edit) | Detect credentials in edits | hard-block |
| `slop-detector.sh` | PostToolUse (Write\|Edit) | Block AI slop in edits (T1 phrase + T2 structure + multi-dim scoring, KO+EN patterns) | advisory (opt-in strict) |
| `stop-verify.sh` | Stop | Run regression tests on session end | hard-block |

The 3 new hooks (worktree-guard, task-detector, session-start-check) implement the worktree enforcement rule. The worktree-rule scripts (hooks + `lib/worktree-detect.sh`) also ship to consumer repos via `templates/ci/`.

Skill metadata lives in each `skills/<skill-name>/SKILL.md` frontmatter. Use
slash autocomplete or inspect that directory when you need the current
inventory; the README intentionally does not duplicate a changing skill list.

## Worktree rule

The canonical shared rule is `rules/git-workflow.md`. Claude Code discovers it
through the `.claude/rules` compatibility symlink, while Codex reads the same
file through `AGENTS.md`. The rule makes this a hard requirement:

> **Every task = new worktree + client handoff + new branch.** Claude Code opens a new session in the worktree; Codex spawns a subagent there. No edits on the previous task's branch or in the main checkout.

Enforced by 3 hooks:
- `worktree-guard.sh` — hard-block any Edit/Write in the main checkout
- `task-detector.sh` — early warning on new-task prompts ("implement X", "add Y", etc.)
- `session-start-check.sh` — gentle reminder at session start

The canonical worktree location is the client-neutral repository-root path
`.worktrees/<slug>/`. Claude Code and Codex therefore open the same checkout
for a branch. Existing `.claude/worktrees/` and `.codex/worktrees/` checkouts
remain discoverable for log analysis during migration, but new automatic cuts
must use `.worktrees/`.

Plus `bin/devkit-refresh.sh` for the consumer side (refresh cache after PR merge via `git pull` + `rsync`).

## Loghooks (`/dev-kit:log`)

Wrap the standalone [`~/dev/loghooks`](https://github.com/sh-ai-x/loghooks) repo (Claude Code `Stop` + `SessionEnd` + Codex equivalents) as a one-command on/off per project.

```bash
/dev-kit:log setup   # copy tools/save_log.py + scaffold logs/{claude-code,codex}/
/dev-kit:log on      # merge hooks into .claude/settings.json + .codex/hooks.json
/dev-kit:log status  # managed=N captured=N
/dev-kit:log off     # strip sentinel-tagged entries; scaffold left in place
```

Every installed entry carries `_loghooks_managed=true`. `off` strips only those — pre-existing user hooks are always preserved. Captured transcripts land in `logs/<tool>/<branch>/<sid>.jsonl` (grouped by `gitBranch`) and are gitignored (see [`logs/README.md`](logs/README.md)). See `skills/log/SKILL.md` for the full contract.

## Token efficiency analyzer (`/dev-kit:token-analyzer` → `tools/token_efficiency_analyzer.py`)

A standalone stdlib-only Python CLI that consumes the `logs/{claude-code,codex}/*.jsonl` transcripts captured by the loghooks above and emits a single self-contained HTML dashboard. No external dependencies, no JavaScript, no network — the output is one HTML file with inline CSS that opens directly in a browser.

The user-facing entry point is the **`/dev-kit:token-analyzer`** skill (in the `audit` category) which invokes the underlying `tools/token_efficiency_analyzer.py` driver — see [`skills/token-analyzer/SKILL.md`](skills/token-analyzer/SKILL.md) for the skill contract. The CLI itself is also invokable directly for scripted / CI use:

```bash
python3 tools/token_efficiency_analyzer.py --repo "my-project" --days 30
```

The dashboard answers three questions for any given repository over the last N days:

1. **Where is the spend going?** Per-repo cost share, per-tool cost share (with a flag if `Read` dominates), per-session breakdown.
2. **How efficient is each session?** A 0-100 score across 4 dimensions.
3. **What should I fix?** Six anti-pattern warnings + a USD estimate of recoverable spend.

### Quick start

```bash
# 1. Make sure loghooks are running (see Loghooks section above)
#    -> produces logs/claude-code/<branch>/<session-id>.jsonl
# 2. Generate the dashboard for one repository, last 30 days
python3 tools/token_efficiency_analyzer.py --repo "my-project" --days 30
# 3. Open the output file
open token-dashboard-my-project-30d.html
```

### Preview

![Token efficiency dashboard — dev-harness-kit, last 30 days](docs/screenshots/token-dashboard-dev-harness-kit-30d.png)

*The dashboard above was generated against this repo's own `logs/claude-code/` transcripts — Cost Gate banner, 4-tile overview, per-repo / per-tool / per-branch / per-worktree cost distribution, model + cache TTL mix, session table with letter-grade scores + warning chips, and a reclaim-axis savings breakdown.*

Common flags:

| Flag | Default | Purpose |
|---|---|---|
| `--repo <name>` | (required) | Matches `Path(cwd).name` of each session — i.e. the repo's directory basename |
| `--days <n>` | `30` | Look-back window; sessions with `last_ts` older are dropped |
| `--logs-dir <path>` | `./logs` | Root for `claude-code/` and `codex/` subdirs |
| `--out <path>` | `token-dashboard-<repo>-<days>d.html` | Output HTML path |

### Pricing model

Per-model USD pricing baked into the script (matched by model id substring — `opus` / `sonnet` / `haiku`):

| Model | Input $/M | Output $/M | Cache Write $/M | Cache Read $/M |
|---|---:|---:|---:|---:|
| Opus   | 15.00 | 75.00 | 18.75 (1.25×) | 1.50 (0.10×) |
| Sonnet |  3.00 | 15.00 |  3.75 (1.25×) | 0.30 (0.10×) |
| Haiku  |  0.80 |  4.00 |  1.00 (1.25×) | 0.08 (0.10×) |

The 1.25× cache-write surcharge reflects the priming premium on the first turn; the 0.10× cache-read rate reflects that cached tokens cost ~10% of base input. Unrecognized model ids fall back to Sonnet pricing. Override `PRICING` at the top of the file to match your contracted rates.

### Scoring rubric (per session, 0-100 weighted)

The script computes 4 orthogonal dimensions and a weighted total:

| Dimension | Weight | Formula | What it penalizes |
|---|---:|---|---|
| **Cache Utilization** | 0.35 | `cache_read / (input + cache_read) × 100` | Prefix misalignment — every missed cache re-primes the full prompt at full input price |
| **Output Density** | 0.25 | `min(100, output / total_input × 400)` | Sessions that read a lot and ship nothing |
| **Read Redundancy** | 0.20 | `max(0, 100 − (max_repeat_reads − 1) × 12.5)` | Re-reading the same file — a missing cartography |
| **Tool Economy** | 0.20 | `max(0, 100 − tools_per_1k_out × 2)` | Tool thrashing — many calls for thin output |

Total = `0.35×cache + 0.25×density + 0.20×redundancy + 0.20×economy`, rounded to 1 decimal.

### Warning triggers (6 anti-patterns)

Each trigger has a specific condition and the exact recommended fix from the prompt — the message is rendered verbatim in the dashboard:

| Code | Condition | Why it fires |
|---|---|---|
| `CACHE_HIT_LOW` | `cache_hit_ratio < 50%` | A single token shift in the prefix (date, model change, CLAUDE.md edit) invalidates the entire cache. Move volatile data to the tail; do not switch models mid-session. |
| `READ_HEAVY` | `Read` tool cost ≥ 40% of total tool cost | Large files are being re-read repeatedly. Pin them once in cache; build a cartography so the agent finds the entry point without exploration. |
| `HEAVY_CONTEXT` | `total_input > 500K tokens` in one session | Long sessions accumulate context. Delegate exploration to sub-agents and pass summaries back; run `/compact` at sensible checkpoints. |
| `MODEL_OVERSPEC` | Opus + density score < 20 | Typo fixes and trivial logic don't need Opus. Match the model to the task: Sonnet for code, Haiku for routine work. |
| `WRITE_NOT_REUSED` | `cache_write > 50K` AND `cache_read < 2 × cache_write` | The first cache write costs 25% more. If the data won't be re-read 2-3× within the 5-minute TTL, don't put it in the front of the prompt. |
| `REPEATED_USER_MSG` | Any user message text appears ≥ 2× | Re-injecting cached context via user messages is wasted spend and confuses the agent. Drop finished sub-tasks from context immediately. |

### Estimated savings (USD)

Conservative reclaim model — only the cache-miss penalty + duplicate-read waste, not the entire bill:

- **Cache-miss penalty**: shift tokens from the billable input bucket into `cache_read` until the session hits 70%. Saved = `shifted × (input_price − cache_read_price)`.
- **Duplicate-read waste**: for each file read more than once, count `2K tokens × (n − 1)` at base input price.

Real Anthropic billing doesn't break out per-tool spend, so the per-tool cost column is imputed from `n_calls × 2K_tokens × input_price` and is a heuristic, not a billing-API call.

### Dashboard structure

The HTML output has four regions, all from a single template (`render_dashboard()`):

1. **Overview** — 4 metric tiles: active sessions, total cost, avg score, avg cache hit ratio
2. **Cost & Token Distribution** — Cost by Repository (share bar) + Cost by Tool (share bar, with a yellow banner if `Read` is #1)
3. **Sessions** — one row per session: model, start time, input/output/cache-hit/cost, score pill, warning chips
4. **Actionable Insights & Estimated Savings** — USD savings callout (green gradient) + deduplicated warning blocks

### Constraints & guarantees

- **Stdlib only**: `argparse`, `collections`, `datetime`, `html`, `json`, `os`, `pathlib`, `statistics`, `sys`. No `pip install`, no transitive risk.
- **Single self-contained HTML**: inline `<style>`, no `<script>`, no external assets. Drop it in Slack, an email, or a PR — renders identically anywhere.
- **No TODOs / stubs**: the entire path is implemented end-to-end (parser → scoring → warnings → savings → HTML → write).
- **No completion claims without evidence**: see Verification below.

### Verification

The fixture at `fixtures/make_fixture.py` generates one synthetic JSONL per warning trigger, all with `cwd=/tmp/fixture-repo`:

```bash
$ python3 fixtures/make_fixture.py
wrote .../aaaa-low-cache.jsonl       (3 records)
wrote .../bbbb-read-heavy.jsonl      (2 records)
wrote .../cccc-heavy-ctx.jsonl       (2 records)
wrote .../dddd-opus-typo.jsonl       (2 records)
wrote .../eeee-write-not-reused.jsonl (2 records)
wrote .../ffff-repeated-msg.jsonl    (6 records)

$ python3 tools/token_efficiency_analyzer.py --repo "fixture-repo" --days 30 \
      --logs-dir fixtures/logs --out fixtures/out/dashboard.html
[ok] sessions=6  files_scanned=6  total_cost=$10.64  estimated_savings=$2.25
[ok] dashboard -> fixtures/out/dashboard.html
EXIT=0
```

All 6 warning codes (`CACHE_HIT_LOW`, `READ_HEAVY`, `HEAVY_CONTEXT`, `MODEL_OVERSPEC`, `WRITE_NOT_REUSED`, `REPEATED_USER_MSG`) appear in the rendered HTML; score pills render across all three bands (good / warn / bad).

### Why a tool, not a skill

This is a CLI, not a `/dev-kit:*` skill, because it operates on local files with no LLM call in the loop — wrapping it as a skill would force an unnecessary model round-trip for pure data transformation. Skills are reserved for steps where the model adds value (planning, design, review, eval). The loghooks that *produce* the input remain a skill (`/dev-kit:log`); the analyzer that *consumes* the output is a script.

## Cost gate (`/dev-kit:cost-gate`)

A **read-only cost measurement** layer. Distinct from the post-hoc `/dev-kit:token-analyzer` dashboard: cost-gate prints the running ledger on demand and emits the trailer block the PR aggregator needs; the analyzer replays historical JSONL sessions. Two layers, one state file at `<cwd>/.dev-kit/.cost-gate/state.json`. **The gate is observed only — it never blocks tool calls.** A runaway session still shows up in the dashboard, in commit trailers, and in the PR-level label, but the model is never denied a tool mid-flight because of a cost threshold.

| Layer | Trigger | Threshold (default) | Behavior |
|---|---|---:|---|
| **Session warn** | `/dev-kit:cost-gate` (human invocation) | `$5.00` | one-screen text status (`ok` / `warn`); visible to the operator, never a deny |
| **PR flag** | PR `opened`/`synchronize`/`reopened` | `$20.00` | applies `cost-flag` label + upserts a single `<!-- dev-kit:cost-gate -->` comment |

Override thresholds via env: `DEV_KIT_COST_WARN_USD`, `DEV_KIT_PR_COST_FLAG_USD`.

### Architecture

```
                /dev-kit:cost-gate skill (human invocation)
                          │
                          ▼
                tools/cost_gate_status.py   (CLI: text / json / html / footer / aggregate-pr)
                          │
                          ▼
                lib/cost_gate.py
                  ├── pricing tiers (opus/sonnet/haiku/minimax — INDEPENDENT of analyzer)
                  ├── transcript scanner (byte-offset cursor + dedup)
                  ├── heuristic fallback (per-tool token estimate, marked provenance)
                  ├── state I/O via lib/atomic.py
                  ├── footer parsing + dedup (per-session max)
                  └── threshold evaluation (warn / ok; no kill branch)
```

The PR aggregator (`.github/workflows/cost-flag.yml`) sums per-session maxima extracted from commit-footers:

```
Cost-gate: $8.42
Cost-gate-Session: <session-id>
```

The `/dev-kit:cost-gate` skill prints the exact two-line block via `--footer`; commit it as:

```bash
git commit -m "feat: thing" -m "$(python3 tools/cost_gate_status.py --footer)"
```

### Files

| Path | Purpose |
|---|---|
| `lib/cost_gate.py` | Pricing (4 tiers, substring match), transcript scanner with byte-offset cursor, heuristic fallback, state I/O, threshold eval (`ok` / `warn` — never `kill`), footer parsing, PR aggregation |
| `tools/cost_gate_status.py` | Read-only CLI. Flags: `--state`, `--json`, `--html PATH`, `--footer`, `--aggregate-pr --bodies-file PATH`. No hook modes — the cost-gate hook is removed |
| `skills/cost-gate/SKILL.md` | Human-use inspection skill (read-only, `disallowed-tools: Write Edit`). Renders current spend, threshold distance, and the trailer block |
| `.github/workflows/cost-flag.yml` | PR aggregator: applies / removes `cost-flag` label + upserts one `<!-- dev-kit:cost-gate -->` comment |
| `tests/test_cost_gate.py` | black-box tests: pricing, threshold eval, state I/O atomicity, heuristics, footer dedup, CLI modes, hook removal (no `hooks/cost-gate.sh`, no `cost-gate.sh` wiring in `hooks.json`), isolation |

### Isolation guarantee

`lib/cost_gate.py` and `tools/cost_gate_status.py` do **not** import `tools/token_efficiency_analyzer.py`. Both pricing tables, state files, and transcript scanners remain independent paths. The post-hoc dashboard (`/dev-kit:token-analyzer`) and the live gate (`/dev-kit:cost-gate`) read different files and have different lifecycles. A regression test (`TestIsolation`) asserts no cross-import.

### Manual smoke

```bash
# No state file → renders defaults, no error
$ python3 tools/cost_gate_status.py
scope: session  scope_id: ephemeral
status: ok  cost_usd: $0.00
...
session_warn: $5.00  pr_flag: $20.00

# Footer for the next commit
$ python3 tools/cost_gate_status.py --footer
Cost-gate: $0.00
Cost-gate-Session: ephemeral

# JSON / HTML render
$ python3 tools/cost_gate_status.py --json | jq .
$ python3 tools/cost_gate_status.py --html /tmp/cost.html
```

### Why a skill (not a hook)

- **Skill** is the human-read-only window into the running ledger. Renders current spend, distance to thresholds, and the trailer block the PR aggregator needs. Pure measurement — no hook can block a tool call.
- **CI workflow** is the cross-session aggregator — neither the post-hoc analyzer nor the cost-gate skill can read every session that contributed to a PR (state files are local to the worktree where the session ran). This is the only path that can label a PR `cost-flag`.

## Codex CLI compatibility (`.codex-plugin/plugin.json`)

Codex CLI's official plugin format ([openai/plugins](https://github.com/openai/plugins)) is a `.codex-plugin/plugin.json` manifest with a `"skills"` field pointing at a skills directory. The manifest points `"hooks"` at the bundled `.codex-plugin/hooks/hooks.json`; this copy mirrors the canonical `hooks/hooks.json` because Codex requires plugin-bundled hook files to remain inside the plugin root. Codex commands use `${PLUGIN_ROOT}`, while Claude Code commands use `${CLAUDE_PLUGIN_ROOT}`. Claude Code continues to load the canonical definition from `hooks/hooks.json`; a regression test keeps both event inventories synchronized.

To update the Codex plugin marketplace snapshot after a merge, run the
repository updater described in [Plugin cache refresh](#plugin-cache-refresh-when-and-how):

```bash
bash skills/codex-cache-update/scripts/update.sh
```

After enabling the plugin, review and trust its hooks with `/hooks` in Codex. Codex marks new or changed non-managed hooks for review and skips them until trusted. Run the local status check from the repository root:

```bash
python3 bin/dev-kit-hooks-status.py
python3 bin/dev-kit-hooks-status.py --json
```

The report distinguishes Claude Code registration, Codex registration and trust review, the `.dev-kit/.active-hooks.json` matrix, and Git's separate pre-push hook. Git enforcement is active only after configuring it explicitly:

```bash
git config core.hooksPath .githooks
```

`PreToolUse`, `UserPromptSubmit`, `SessionStart`, `PostToolUse`, and `Stop` are shared through the plugin hook definition. Claude Code's `permissionDecision` response is client-specific; Codex's hook trust and lifecycle handling remain separate and are reported rather than inferred from the Claude configuration.

Claude Code keeps reading `skills/` directly via `.claude-plugin/`, unaffected. MiniMax needs no generated artifact at all — it's a model backend, not a harness, reached by pointing either harness's existing model config at MiniMax's Anthropic-/OpenAI-compatible endpoint.

## Skills by audience

Not every skill appears in `/dev-kit:` slash autocomplete — only the user-facing ones do. Each skill's SKILL.md has a `user-invocable` frontmatter flag that controls this:

- **`user-invocable: true`** (or unset) — surfaces in `/dev-kit:` autocomplete. *You* type it.
- **`user-invocable: false`** — hidden from autocomplete. *Claude* auto-invokes it as a sub-step when its parent skill runs.

**If a skill name doesn't autocomplete, it's an internal sub-skill — you can't (and shouldn't) invoke it directly.** Type the user-facing parent instead (e.g. type `/dev-kit:refactor`, not `/dev-kit:build-refactor`).

### Finding the current skill surface

Only skills whose `SKILL.md` frontmatter sets `user-invocable: true` appear in
slash autocomplete. Internal skills set it to `false` and are dispatched by a
parent skill. Inspect the frontmatter or use the client's autocomplete for the
current surface; this README intentionally avoids duplicating a changing skill
inventory.

The parent skill owns the workflow; inspect its `SKILL.md` when you need the
internal dispatch details.

**Mental model**: user-facing skills are verbs (the *what*). Model-use skills are the actual mutating machinery (the *how*). Slash autocomplete surfaces only the verbs; Claude fills in the machinery.

## Refactor (`/dev-kit:refactor`)

Whole-pipeline **refactor** in 3 gated phases — read-only baseline (`/dev-kit:inspect`) → mutating 4-pass cleanup (dead → dup → naming → coverage) → per-diff verification (`/dev-kit:review`). Each phase gates the next on a quoted exit code + test count; no phase starts without the previous phase's green evidence (MUST-L3, hook `stop-verify`).

```
[1/3] inspect  -> /dev-kit:inspect         (read-only)
                 ↓ quoted: report path + verdict + finding count
[2/3] refactor -> 4-pass cleanup   (dead → dup → naming → coverage; mutating)
                 ↓ quoted: 4 x (pass name + test count + exit 0)
[3/3] review   -> /dev-kit:review          (3-dim: correctness + security + architecture)
                 ↓ quoted: per-dim finding count + overall verdict
```

No version-gated precondition — dev-harness-kit is itself the provider of `/dev-kit:ci-setup`, so requiring a consumer-side marker here would be self-referential. Optional `--phase N` (1|2|3) re-runs only that phase. Full contract: [`skills/refactor/SKILL.md`](skills/refactor/SKILL.md).

> **This skill rewrites code, it does not delete it.** For project-wide deletion of AI slop, dead code, and unused features, use `/dev-kit:prune` instead. For removing one named feature end-to-end, use `/dev-kit:feat-remove <feature>`.

## Prune (`/dev-kit:prune`)

Whole-pipeline **deletion sweep** in 3 gated phases — same baseline (`/dev-kit:inspect`) → 3-pass deletion (orphan-code → dead-feature → slop-pattern) → per-diff verification (`/dev-kit:review`). Mirrors `refactor` in shape but **removes** rather than rewrites.

```
[1/3] inspect  -> /dev-kit:inspect         (read-only)
                 ↓ quoted: report path + verdict + finding count
[2/3] prune    -> 3-pass deletion  (orphan-code → dead-feature → slop-pattern)
                 ↓ quoted: 3 x (pass name + test count + exit 0)
                 ↓ each pass emits rm/git-rm commands for the user to run
[3/3] review   -> /dev-kit:review          (3-dim: correctness + security + architecture)
                 ↓ quoted: per-dim finding count + overall verdict
```

The skill never calls `rm` itself. It writes deletion candidates (path + reason + dependents) to `.dev-kit/hand-off/prune-report.md` and waits for the user to run the commands — same discipline as `/dev-kit:feat-remove`. Refuses to cascade to dependents without explicit user ack. Full contract: [`skills/prune/SKILL.md`](skills/prune/SKILL.md).

> **This skill deletes code, it does not refactor it.** For pure refactoring (rename, extract, dedup), use `/dev-kit:refactor` instead. For one named feature, use `/dev-kit:feat-remove <feature>`.

## Agent-behavior eval (`/dev-kit:eval`)

Measures whether the **agent produces the right output for the right input** when running the dev-kit skills. The unit of eval is a **case fixture + a recorded agent transcript → per-dim rubric judgment** (not a file on disk). Replay-only in v1: a case without a recorded transcript is `SKIPPED` (setup gap, not a regression). Live re-run is a v2 follow-up.

### Three dimensions, three per-dim rubrics (each axis 0–10)

| Dim | Axes | What it measures |
|---|---|---|
| `review` | `verdict_consistency` · `severity_calibration` · `precision` · `recall` · `code_sanity_score` | review verdict + findings quality + clean-code/over-eng/value rubric |
| `security` | `owasp_classification_accuracy` · `severity_accuracy` · `precision` | OWASP A01–A10 mapping + severity + false-positive rate |
| `plan` | `spec_clarity` · `step_atomicity` · `ac_executability` · `dependency_ordering` | ambiguity ≤ 3 per step · single-deliverable steps · runnable AC · buildable order |

Per-case axis mean drives the verdict: **OK** ≥ 8.0 · **DRIFT_WARNING** 5.0–7.9 · **ROT** < 5.0 · **SKIPPED** (no transcript).

### Code-sanity rubric (20 checkboxes)

Embedded in the `review` judge for the `code_sanity_score` axis. Composite = `0.4 × clean + 0.4 × over_eng + 0.2 × value`:

- **Clean code (CC-1..8):** vague names · function > 50 lines or > 4 params · dead code / unused imports · magic numbers without constants · copy-paste duplication · bare except / swallowed errors · type unsafety · stale comments
- **Over-engineering (OE-1..8):** interface with one implementer · speculative params · premature optimization · YAGNI features · excessive layering · factory/Strategy for one impl · deep inheritance · 1-class-per-file without justification
- **Value / meaning (VM-1..4):** stated purpose tied to a real user need · not noise/cosmetic/churn · scope matches the problem · the diff earns its lines

The full checklist lives in `eval/prompts/judge-code-sanity.md`; pinning it in the ADR-0022 freezes the rubric. Any change to the 20 items requires an ADR update.

### Seed cases (across 3 dims)

```
eval/cases/
├── review/
│   ├── 01_sql_injection.json          (real-bug)         → Blocked, ≥1 security/major+
│   ├── 02_parameterized_query.json    (trap)             → Approve, 0 findings
│   ├── 03_addition.json               (clean)            → Approve, 0 findings
│   ├── 04_factory_one_impl.json       (over-engineering) → Changes, ≥1 architecture/major
│   ├── 05_long_function.json          (clean-violation)  → Changes, ≥1 correctness/minor
│   └── 06_churn_only.json             (no-value)         → Approve + value comment
├── security/
│   ├── 01_owasp_a01_idor.json         (A01)              → A01, major
│   ├── 02_owasp_a05_sql_injection.json (A05)             → A05, critical
│   └── 03_trap_safe_hash.json         (trap)             → Approve, 0 findings
└── plan/
    ├── 01_clear_spec.json             (clear-spec)       → 3 atomic steps, runnable AC
    ├── 02_ambiguous_spec.json         (ambiguous-spec)   → held at Gate 2, re-decompose
    └── 03_coupled_spec.json           (coupled-spec)     → split into ≥4 steps
```

Review cases 01–03 reuse the existing `skills/review/fixtures/{real-bugs,traps,clean}/*.py`; the other 6 fixtures live in `eval/cases/<dim>/*.py`.

### Architecture (files)

```
eval/cases/<dim>/<name>.json     # input + expected behavior
eval/transcripts/<dim>/<name>.json  # recorded agent output (replay)
eval/prompts/judge-<dim>.md     # per-dim LLM-as-judge rubric
eval/prompts/judge-code-sanity.md  # shared 20-checkbox rubric
eval/golden/<dim>-<name>-<hash>.json  # frozen baseline (schema in ADR-0022)
lib/eval_runner.py              # discover_cases → replay → judge → report
lib/llm_judge.py                # keep JUDGE_AXES; add DIM_AXES
```

### CLI

```bash
# Full eval (all cases across 3 dims) — writes .dev-kit/eval-report.md
python lib/eval_runner.py --project-root . [--dry-run]

# Restrict to one dim
python lib/eval_runner.py --project-root . --dim plan

# Restrict to a single case
python lib/eval_runner.py --project-root . --case review-04-factory-one-impl
```

`--dry-run` skips LLM calls and mocks each case at 7.0/DRIFT_WARNING — useful in CI without an API key.

### Adding a new case

No code change required. Two JSON drops:

```bash
# 1. drop the case fixture
$EDITOR eval/cases/review/07_my_new_case.json
# {
#   "case_id": "review-07-my-new-case",
#   "dim": "review",
#   "category": "real-bug",
#   "input_path": "path/to/fixture.py",   # or input_inline for plans
#   "expected": { "verdict": "Blocked", "min_severity": "major", ... },
#   "schema_version": "<see ADR-0022>"
# }

# 2. record + drop the transcript (one-time per case)
$EDITOR eval/transcripts/review/review-07-my-new-case.json
# { "case_id": "...", "dim": "review",
#   "agent_output": { "verdict": "...", "findings": [...] } }

# 3. run; new case shows up in .dev-kit/eval-report.md
python lib/eval_runner.py --project-root . --dry-run
```

Missing transcript = `SKIPPED` (a setup gap, not a regression). API error per-case = `ROT`, loop continues.

See `docs/adr/ADR-0022-eval-agent-behavior.md` for the full rationale, alternatives considered, and consequences.

## Consumer-install via `/dev-kit:ci-setup`

`/dev-kit:ci-setup` is what makes dev-kit work in OTHER repos. It copies:
- GitHub Actions workflows (ci, auto-fix-pr, review)
- scripts (validate, test, branch-policy, ci-local)
- a pre-push hook
- worktree-rule files (hooks, lib, rule, tests)

The shipped `review.yml` is **self-aware**: it detects whether the checkout IS the dev-kit plugin (self-install, symlink) or a plain consumer repo (clones from public), so the same workflow file works in both contexts.

**Switching the CI review provider** (MiniMax / Anthropic / DeepSeek): edit `CI_REVIEW_PROVIDER` in your local `.env`, then commit. `.githooks/pre-commit` reads it from the main checkout's `.env` (works from any worktree) and syncs the git-tracked `.github/ci-review-provider.txt` into that commit if it drifted — CI itself only ever reads the tracked file, since a GitHub-hosted runner can't see your local `.env`. Each provider needs its matching repo secret (`MINIMAX_API_KEY`, `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY`) pushed via `gh secret set` first. Note: a PR that itself edits `.github/workflows/review.yml` will always get skipped by `claude-code-action`'s own anti-tampering guard ("workflow file must match the default branch") — that's expected and resolves itself once such a PR is merged.

After install, consumer repos get:
- The branch strategy enforced
- TDD + slop (v2: multi-tier + KO structural coverage) + secret hooks
- A worktree rule that prevents editing in the main checkout
- 4 regression tests for the worktree rule

### `/dev-kit:ci-setup --force` — when to use vs when NOT

`ci-setup` is **idempotent by default**. The marker file `.dev-kit/ci-config.json` records installed_at + content hashes; if all EXPECTED_PATHS files are present and match, the re-run is a no-op (skip + report). `--force` overwrites the EXPECTED_PATHS files regardless.

**Use `--force` when:**
- First install on a fresh repo (default install is also correct on a truly fresh repo, but `--force` is robust against partial installs from a previous attempt and against a stale plugin cache — see "First-time consumer setup" above)
- A new template was added to dev-kit (e.g. `templates/ci/scripts/branch-policy.sh`) and you want it on your consumer repo
- A template was fixed in dev-kit (e.g. PR #45 patched a stale `review.yml` gate) and you want the fix
- You suspect the install is stale: marker exists but a template file is missing or drifted (often from a stale plugin cache — see "Plugin cache refresh" above)
- The lint pass on a previous install reported warnings that need a refresh to clear

**Do NOT use `--force` when:**
- Re-running after no upstream changes (default is a no-op skip; `--force` creates noise in the diff and may overwrite local customizations)
- The consumer repo has hand-edited any of the EXPECTED_PATHS files (e.g. a customized `branch-policy.sh`). `--force` overwrites your edits — review the diff before committing
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

# 4. Verify CI readiness before opening the first PR — /dev-kit:ci-doctor prints PASS/FAIL
/dev-kit:ci-doctor
# Re-run after each remediation until verdict=PASS, then push + open PR

# 5. Commit + push
git add -A && git commit -m "chore(ci): refresh dev-kit templates"
```

## Design principles (ADR-0011~22)

- **NO-DUP**: Iron Law in one place (CLAUDE.md §1). 2-layer verification (hook + skill).
- **NO-BOTTLENECK**: 0-arg UX. Lazy CLAUDE.md. Sub-agents in parallel.
- **NO-MEANINGLESS-LOOP**: 5-field loop semantics + auto-STOP + user interrupt.
- **HOTL (MUST-29)**: auto-progress + user supervisor + 1× interrupt.
- **Methodology extension (MUST-48)**: TDD/SDD/DDD/BDD/FDD selectable.
- **A2A typed (MUST-39)**: Sub-agent ↔ main JSON Schema SSOT.
- **Plugin-only**: `.claude/skills/`, `commands/`, `install.sh` all removed. plugin manifest is SSOT.
- **Worktree-per-task**: every task is a new worktree + client-specific handoff + new branch. Enforced by 3 hooks, documented in `rules/git-workflow.md`.
- **Consumer-install**: the same `review.yml` + worktree-rule files work in both dev-harness-kit and consumer repos via a self-aware install step.

## Contributing

Pass pre-impl gate (`docs/PRE-IMPL-CHECK.md`) + 8-dimension cost (`docs/COST-ANALYSIS.md`).

```bash
python3 -m pytest tests/ -q
claude plugin validate .claude-plugin/plugin.json
```

## License

MIT

## Status

🚀 **Active development. Ongoing: per-skill drift audit, slop-detector (multi-tier scan), eval case expansion, template refresh.**

See [`docs/STAGES.md`](docs/STAGES.md), [`docs/NAMING.md`](docs/NAMING.md), [`CHANGELOG.md`](CHANGELOG.md).
