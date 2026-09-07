# dev-harness-kit — PAS (Problem · Analysis · Solution)

> **What problem does this repo solve, and how?**
>
> A 5-minute orientation for newcomers who want the *why* before the
> *what*. If you've never seen dev-harness-kit before, read this first,
> then [`00-index.md`](00-index.md) for the install + 60-second tour.

**Language:** English

---

## Problem

AI coding agents — Claude Code, Codex, and their siblings — are fluent
enough to ship real code, but they are also fluent enough to **talk
their way past the guardrails** the operator asked them to follow.

In practice that surfaces as a small, recurring set of failures:

| Failure mode | What it looks like |
|---|---|
| **TDD skipped under pressure** | The model writes the implementation, then back-fills a test that asserts the implementation is correct. |
| **Commits straight to `main`** | "It's a tiny change, no PR needed" — until a tiny change breaks prod. |
| **"Done" without evidence** | The model declares a build step complete with no quoted exit code, no failing-then-passing test, no diff the operator can audit. |
| **Destructive commands slip through** | `rm -rf`, force-pushes, `git reset --hard`, and credential leaks happen because the model wasn't asked to confirm. |
| **The plan was wrong but we kept going** | A bad assumption in step 1 propagates through step 12 because no one re-validates the spec mid-build. |
| **Lost work between sessions** | The laptop closes mid-step; the next session has no idea where the build was, which tests had run, or which decisions were pending. |
| **Review drift** | "Approved!" without per-line findings, no verifier pass, and no deterministic test that the fix actually fixed the bug. |

These are not edge cases — they are the *median* outcome of an
unguarded agent session. Every operator who has shipped with an AI
agent for more than a sprint has hit at least three of them.

The unifying theme: **soft prompts get skipped. Deterministic
guardrails do not.**

---

## Analysis

