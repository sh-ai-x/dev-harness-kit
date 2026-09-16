# Cache hit rate — CI enforcement (PR #765)

> Companion record to [`structural-fix.yaml`](structural-fix.yaml).
> The proposal ships F1 + A1 + A2 as advisory-only audits. PR #765
> turns them into a hard CI gate via `.github/workflows/cache-decay-audit.yml`.

## What changed since the proposal landed

The proposal's §Validation gates defined:

| Gate | Mechanism | Status |
|---|---|---|
| G1 — analyzer doesn't regress | `tests/test_token_efficiency_analyzer.py` (13 cases at the time) | ✅ passing |
| G2 — `cache_decay` field emitted | new test | ✅ passing |
| G3 — HTML tile renders | new test (regex on output) | ✅ passing |
| G4 — hook stdout determinism | new test | ✅ passing — but **advisory only** |
| G5 — skill frontmatter audit | new test | ✅ passing — but **advisory only** |
| G6 — L6 governance | `tests/test_skill_governance.py` | ✅ passing |
| G7 — analyzer hit-rate movement | manual | not yet measured |

PR #765 ships the change that closes the "advisory only" gap on G4 + G5:
a new GH-Actions workflow runs both audits on every PR + push-to-main,
and a downstream `aggregate_report` job surfaces the failure to branch
protection via a deterministic terminal guard.

## Workflow shape

```
                  ┌────────────────────────────────────────┐
                  │  .github/workflows/cache-decay-audit │
                  └────────────────────────────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                                                             ▼
┌──────────────────────┐                                  ┌──────────────────────┐
│ G4 — hook stdout     │                                  │ G5 — skill frontmatter│
│ determinism          │                                  │ audit                 │
│ (timeout: 2 min)     │                                  │ (timeout: 2 min)      │
└──────────────────────┘                                  └──────────────────────┘
        │                                                             │
        └──────────────────────────────┬──────────────────────────────┘
                                       ▼
                          ┌──────────────────────────┐
                          │ cache-decay-audit summary │
                          │ (always runs; exit 1 on  │
                          │  failure — branch gate)  │
                          └──────────────────────────┘
                                       │
                                       ▼
                          ┌──────────────────────────┐
                          │ PR comment (idempotent    │
                          │ marker-based overwrite)   │
                          └──────────────────────────┘
```

## Required precondition

The PR-comment step requires the repo's default `GITHUB_TOKEN` to have
**read + write** PR-comment scope. Most repos enable this by default
under *Settings → Actions → General → Workflow permissions →
"Allow GitHub Actions to create and approve pull request comments"*.

When the permission is read-only, the `gh api POST` 403s. To prevent
the 403 from turning `aggregate_report` red for an unrelated reason,
the comment step carries an explicit fork-PR guard
(`github.event.pull_request.head.repo.fork != true`): fork PRs skip
the comment entirely, run the upstream audits (G4 + G5), and surface
a verdict via branch protection — the human-visible comment is the
only thing that's missing. The workflow's `aggregate_report` terminal
guard does **not** depend on the comment step, so the branch gate
still fires on every PR, fork or not.

## Why this is a separate workflow

`maintenance.yml` already runs an LLM-judge (`/dev-kit:maintenance`)
on every PR, but the judge is heavyweight (~15 min, $0.05+ per run,
subject to rate limits). The cache-decay audits are deterministic —
bash + python — and run in <30s. Folding them into `maintenance.yml`
would either:

1. **Inflate cost** — every cache-decay-audit run burns 15 min of GH-Actions
   minutes that could be a sub-second bash invocation.
2. **Defeat the hard-rule tier** — `maintenance.yml` is the "verdict
   + docs-updated" soft tier (it can land on Changes Requested for
   reason X and still approve the docs sub-gate). The cache-decay
   surface is the kind of thing that should hard-fail a PR, not
   appear as one finding among many.

So: separate workflow, deterministic, hard gate. Maintenance.yml
still reviews the same code surface on the LLM-judge tier — they're
complementary, not redundant.

## Why this is not folded into `lint (ruff)` or `test (python)`

- **ruff**: caches, not audits. It checks syntax/style, not
  prefix-stability invariants.
- **pytest**: validates the analyzer code itself. It catches a wrong
  implementation but not a *correct* implementation against a
  *wrong* repo surface (the audits' job).

The audits ARE the runtime oracle: they invoke every `hooks/*.sh`
twice and walk every `skills/*/SKILL.md` frontmatter. A test that
doesn't do that is a regression on the meta-test, not the surface.

## Failure mode playbook

| Symptom | First action |
|---|---|
| G4 fails on a clean PR | A contributor added `date` / `$RANDOM` / `/dev/urandom` to a hook's stdout. Fix: move the volatile content into the **tool result body** (the model still sees it), not the **hook stdout** (which sits in the prefix). |
| G5 fails on a clean PR | A contributor added `last-updated: $(date)` (auto-regenerating) to a `SKILL.md` frontmatter. Fix: rename to a maintenance-allowlisted key (`last-reviewed` / `reviewed-at` / `committed-at`) or remove the field. |
| Both fail | Almost certainly a CI-only difference (line endings, bash version, env vars). Diff your local result with `bash --version` on the runner image (`ubuntu-24.04`). |

## Future work (deferred)

Per the proposal's §Recommended approach, G7 measurement is the gate
to Phase 2:

- **D — sub-agent prompt isolation**: each `Agent` call inherits the
  parent's volatile prefix; sub-agents should pay `cache_write` once
  for a clean prefix.
- **E — `cache_control: { ttl: "1h" }` markers** in dev-kit's own
  skill loader.
- **G — one-model-per-session lock** (3 `MODEL_OVERSPEC` warnings).

Phase 2 is gated on G7: post-merge re-run of `--days 30 --json`;
aggregate hit ≥97% OR `CACHE_HIT_LOW` count drops ≥20%. Otherwise
Phase 2 is unnecessary.

Also out of scope: `stale_cost_usd = 86.7%` of total is the dominant
cost lever and belongs in its own `stale-worktree-cleanup` umbrella.