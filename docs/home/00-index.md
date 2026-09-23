# dev-harness-kit — Documentation Home

> A unified harness plugin for shipping — Plan, Build, Review, Ship with typed
> sub-agent delegation, an Eval-Repair loop, and Human-on-the-Loop supervision.
> Hooks, agents, and operators share one Python codebase via direct subprocess
> calls (no shared in-process state substrate).


---

## TL;DR — What is dev-harness-kit?

**dev-harness-kit = one small Claude Code / Codex plugin + a per-stage
harness spec + an Eval-Repair loop.**

The plugin ships with a fixed set of skills (`/dev-kit:plan`,
`/dev-kit:build`, `/dev-kit:review`, `/dev-kit:ship`, …), a hook-based
enforcement matrix (`worktree-guard`, `git-guard`, `tdd-guard`,
`bash-guard`, `secret-scan`, `slop-detector`, `stop-verify`, and more —
see the full, current inventory in the root
[`README.md` → Under the hood](../../README.md#under-the-hood)),
and a stage-by-stage spec (`docs/stages/STAGES.md`) that pins which
skill owns which step and what the acceptance criteria are.

| Property | Value |
|---|---|
| **What it is** | A Claude Code + Codex plugin (skills + hooks + commands) |
| **Stage spec** | Bootstrap → Plan → Valuate → Build → Review → Security → Ship |
| **State substrate** | Direct shell + subprocess; no shared in-process cache |
| **Wire format** | JSON envelopes on disk (`.dev-kit/`) |

Everything below explains *why this exists*, *how to use it in 60 seconds*,
*what you get out of it*, and *where every other doc lives*.

---

## 1. Why this system exists, and why it has to exist

> **Start here if you have never seen dev-harness-kit before.**

### The pain

Multiple files in `hooks/` and `lib/` each needed the same live state
("what is the current branch slot version?" or "is PR #447 green?").
Each one shell-out to `git` or `gh`, parsed the JSON inline, wrapped
errors in its own shape, and re-implemented caching — or skipped it
entirely. Drift was inevitable: a new field in `gh pr view` would break
three hooks on a different day each.

The harness answers this with strict per-stage ownership (one skill
per stage), shared hook enforcement (no per-file re-implementation),
and a typed on-disk envelope (`.dev-kit/hand-off/*.md`,
`.dev-kit/valuations/<plan-id>.json`, `phases/<name>/index.json`).

**Why it has to exist**: in a multi-runtime harness (Claude Code +
Codex), every consumer that re-implements state-reading creates a
cross-runtime failure surface. The hook matrix collapses that surface
to a shared set of shell scripts that every consumer uses.

---

## 2. 60-second quickstart

> **If you only run three commands today, run these.**

You do not need to read the rest of the docs to use dev-harness-kit.
The first three commands below produce a runnable demo in under a
minute.

### Step 1 — bootstrap a fresh repo

```bash
/dev-kit:bootstrap
# writes CLAUDE.md + AGENTS.md + .dev-kit/.active-hooks.json
```

### Step 2 — explore the per-stage harness

```bash
$ open docs/stages/STAGES.md   # the per-stage spec
```

### Step 3 — explore the full skill surface

```bash
$ ls skills/   # every /dev-kit:<skill> name + its SKILL.md
```

That is the entire surface. Anything more specific is in the docs.

---

## 3. What value you actually get

> **Concrete numbers, not promises.**

| Metric | Value | Detail |
|---|---|---|
| Hooks shipped | see [Under the hood](../../README.md#under-the-hood) | `worktree-guard`, `git-guard`, `tdd-guard`, `bash-guard`, `secret-scan`, `slop-detector`, `stop-verify`, and others — the table there is the current, maintained inventory |
| Stage owners | **6** | bootstrap, plan, build, review, security, ship |
| Eval-Repair loops | **2 dims** | harness-quality + os-quality |
| Return shapes | **1 per stage** | typed envelope contract pinned by `docs/stages/STAGES.md` |

### Three concrete wins

1. **Slot-bump drift detection.** `hooks/git-guard.sh` checks whether
   your `plugin.json` bump matches `origin/main` before a push. No
   state substrate required — direct `git show
   origin/main:.claude-plugin/plugin.json` + JSON parse.

2. **Build no-go gate transparency.** The plan-value gate was removed
   entirely (PR chore/remove-valuate); the prior advisory envelope
   (`decision` / `rationale` / `blocking_findings`) at
   `.dev-kit/valuations/<plan-id>.json` no longer exists. `/dev-kit:plan`
   owns value judgment via its interview hand-off; the build proceeds regardless.

3. **Pre-push intent check.** `.githooks/pre-push` runs the maintenance
   gate on every push; `hooks/worktree-guard.sh` denies Edit/Write in
   the main checkout; `hooks/git-guard.sh` blocks `git commit` on
   `main`. Three deterministic checks, zero shared state.

---

## 4. Documentation map

> Pick what fits your role. Read top-to-bottom for newcomers.

### Newcomer path (read in order)

- **00 — Documentation Home** (this file)
  > Why the system exists, what value you get, and where to go next.
- [`../../README.md`](../../README.md) — repo `dev-harness-kit`
  > Repo-level overview: install, the Plan/Build/Review/Ship loop, command
  > reference, and the full skill surface.
- [STAGES — per-stage harness spec](../stages/STAGES.md)
  > What happens in each stage of the loop (bootstrap → plan → build →
  > review → ship), and which skill owns which step.

### Every other doc, by category

The root [`README.md` → Doc map](../../README.md#doc-map) is
the single categorized index of every topic doc, ADR, and skill reference
in the repo (architecture, naming, CI setup, cost/risk, team adoption, hook
coverage, and the skill index) — with HTML/Markdown siblings
linked per row. This page stays a Markdown-only landing page and does not
duplicate that table; follow the link above for the full map.

### Hooks + enforcement

- [Hook coverage gaps](../hooks/hook-coverage-gaps.md) — what's enforced, what's
  not, and what the next enforcement candidates are.