The failures above are not caused by the model being "bad" — they are
caused by the model being **optimizable for completion** in a way that
is orthogonal to the operator's actual goals. A polite instruction
("please commit to a branch, please write a test first, please don't
push to main") is a soft constraint the model can satisfy in form while
violating in substance, because the model has no penalty for doing so.

Three properties have to hold for the guardrail to actually work:

1. **Deterministic.** The check runs as code — shell hooks, regex
   matchers, file-existence asserts — not as a *request* the model can
   deprioritize. "Please don't push to main" fails the first time the
   model decides it's expedient.
2. **Stateful.** A plan, a build step, a verdict, a regression test —
   the harness persists them as durable artifacts the next session
   must reconcile against. The work cannot silently evaporate when the
   session ends.
3. **Verifiable.** A claim of "done" must be backed by an artifact the
   operator can read: a test that failed and then passed, an exit code,
   a diff the operator can audit. The model's confidence in its own
   completion is **not** a verification artifact.

The corollary is that the harness cannot be a *prompt* — it has to be
a *runtime* that wraps the agent's tool calls. The agent does not see
the harness as a paragraph of instructions; the agent sees it as
**PreToolUse** and **PostToolUse** hooks that intercept `Bash`, `Edit`,
`Write`, and `Stop`, plus a stage machine (`plan → build → review →
ship`) that the agent cannot advance without satisfying the previous
gate.

A second-order constraint follows: the harness has to be **dual-runtime**
because Claude Code and Codex wire hooks through different manifests
(`.claude/settings.json` + `hooks/hooks.json` vs `.codex-plugin/plugin.json`
+ `hooks/hooks.json`). The same enforcement semantics must survive the
move — no "Claude-only" hooks, no "Codex-only" skills.

---

## Solution

`dev-harness-kit` is one small dual-runtime plugin that turns the three
properties above into a single installable package.

### Deterministic layer — hooks

Eight+ hooks fire automatically on every tool call, in both runtimes:

- `worktree-guard` — hard-blocks `Edit`/`Write`/`MultiEdit` in the main
  checkout (every task must run in its own worktree).
- `git-guard` — hard-blocks `git commit` on `main`, `git push` to `main`,
  `git push --force`, and `git push --force-with-lease` outside your
  own unmerged branch.
- `tdd-guard` — refuses production code without a failing test in the
  same diff.
- `bash-guard` — two tiers: a catastrophic tier (catastrophic
  commands deny unconditionally — even `DEV_KIT_STRICT=1` cannot
  override them) and a recoverable tier (destructive-but-reversible
  commands deny under strict mode).
- `secret-scan` / `slop-detector` / `l4-todo-scan` — pattern banks
  loaded from `hooks/references/` that block credential leaks,
  high-signal "AI slop" patterns, and TODO/FIXME markers.
- `stop-verify` — at `SessionEnd`/`Stop`, re-runs the regression test
  before the model is allowed to declare "done".

Full inventory in [`docs/hooks/HOOK-REFERENCE.md`](../hooks/HOOK-REFERENCE.md)
and the matrix in [`hooks/index.md`](../../hooks/index.md).

### Stateful layer — stage machine

The same plugin pins a stage spec:

```
bootstrap → (evidence-plan?) → plan → valuate → build → review → security → ship
```

Each stage owns its own slash (`/dev-kit:plan`, `/dev-kit:build`,
`/dev-kit:review`, etc.), reads from a deterministic input, and writes
a deterministic output (a `PRD.md`, a `phases/<name>/index.json`, a
verdict JSON envelope). Re-running a stage is always safe — it picks
up from the first step that isn't `completed`.

The stage spec is the single source of truth; the prose is just
orientation. See [`docs/stages/STAGES.md`](../stages/STAGES.md).

### Verifiable layer — the Eval-Repair loop

Every "done" claim must carry one of:

- a failing-then-passing test (`build-tdd`, `tdd-guard`),
- a quoted exit code + test count + build log (`build-verify`,
  `stop-verify`),
- an LLM-judge verdict with per-line findings (`/dev-kit:review`,
  `/dev-kit:security`),
- a deterministic scorecard (`/dev-kit:security-metrics`,
  `/dev-kit:harness-effectiveness`).

Missing evidence is reported as `INSUFFICIENT_EVIDENCE`, never
inferred. The harness cannot be talked into accepting a bare
assertion of completion.

### Dual-runtime portability

The same hooks + skills run under Claude Code and Codex via a shared
`hooks/hooks.json` and a `.codex-plugin/plugin.json` mirror. A
regression test (`tests/test_hooks_parity.py`) keeps the two manifests
in lockstep. The plugin can also be installed into *consumer* repos via
`/dev-kit:ci-setup` — one self-aware workflow set works in this repo
and in any repo that adopts it.

### Why "one plugin" instead of many

The failure modes above are *coupled*: a missing regression test breaks
the review; a missing worktree breaks the merge; a missing plan breaks
the build. Splitting the guardrails across many optional tools lets an
operator pick the cheap subset and inherit none of the coupling. By
shipping them as one plugin with a stage spec, every guardrail is
installed (or none is) — there is no half-installed state where
`tdd-guard` is on but the worktree rule is off.

---

## What's NOT in scope

Three things this repo deliberately does not do, because doing them
would weaken the core guarantees:

- **MCP server**. The plugin is hooks + skills + library functions;
  no MCP entry point. See
  [`docs/decisions/0001-no-mcp.md`](../decisions/0001-no-mcp.md) for
  the rationale.
- **In-process shared state**. The plugin's two runtimes communicate
  via JSON envelopes on disk (`.dev-kit/`) and direct subprocess
  calls — no shared memory, no IPC. This keeps the model context
  cache stable across hooks and keeps the consumer-install contract
  portable.
- **Soft prompts as enforcement**. Anything written as "the model
  should …" rather than as a hook that denies the call is not in
  this repo. The `L8` Iron Law explicitly forbids restating hook
  contracts in skill prose.

---

## Next step

- New here? Read [`00-index.md`](00-index.md) — the 60-second tour.
- Looking for the install command? See the parent
  [`README.md`](../../README.md) → *Install* / *Quickstart*.
- Looking for the architectural decisions behind specific choices?
  [`docs/adr/`](../adr/) holds the full ADR series.
- Looking for one specific stage? [`docs/stages/STAGES.md`](../stages/STAGES.md)
  pins the stage spec; the per-skill pages live under
  [`docs/skills/`](../skills/).
